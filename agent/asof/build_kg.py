"""스냅샷 DB -> 스냅샷 KG와 조건 B DB. 기존 파이프라인 스크립트를 수정 없이 하위 프로세스로 실행한다.

순서
  1. scripts/build_kg.py <스냅샷 DB> --out <config.kg_dir(이름)>      R2RML 매핑, T-Box 폐포, 파생 관계
  2. scripts/build_condition_b.py <스냅샷 DB>                          <이름>_B.sqlite(스냅샷 DB와 같은 폴더) + 뷰 행 수 대 KG 대조
  3. 위반 그래프 유도와 적재(아래)
  4. abox.nt의 sha256을 기록하고 삭제(수백 MB. 저장소에 같은 내용이 있다)
  5. 검증(validate.kg_bounds, tbox_parity, 조건 B DB의 시각 상한, scripts/check_kg.py)과 실행 목록 asof_manifest.json 작성
build_condition_b.py와 check_kg.py는 KG 위치를 DB 파일 이름으로 정한다(data/derived/kg/<DB 이름>). config.kg_dir와 같은지 확인한다.
실행 목록에는 원본 시나리오, t, 판, 해시, 행 수, 위반 집계, 검증 결과, 소요 시간을 남긴다. 정답 파일의 내용은 쓰지 않는다.
이 모듈은 정답 파일을 읽지 않는다. 정답이 필요한 검증은 scripts/agent_prepare_asof.py가 함수(kg_checks)로 넘긴다.

위반 그래프(<https://w3id.org/ontosemc/graph/violations>)
  스냅샷 KG에 SHACL을 다시 실행하지 않는다(시나리오당 3시간). 원본 KG의 위반 사실에서 t 시점의 위반 집합을 유도한다.
  원본에 적재된 위반은 전부 Q-time 위반이다. 유도 규칙은 "스냅샷 KG에 QTimeShape를 실행했을 때 나올 결과"와 같게 잡았다.
  - 기한(deadline)이 t보다 이른 위반만 남긴다
  - 종료 공정 투입(exitExecution)이 t 뒤이거나 없으면 대기 중으로 쓴다: exitExecution 삭제, stillWaiting true,
    overrunHours = (t - 기한) 시간. 반올림은 validate_kg.py와 같다(소수 넷째 자리)
  - 종료 공정 투입이 t 이전이면 그대로 복사한다
  - 진입 실행(violationValue)이 스냅샷 KG에 없는 위반: 같은 진입을 가리키는 기록 시작 스냅샷(QTimeSnapshotEntry)이 스냅샷 KG에
    있으면 violationValue를 그 항목으로 바꾼다. 없으면 뺀다. strict 판에서는 t에 완료하지 않은 lot의 YMS 이력이 스냅샷 KG에
    없으므로 그 이력에만 있는 진입 실행이 여기에 해당한다(SHACL도 스냅샷을 진입으로 잡거나, 진입을 찾지 못한다)
  - violationMessage는 lot, 진입 종료 시각, 기한만 담는다(전부 t 이전). 문구 안의 시각이 t 뒤이면 그 속성을 뺀다
  - 위반 IRI의 일련번호는 남긴 위반만으로 다시 매긴다(원본 번호의 빈 자리가 t 뒤의 위반 수를 드러내지 않게 한다)
  - validate_kg.py와 같이 fab.ttl을 기본 그래프에 한 번 더 넣는다. 원본 KG와 T-Box 부분이 같아진다(1,662 트리플)
  스냅샷 KG 저장소는 이 단계에서만 쓰기로 열고, flush한 뒤 닫는다.
  확인: 유도한 위반 수가 스냅샷 DB의 SQL 건수와 같다(check_kg.py의 SHACL 3항목. 만든 3쌍에서 266, 1,191, 929건 일치)
"""
import gc
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .. import config
from ..tools.sparql_common import DATA, FAB, VIOLATION_GRAPH
from . import asof_name, build_db

MANIFEST = 'asof_manifest.json'
MANIFEST_VERSION = 1
XSD = 'http://www.w3.org/2001/XMLSchema#'
RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'


class BuildError(RuntimeError):
    pass


def run_script(script, *args, timeout=7200):
    """scripts/의 스크립트를 수정 없이 하위 프로세스로 실행한다. -> (종료 코드, 출력, 초)"""
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING='utf-8')        # 출력만 UTF-8로 받는다. 스크립트의 동작은 바꾸지 않는다
    r = subprocess.run([sys.executable, os.path.join(config.SCRIPTS, script), *args], cwd=config.ROOT, env=env,
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    return r.returncode, (r.stdout or '') + (r.stderr or ''), round(time.time() - t0, 1)


def kg_time(s):
    """xsd:dateTime 어휘형 또는 DB 시각 문자열 -> datetime. KG의 소수부는 자릿수가 줄어 있을 수 있다."""
    head, _, frac = s.replace(' ', 'T').partition('.')
    return datetime.strptime(head, '%Y-%m-%dT%H:%M:%S') + timedelta(microseconds=int((frac + '000000')[:6]))


def dir_bytes(path):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


# ---- 위반 그래프 -------------------------------------------------------------
def derive_violations(full, cut, t):
    """원본 KG(full)의 위반 사실에서 t 시점의 위반 사실을 만든다. cut: 스냅샷 KG 저장소(기본 그래프 조회용). -> (quad 목록, 집계)"""
    import pyoxigraph as ox
    G, DG = ox.NamedNode(VIOLATION_GRAPH), ox.DefaultGraph()
    P = lambda n: ox.NamedNode(FAB + n)
    boolean, double = ox.NamedNode(XSD + 'boolean'), ox.NamedNode(XSD + 'double')
    t_dt = kg_time(t)
    present = lambda node: next(iter(cut.quads_for_pattern(node, None, None, DG)), None) is not None
    by = defaultdict(lambda: defaultdict(list))
    for q in full.quads_for_pattern(None, None, None, G):
        by[q.subject][q.predicate.value.rsplit('#', 1)[-1]].append(q.object)
    stat, kept = Counter(source=len(by)), []
    for v, pr in by.items():
        kinds = sorted(o.value.rsplit('#', 1)[-1] for o in pr['type'] if not o.value.endswith('#ConstraintViolation'))
        if kinds != ['QTimeViolation'] or len(pr['deadline']) != 1:
            stat['not_qtime'] += 1          # t 시점 판정 규칙이 없는 위반 종류. 넣지 않고 집계에 남긴다(검증에서 실패로 본다)
            continue
        deadline = kg_time(pr['deadline'][0].value)
        if not deadline < t_dt:
            stat['deadline_not_before_t'] += 1
            continue
        value, focus, lot = pr['violationValue'][0], pr['violationFocus'][0], pr['violationLot'][0]
        if not present(value):
            entry_end, snap = kg_time(pr['entryEndTime'][0].value), None
            for q in cut.quads_for_pattern(None, P('openConstraint'), focus, DG):
                s = q.subject
                same_lot = next(iter(cut.quads_for_pattern(s, P('snapshotLot'), lot, DG)), None) is not None
                tout = next(iter(cut.quads_for_pattern(s, P('entryTrackOutTime'), None, DG)), None)
                if same_lot and tout is not None and kg_time(tout.object.value) == entry_end:
                    snap = s
            if snap is None:
                stat['entry_not_in_asof'] += 1
                continue
            value = snap
            stat['value_from_snapshot'] += 1
        exits = pr.get('exitExecution') or []
        closed = False
        if exits:
            tin = next(iter(full.quads_for_pattern(exits[0], P('trackInTime'), None, DG)), None)
            if tin is not None and kg_time(tin.object.value) <= t_dt:
                closed = present(exits[0])
                stat['exit_not_in_asof'] += not closed
            else:
                stat['exit_after_t'] += 1
        else:
            stat['waiting_in_source'] += 1
        trip = [(RDF_TYPE, o) for o in pr['type']]
        trip += [(FAB + k, o) for k in ('violatesShape', 'violationFocus', 'violationLot', 'violatedConstraint', 'entryEndTime', 'deadline')
                 for o in pr.get(k, [])]
        trip.append((FAB + 'violationValue', value))
        for m in pr.get('violationMessage', []):
            if build_db.later_than(m.value, t):
                stat['message_dropped'] += 1
            else:
                trip.append((FAB + 'violationMessage', m))
        if closed:
            trip += [(FAB + 'exitExecution', exits[0]), (FAB + 'stillWaiting', pr['stillWaiting'][0]), (FAB + 'overrunHours', pr['overrunHours'][0])]
        else:
            over = round((t_dt - deadline).total_seconds() / 3600, 4)
            trip += [(FAB + 'stillWaiting', ox.Literal('true', datatype=boolean)), (FAB + 'overrunHours', ox.Literal(repr(over), datatype=double))]
        stat['closed' if closed else 'still_waiting'] += 1
        kept.append((pr['violatesShape'][0].value, focus.value, value.value, kinds[0], trip))
    kept.sort(key=lambda r: r[:3])          # validate_kg.py와 같은 순서(shape, focus, value)
    quads, seq = [], Counter()
    for _, _, _, kind, trip in kept:
        seq[kind] += 1
        s = ox.NamedNode(f'{DATA}violation/{kind}/{seq[kind]:06d}')
        quads += [ox.Quad(s, ox.NamedNode(p), o, G) for p, o in trip]
    stat['kept'], stat['quads'] = len(kept), len(quads)
    return quads, dict(stat)


def load_violations(scenario, name, t):
    """원본 KG에서 유도한 위반 사실을 스냅샷 KG의 위반 그래프에 넣는다. 스냅샷 KG는 이 단계에서만 쓰기로 연다.

    build_kg.py가 방금 만든 저장소에 한 번만 실행한다. validate_kg.load_violations와 같이 fab.ttl을 기본 그래프에 다시 넣는다
    (원본 KG는 이 단계 때문에 fab.ttl의 빈 노드 구조가 한 벌 더 있다. 스키마 조회 결과가 원본 KG와 같게 맞춘다)."""
    import pyoxigraph as ox
    full = ox.Store.read_only(os.path.join(config.kg_dir(scenario), 'store'))
    cut = ox.Store(os.path.join(config.kg_dir(name), 'store'))
    try:
        quads, stat = derive_violations(full, cut, t)
        cut.update(f'DROP SILENT GRAPH <{VIOLATION_GRAPH}>')
        cut.bulk_load(os.path.join(config.ROOT, 'ontology', 'fab.ttl'), 'text/turtle')
        cut.extend(quads)
        cut.flush()
        stat['loaded'] = sum(1 for _ in cut.quads_for_pattern(None, None, None, ox.NamedNode(VIOLATION_GRAPH)))
        stat['default_graph_triples'] = len(cut) - stat['loaded']
    finally:
        del full, cut
        gc.collect()
    return stat


# ---- 실행 목록 ---------------------------------------------------------------
def manifest_path(name):
    return os.path.join(config.kg_dir(name), MANIFEST)


def read_manifest(name):
    try:
        with open(manifest_path(name), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def manifest_valid(name, variant=None):
    """실행 목록이 있고, 검증을 통과했고, 가리키는 파일이 그대로 있는지. -> (bool, 사유)"""
    m = read_manifest(name)
    if m is None:
        return False, '실행 목록 없음'
    if m.get('manifest_version') != MANIFEST_VERSION or m.get('rules_version') != build_db.RULES_VERSION:
        return False, '실행 목록 또는 스냅샷 생성 규칙의 판이 다름'
    if variant and m.get('variant') != variant:
        return False, f"판이 다름({m.get('variant')})"
    if not m.get('ok'):
        return False, '검증 실패 기록: ' + ', '.join(m.get('failures', []))
    for key, path in (('db', config.db_path(name)), ('db_views', config.db_path(name, 'views'))):
        if not os.path.exists(path) or build_db.file_sha256(path) != m[key]['sha256']:
            return False, f'{os.path.basename(path)} 없음 또는 해시 불일치'
    if not os.path.isdir(os.path.join(config.kg_dir(name), 'store')):
        return False, 'KG 저장소 없음'
    return True, ''


def build(scenario, t, db_info, validation=None, kg_checks=None, tasks=(), log=print, revalidate=False):
    """스냅샷 DB(이미 만들어져 있어야 한다)에서 KG, 조건 B DB, 위반 그래프를 만들고 실행 목록을 쓴다. -> 실행 목록 dict

    db_info: build_db.build의 반환 값. validation: 스냅샷 DB 단계의 검증 결과(validate.db_checks).
    kg_checks: 이름을 받아 (검증 결과 dict, 추가 점검 dict)를 돌려주는 함수. 정답 파일을 읽는 검증은 호출하는 쪽이 넘긴다.
    검증 결과는 ok 판정에 들어간다. 추가 점검의 실패는 warnings에 남기고 ok 판정에는 넣지 않는다.
    revalidate: KG를 다시 만들지 않는다. 이미 있는 스냅샷 KG에 검증만 다시 실행해 실행 목록을 갱신한다(조건 B DB는 다시 만든다)."""
    from . import validate
    name = asof_name(scenario, t)
    db, kg = config.db_path(name), config.kg_dir(name)
    if os.path.normcase(os.path.abspath(kg)) != os.path.normcase(os.path.join(config.ROOT, 'data', 'derived', 'kg', name)):
        raise BuildError(f'config.kg_dir({name})가 파이프라인 스크립트의 KG 위치(data/derived/kg/<DB 이름>)와 다르다')
    if build_db.file_sha256(db) != db_info['sha256']:
        raise BuildError(f'{db}의 해시가 스냅샷 DB 기록과 다르다')
    timing, t0 = {}, time.time()
    prev = read_manifest(name) if revalidate else None
    if revalidate and (prev is None or not os.path.isdir(os.path.join(kg, 'store')) or prev['db']['sha256'] != db_info['sha256']):
        raise BuildError('다시 검증할 스냅샷 KG가 없거나, 스냅샷 DB가 KG를 만든 때와 다르다')

    if not revalidate:
        log(f'{name}: build_kg.py 실행')
        code, text, timing['build_kg_s'] = run_script('build_kg.py', db, '--out', kg)
        if code != 0 or not os.path.isdir(os.path.join(kg, 'store')):
            raise BuildError('build_kg.py 실패: ' + text[-2000:])
    with open(os.path.join(kg, 'kg_stats.json'), encoding='utf-8') as f:
        stats = json.load(f)

    log(f'{name}: build_condition_b.py 실행')
    code_b, text_b, timing['condition_b_s'] = run_script('build_condition_b.py', db)
    db_b = config.db_path(name, 'views')
    if not os.path.exists(db_b):
        raise BuildError('build_condition_b.py 실패: ' + text_b[-2000:])
    views = {}
    for line in text_b.splitlines():
        p = line.replace(',', '').split()
        if len(p) >= 5 and p[0].startswith('v_') and p[2] == 'KG':
            views[p[0]] = dict(rows=int(p[1]), kg=int(p[3]), same=p[4] == 'OK')
    cond_b = dict(ok=code_b == 0 and len(views) == 6 and all(v['same'] for v in views.values()), returncode=code_b, views=views)

    if revalidate:      # 위반 그래프와 abox.nt의 기록은 만든 때의 것을 쓰고, 적재된 위반 quad 수만 다시 센다
        import pyoxigraph as ox
        store = ox.Store.read_only(os.path.join(kg, 'store'))
        loaded = sum(1 for _ in store.quads_for_pattern(None, None, None, ox.NamedNode(VIOLATION_GRAPH)))
        del store
        gc.collect()
        viol, abox_info = dict(prev['violations'], loaded=loaded), prev['kg']['abox_nt']
        timing = dict(prev['timings'], condition_b_s=timing['condition_b_s'])
    else:
        log(f'{name}: 위반 그래프 유도')
        t1 = time.time()
        viol = load_violations(scenario, name, t)
        timing['violations_s'] = round(time.time() - t1, 1)
        abox = os.path.join(kg, 'abox.nt')
        abox_info = dict(sha256=build_db.file_sha256(abox), bytes=os.path.getsize(abox), deleted=True)
        os.remove(abox)
    viol['ok'] = viol.get('not_qtime', 0) == 0 and viol.get('exit_not_in_asof', 0) == 0 and viol['loaded'] == viol['quads']

    log(f'{name}: 검증(KG 시각 상한, T-Box 대조, check_kg.py)')
    t1 = time.time()

    def timed(fn, *args):
        t2 = time.time()
        out = fn(*args)
        out['seconds'] = round(time.time() - t2, 1)
        return out

    checks = dict(validation or {})
    checks.pop('ok', None)
    checks['condition_b'] = cond_b
    checks['violations'] = dict(ok=viol['ok'])
    checks['kg_bounds'] = timed(validate.kg_bounds, kg, t)
    checks['tbox_parity'] = timed(validate.tbox_parity, config.kg_dir(scenario), kg)
    checks['views_db_bounds'] = timed(validate.db_bounds, db_b, t)
    checks['check_kg'] = validate.check_kg(db)
    timing['validation_s'] = round(time.time() - t1, 1)
    extra = {}
    if kg_checks:
        t1 = time.time()
        required, extra = kg_checks(name)
        checks.update(required)
        timing['task_checks_s'] = round(time.time() - t1, 1)
    timing['revalidate_s' if revalidate else 'total_s'] = round(time.time() - t0, 1)

    fail = validate.failures(checks)
    onto = os.path.join(config.ROOT, 'ontology')
    m = dict(manifest_version=MANIFEST_VERSION, rules_version=build_db.RULES_VERSION, name=name, scenario=scenario, as_of_ts=t,
             variant=db_info['variant'], tasks=[x.id for x in tasks],
             built_at=prev['built_at'] if revalidate else datetime.now().isoformat(timespec='seconds'),
             validated_at=datetime.now().isoformat(timespec='seconds'),
             ok=not fail, failures=fail, warnings=validate.failures(extra),
             source=dict(db=db_info['source_db'], db_sha256=db_info.get('source_sha256'),
                         kg=os.path.relpath(config.kg_dir(scenario), config.ROOT).replace('\\', '/'),
                         window=[db_info['window_start_ts'], db_info['source_window_end_ts']]),
             db=dict(path=db_info['db'], sha256=db_info['sha256'], bytes=db_info['bytes'], rows=db_info['rows'],
                     moved_to_wait_open=db_info['moved_to_wait_open'], scheduled_pm=db_info['scheduled_pm']),
             db_views=dict(path=os.path.relpath(db_b, config.ROOT).replace('\\', '/'), sha256=build_db.file_sha256(db_b),
                           bytes=os.path.getsize(db_b)),
             kg=dict(dir=os.path.relpath(kg, config.ROOT).replace('\\', '/'), store_bytes=dir_bytes(os.path.join(kg, 'store')),
                     triples_default_graph=viol['default_graph_triples'], abox_mapped=stats['abox_mapped'], instances=stats['instances'],
                     abox_nt=abox_info, tbox_closure_sha256=build_db.file_sha256(os.path.join(kg, 'tbox_closure.ttl')),
                     ontology_sha256={f: build_db.file_sha256(os.path.join(onto, f))
                                      for f in ('fab.ttl', 'dr_subset.ttl', 'fab_mapping.r2rml.ttl')}),
             violations=viol, validation=checks, extra_checks=extra, timings=timing)
    with open(manifest_path(name), 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    return m
