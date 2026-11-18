"""SHACL 데이터 제약(ontology/shapes_data.ttl)으로 KG를 검증하고 위반을 fab:ConstraintViolation 사실로 KG에 적재한다.

검증 엔진은 pySHACL(추론 없음)이다. KG 전체(약 380만 트리플)를 한 그래프로 검증하면 한 시간이 지나도 끝나지 않아
shape이 노드를 잇는 범위에 맞춰 그래프를 나눈다.
  lot 분할   같은 라우트의 lot 묶음. 그 lot의 실행·dedication·Q-time 스냅샷·웨이퍼 + 그 라우트의 Q-time 제약과 LTL 규칙.
             shape: QTime, LtlChain, LtlRecord, StepExecution, Lot, Wafer
  장비 분할  장비 묶음. 그 장비의 상태 구간·정지 구간·그 장비에서 처리한 실행.
             shape: ToolQualification, ToolAvailability, StateIntegrity, StateInterval
분할이 결과를 바꾸지 않는 이유: lot 분할 shape의 질의는 한 lot의 노드와 그 lot 라우트의 제약만 잇는다. 장비 분할 shape의
질의는 한 장비의 노드와 step의 toolgroup(전 분할 공통)만 잇는다. 그래서 분할별 위반의 합집합 = 전체 그래프의 위반이다.
--selftest 로 작은 부분 그래프에서 일괄 실행과 분할 실행의 결과가 같은지 확인한다.
라우트로 묶는 이유: Q-time 제약 372개를 모든 분할에서 focus로 돌리면 분할당 45초가 든다. 라우트별로는 평균 29개다.

메모리: 주 프로세스는 분할 목록만 들고, 작업 프로세스가 KG를 읽기 전용으로 열어 자기 분할만 읽는다.
재개: 분할 결과를 data/derived/kg/<이름>/shacl_parts/ 에 남긴다. 중단 후 다시 실행하면 끝난 분할은 건너뛴다.
      shape 파일이나 분할 구성이 바뀌면 키가 달라져 다시 계산한다.

결과는 named graph <https://w3id.org/ontosemc/graph/violations>에 적재한다. 기본 그래프에는 넣지 않는다.
조건 C(KG, 검증 없음)는 기본 그래프만 보고, 조건 D의 get_violations 도구만 이 그래프를 본다(기획서 7.1).
Q-time 위반은 진입 종료 시각, 기한, 첫 종료 step 실행, 초과 시간, 기록 종료 시 대기 중 여부를 함께 남긴다.

예) python scripts/validate_kg.py data/derived/db/lvhm_e_cr_s0.sqlite --workers 2
    python scripts/validate_kg.py data/derived/db/lvhm_e_cr_s0.sqlite --selftest
SQL 참조와의 일치는 scripts/check_kg.py가 확인한다
"""
import argparse
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime

import pyoxigraph as ox
import rdflib
from pyshacl import validate
from rdflib.namespace import RDF, XSD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAB = rdflib.Namespace('https://w3id.org/ontosemc/fab#')
SH = rdflib.Namespace('http://www.w3.org/ns/shacl#')
SHP = 'https://w3id.org/ontosemc/shapes#'
D = 'https://w3id.org/ontosemc/data/'
SHAPES = os.path.join(ROOT, 'ontology', 'shapes_data.ttl')
TBOX = os.path.join(ROOT, 'ontology', 'fab.ttl')
VIOL_GRAPH = ox.NamedNode('https://w3id.org/ontosemc/graph/violations')
DG = ox.DefaultGraph()
TYPE = ox.NamedNode(str(RDF.type))
LOTS_PER_PART, TOOLS_PER_PART = 100, 100
GROUPS = {'lot': {'QTimeShape', 'LtlChainShape', 'LtlRecordShape', 'StepExecutionShape', 'LotShape', 'WaferShape'},
          'tool': {'ToolQualificationShape', 'ToolAvailabilityShape', 'StateIntegrityShape', 'StateIntervalShape'}}
KIND = {'QTimeShape': 'QTimeViolation', 'LtlChainShape': 'LtlViolation', 'LtlRecordShape': 'LtlViolation',
        'ToolQualificationShape': 'QualificationViolation', 'ToolAvailabilityShape': 'AvailabilityViolation'}


def P(*names):
    return {str(FAB[n]) for n in names} | {str(RDF.type)}


EXEC_P = P('ofLot', 'atStep', 'processedOn', 'waitedAt', 'trackInTime', 'trackOutTime')
LOT_P = P('identifier', 'followsRoute', 'waferQty')
DED_P = P('dedicatedLot', 'dedicatedStep', 'dedicatedTool')
SNAP_P = P('snapshotLot', 'openConstraint', 'entryTrackOutTime')
WAFER_P = P('waferOf', 'exhibits')
QT_P = P('entryStep', 'exitStep', 'maxHours')
TOOL_P = P('memberOf')
DOWN_P = P('onTool', 'startTime', 'endTime')
STATE_P = P('stateOf', 'startTime', 'endTime', 'e10State', 'stateReason')
TEXEC_P = {str(FAB[n]) for n in ('processedOn', 'atStep', 'trackInTime')}


def n(name):
    return ox.NamedNode(str(FAB[name]))


def to_rdflib(t):
    if isinstance(t, ox.NamedNode):
        return rdflib.URIRef(t.value)
    if isinstance(t, ox.BlankNode):
        return rdflib.BNode(t.value)
    if t.language:
        return rdflib.Literal(t.value, lang=t.language)
    return rdflib.Literal(t.value, datatype=rdflib.URIRef(t.datatype.value))


def to_ox(t):
    if isinstance(t, rdflib.URIRef):
        return ox.NamedNode(str(t))
    if isinstance(t, rdflib.BNode):
        return ox.BlankNode(str(t))
    return ox.Literal(str(t), datatype=ox.NamedNode(str(t.datatype))) if t.datatype else ox.Literal(str(t))


# ---- KG에서 분할 읽기 ----------------------------------------------------
def node(store, s, preds):
    return [(to_rdflib(q.subject), to_rdflib(q.predicate), to_rdflib(q.object))
            for q in store.quads_for_pattern(s, None, None, DG) if q.predicate.value in preds]


def subjects(store, pred, obj):
    return [q.subject for q in store.quads_for_pattern(None, n(pred), obj, DG)]


def common_triples(store):
    """전 분할 공통: step의 타입과 toolgroup, 장비 타입(sh:class 검사용), 기록 종료 시각."""
    out = []
    for q in store.quads_for_pattern(None, TYPE, n('ProcessStep'), DG):
        out.append((to_rdflib(q.subject), RDF.type, FAB.ProcessStep))
    for q in store.quads_for_pattern(None, n('requiresToolGroup'), None, DG):
        out.append((to_rdflib(q.subject), FAB.requiresToolGroup, to_rdflib(q.object)))
    for q in store.quads_for_pattern(None, n('windowEnd'), None, DG):
        out.append((to_rdflib(q.subject), FAB.windowEnd, to_rdflib(q.object)))
    return out


def route_triples(store, route):
    """라우트의 Q-time 제약과 LTL 규칙(전이 폐포 포함)."""
    out = []
    for st in subjects(store, 'onRoute', ox.NamedNode(route)):
        out += [(to_rdflib(q.subject), FAB.ltlDedicatedTo, to_rdflib(q.object))
                for q in store.quads_for_pattern(st, n('ltlDedicatedTo'), None, DG)]
        for c in subjects(store, 'entryStep', st):
            out += node(store, c, QT_P)
    return out


def lot_triples(store, lot):
    L = ox.NamedNode(lot)
    out = node(store, L, LOT_P)
    tools = set()
    for x in subjects(store, 'ofLot', L):
        tr = node(store, x, EXEC_P)
        out += tr
        tools |= {o for _, p, o in tr if p == FAB.processedOn}
    for d in subjects(store, 'dedicatedLot', L):
        tr = node(store, d, DED_P)
        out += tr
        tools |= {o for _, p, o in tr if p == FAB.dedicatedTool}
    for s in subjects(store, 'snapshotLot', L):
        if s.value.startswith(D + 'qtsnap/'):  # WIP 스냅샷도 snapshotLot을 쓴다
            out += node(store, s, SNAP_P)
    for w in subjects(store, 'waferOf', L):
        out += node(store, w, WAFER_P)
    out += [(t, RDF.type, FAB.Tool) for t in tools  # sh:class fab:Tool 검사용. KG에 타입이 있을 때만
            if next(iter(store.quads_for_pattern(to_ox(t), TYPE, n('Tool'), DG)), None) is not None]
    return out


def tool_triples(store, tool):
    T = ox.NamedNode(tool)
    out = node(store, T, TOOL_P)
    for d in subjects(store, 'onTool', T):
        out += node(store, d, DOWN_P)
    for i in subjects(store, 'stateOf', T):
        out += node(store, i, STATE_P)
    for x in subjects(store, 'processedOn', T):
        out += node(store, x, TEXEC_P)
    return out


def build_graph(store, base, common, job):
    g = rdflib.Graph()
    for t in base:
        g.add(t)
    for t in common:
        g.add(t)
    if job['kind'] == 'lot':
        for t in route_triples(store, job['route']):
            g.add(t)
        for lot in job['keys']:
            for t in lot_triples(store, lot):
                g.add(t)
    else:
        for tool in job['keys']:
            for t in tool_triples(store, tool):
                g.add(t)
    return g


# ---- 검증 ----------------------------------------------------------------
def dt(lit):
    # KG의 xsd:dateTime은 소수부 자릿수가 줄어 있을 수 있다(예: .85). Python 3.10 fromisoformat은 3·6자리만 받는다
    s = str(lit)
    if '.' in s:
        head, frac = s.split('.')
        s = f'{head}.{(frac + "000000")[:6]}'
    return datetime.fromisoformat(s)


def qtime_details(g, focus, value):
    """Q-time 위반 1건의 lot, 진입 종료 시각, 기한, 첫 종료 실행(없으면 None)."""
    lot = g.value(value, FAB.ofLot) or g.value(value, FAB.snapshotLot)
    tout = dt(g.value(value, FAB.trackOutTime) or g.value(value, FAB.entryTrackOutTime))
    xs, mx = g.value(focus, FAB.exitStep), float(g.value(focus, FAB.maxHours))
    deadline = tout.timestamp() + mx * 3600
    exits = sorted((dt(g.value(x, FAB.trackInTime)).timestamp(), str(x)) for x in g.subjects(FAB.atStep, xs)
                   if g.value(x, FAB.ofLot) == lot and dt(g.value(x, FAB.trackInTime)) > tout)
    return dict(lot=str(lot), tout=tout.isoformat(timespec='milliseconds'), deadline=deadline,
                first=exits[0] if exits else None)


def shape_name(shapes, shape):
    if isinstance(shape, rdflib.BNode):  # 속성 shape: 부모 노드 shape 이름
        parent = shapes.value(None, SH.property, shape)
        if parent is None:  # sh:xone 목록 안의 shape
            return 'StepExecutionShape'
        shape = parent
    return str(shape).split('#')[-1]


def subset(shapes, names):
    g = rdflib.Graph()
    g += shapes
    for s in set(shapes.subjects(RDF.type, SH.NodeShape)):
        if names is not None and str(s).split('#')[-1] not in names:
            g.remove((s, None, None))
    return g


def run_shacl(g, shapes, shape_graph):
    """위반 목록(dict). Q-time 위반은 상세를 붙인다."""
    _, report, _ = validate(g, shacl_graph=shape_graph, inference='none', inplace=True, allow_warnings=True)
    out = []
    for r in report.subjects(RDF.type, SH.ValidationResult):
        sname = shape_name(shapes, report.value(r, SH.sourceShape))
        focus, value = report.value(r, SH.focusNode), report.value(r, SH.value)
        msg = report.value(r, SH.resultMessage)
        det = qtime_details(g, focus, value) if sname == 'QTimeShape' else None
        lot = det['lot'] if det else (g.value(value, FAB.ofLot) if value is not None else None)
        out.append(dict(shape=sname, focus=str(focus), value=None if value is None else str(value),
                        msg=None if msg is None else str(msg), lot=None if lot is None else str(lot), det=det))
    return out


# ---- 작업 프로세스 ------------------------------------------------------
_W = {}


def _init(store_path):
    store = ox.Store.read_only(store_path)
    shapes = rdflib.Graph().parse(SHAPES)
    _W.update(store=store, shapes=shapes, base=list(rdflib.Graph().parse(TBOX)), common=common_triples(store),
              lot=subset(shapes, GROUPS['lot']), tool=subset(shapes, GROUPS['tool']))


def _peak_mb():
    """작업 프로세스의 최대 메모리(MB). 프로세스 시작 이후 누적 최댓값. psutil이 없으면 None."""
    try:
        import psutil
    except ImportError:
        return None
    m = psutil.Process().memory_info()
    return round(getattr(m, 'peak_wset', m.rss) / 2 ** 20)


def _work(job):
    t0 = time.time()
    g = build_graph(_W['store'], _W['base'], _W['common'], job)
    res = run_shacl(g, _W['shapes'], _W[job['kind']])
    out = dict(key=job['key'], kind=job['kind'], route=job.get('route'), n_keys=len(job['keys']), triples=len(g),
               seconds=round(time.time() - t0, 1), peak_mb=_peak_mb(), results=res)
    with open(job['path'] + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(job['path'] + '.tmp', job['path'])
    return job['key'], len(res), out['seconds'], job['kind'], len(g), out['peak_mb']


# ---- 분할 계획 ----------------------------------------------------------
def plan(store, parts_dir, lots_per=LOTS_PER_PART, tools_per=TOOLS_PER_PART):
    by_route = defaultdict(list)
    for q in store.quads_for_pattern(None, n('followsRoute'), None, DG):
        by_route[q.object.value].append(q.subject.value)
    tools = sorted(q.subject.value for q in store.quads_for_pattern(None, TYPE, n('Tool'), DG))
    sig = hashlib.sha1(open(SHAPES, 'rb').read() + open(TBOX, 'rb').read()).hexdigest()[:8]
    jobs = []
    for route in sorted(by_route):
        lots = sorted(by_route[route])
        jobs += [dict(kind='lot', route=route, keys=lots[i:i + lots_per]) for i in range(0, len(lots), lots_per)]
    jobs += [dict(kind='tool', keys=tools[i:i + tools_per]) for i in range(0, len(tools), tools_per)]
    for j in jobs:
        j['key'] = hashlib.sha1((sig + j['kind'] + (j.get('route') or '') + '|'.join(j['keys'])).encode()).hexdigest()[:12]
        j['path'] = os.path.join(parts_dir, f"{j['kind']}_{j['key']}.json")
    return jobs


def load_violations(store, parts, wend):
    """분할 결과를 위반 그래프로 적재. 이전 위반 그래프는 지우고 fab.ttl(위반 어휘 포함)을 기본 그래프에 다시 넣는다."""
    found = sorted((r for p in parts for r in p['results']), key=lambda r: (r['shape'], r['focus'], str(r['value'])))
    store.update(f'DROP SILENT GRAPH <{VIOL_GRAPH.value}>')
    store.bulk_load(TBOX, 'text/turtle')
    seq, out, by_shape = Counter(), [], Counter()
    for r in found:
        vkind = KIND.get(r['shape'], 'IntegrityViolation')
        by_shape[r['shape']] += 1
        seq[vkind] += 1
        v = rdflib.URIRef(f'{D}violation/{vkind}/{seq[vkind]:06d}')
        trip = [(v, RDF.type, FAB[vkind]), (v, RDF.type, FAB.ConstraintViolation),
                (v, FAB.violatesShape, rdflib.URIRef(SHP + r['shape'])), (v, FAB.violationFocus, rdflib.URIRef(r['focus']))]
        if r['value'] is not None and r['value'].startswith('http'):
            trip.append((v, FAB.violationValue, rdflib.URIRef(r['value'])))
        if r['msg']:
            trip.append((v, FAB.violationMessage, rdflib.Literal(r['msg'])))
        if r['lot']:
            trip.append((v, FAB.violationLot, rdflib.URIRef(r['lot'])))
        det = r['det']
        if det:
            late = det['first'][0] if det['first'] else wend
            trip += [(v, FAB.violatedConstraint, rdflib.URIRef(r['focus'])),
                     (v, FAB.entryEndTime, rdflib.Literal(det['tout'], datatype=XSD.dateTime)),
                     (v, FAB.deadline, rdflib.Literal(datetime.fromtimestamp(det['deadline']).isoformat(timespec='milliseconds'),
                                                      datatype=XSD.dateTime)),
                     (v, FAB.stillWaiting, rdflib.Literal(det['first'] is None)),
                     (v, FAB.overrunHours, rdflib.Literal(round((late - det['deadline']) / 3600, 4), datatype=XSD.double))]
            if det['first']:
                trip.append((v, FAB.exitExecution, rdflib.URIRef(det['first'][1])))
        out += [ox.Quad(to_ox(s), to_ox(p), to_ox(o), VIOL_GRAPH) for s, p, o in trip]
    store.extend(out)
    store.flush()
    return len(found), len(out), by_shape


# ---- 자체 점검: 일괄 실행 대 분할 실행 -----------------------------------
def selftest(store_path, lots_per_route=12, n_routes=3, n_tools=40):
    """작은 부분 KG를 메모리 저장소로 만들고, 전 shape 일괄 실행과 분할 실행의 위반 집합을 비교한다.
    부분 KG = 라우트 n_routes개에서 lot을 lots_per_route개씩 + 그 lot이 쓴 장비 중 n_tools대의 상태·정지 구간 + 마스터."""
    src = ox.Store.read_only(store_path)
    done = [q.subject.value for q in src.quads_for_pattern(None, n('completeTime'), None, DG)]
    by_route = defaultdict(list)
    for lot in sorted(done):
        r = next(iter(src.quads_for_pattern(ox.NamedNode(lot), n('followsRoute'), None, DG))).object.value
        by_route[r].append(lot)
    routes = sorted(by_route, key=lambda r: -len(by_route[r]))[:n_routes]
    lots = [l for r in routes for l in by_route[r][:lots_per_route]]
    mem = ox.Store()
    quads = []
    add = lambda trips: quads.extend(ox.Quad(to_ox(s), to_ox(p), to_ox(o)) for s, p, o in trips)
    add(common_triples(src))
    for r in routes:
        add(route_triples(src, r))
        for st in subjects(src, 'onRoute', ox.NamedNode(r)):
            add([(to_rdflib(st), FAB.onRoute, rdflib.URIRef(r))])
    used = set()
    for lot in lots:
        tr = lot_triples(src, lot)
        add(tr)
        used |= {str(o) for _, p, o in tr if p == FAB.processedOn}
    for tool in sorted(used)[:n_tools]:
        T = ox.NamedNode(tool)
        add(node(src, T, TOOL_P))
        for d in subjects(src, 'onTool', T):
            add(node(src, d, DOWN_P))
        for i in subjects(src, 'stateOf', T):
            add(node(src, i, STATE_P))
    for tool in sorted(used)[n_tools:]:
        add(node(src, ox.NamedNode(tool), TOOL_P))
    mem.extend(quads)
    del src
    base, shapes = list(rdflib.Graph().parse(TBOX)), rdflib.Graph().parse(SHAPES)
    key = lambda r: (r['shape'], r['focus'], r['value'])
    # 일괄: 부분 KG 전체 + 전 shape
    g = rdflib.Graph()
    for t in base:
        g.add(t)
    for q in mem.quads_for_pattern(None, None, None, DG):
        g.add((to_rdflib(q.subject), to_rdflib(q.predicate), to_rdflib(q.object)))
    t0 = time.time()
    ref = Counter(key(r) for r in run_shacl(g, shapes, shapes))
    t1 = time.time()
    # 분할: 같은 부분 KG를 본 실행과 같은 코드로 나눠 실행
    jobs = plan(mem, '', lots_per=5, tools_per=25)
    common, ours = common_triples(mem), Counter()
    for j in jobs:
        ours.update(key(r) for r in run_shacl(build_graph(mem, base, common, j), shapes, subset(shapes, GROUPS[j['kind']])))
    t2 = time.time()
    by = Counter(k[0] for k in ref.elements())
    print(f'자체 점검: 부분 KG {len(g):,} 트리플 (lot {len(lots)}개, 라우트 {len(routes)}개, 상태 구간 장비 {min(n_tools, len(used))}대)')
    print(f'  일괄 실행 {sum(ref.values())}건 {t1 - t0:.0f}초 / 분할 실행({len(jobs)}개 분할) {sum(ours.values())}건 {t2 - t1:.0f}초')
    print(f'  shape별 위반(일괄): {dict(by)}')
    print(f'  일치: {ref == ours}')
    for k in list((ref - ours).elements())[:5]:
        print('  일괄에만:', k)
    for k in list((ours - ref).elements())[:5]:
        print('  분할에만:', k)
    return ref == ours


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db', help='KG 이름을 정하는 원천 DB 경로 (data/derived/kg/<DB 이름>)')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--selftest', action='store_true', help='일괄 실행과 분할 실행의 결과 비교만 하고 끝낸다')
    ap.add_argument('--max-parts', type=int, help='이번 실행에서 처리할 분할 수 상한(시간 측정·나눠 돌리기용)')
    a = ap.parse_args()
    name = os.path.splitext(os.path.basename(a.db))[0]
    kg_dir = os.path.join(ROOT, 'data', 'derived', 'kg', name)
    store_path = os.path.join(kg_dir, 'store')
    if a.selftest:
        raise SystemExit(0 if selftest(store_path) else 1)
    parts_dir = os.path.join(kg_dir, 'shacl_parts')
    os.makedirs(parts_dir, exist_ok=True)
    store = ox.Store.read_only(store_path)
    jobs = plan(store, parts_dir)
    wend = dt(next(iter(store.quads_for_pattern(None, n('windowEnd'), None, DG))).object.value).timestamp()
    del store
    todo = [j for j in jobs if not os.path.exists(j['path'])]
    print(f'분할 {len(jobs)}개 (lot {sum(j["kind"] == "lot" for j in jobs)}, 장비 {sum(j["kind"] == "tool" for j in jobs)}), '
          f'완료 {len(jobs) - len(todo)}, 남음 {len(todo)}', flush=True)
    if a.max_parts:
        todo = todo[:a.max_parts]
    t0, n_done, pending = time.time(), 0, set()
    if todo:
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init, initargs=(store_path,)) as ex:
            def drain():
                nonlocal pending, n_done
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    key, nres, sec, kind, ntrip, peak = f.result()
                    n_done += 1
                    print(f'  분할 {n_done}/{len(todo)} 완료 ({kind}, {ntrip:,} 트리플, {sec:.0f}초, 위반 {nres}, '
                          f'최대 메모리 {peak} MB), 경과 {time.time() - t0:.0f}초', flush=True)
            for j in todo:
                pending.add(ex.submit(_work, j))
                if len(pending) >= a.workers:
                    drain()
            while pending:
                drain()
    missing = [j for j in jobs if not os.path.exists(j['path'])]
    if missing:
        print(f'남은 분할 {len(missing)}개. 같은 명령을 다시 실행하면 이어서 한다. 위반 그래프는 아직 쓰지 않았다')
        return
    parts = [json.load(open(j['path'], encoding='utf-8')) for j in jobs]
    store = ox.Store(store_path)
    n_viol, n_trip, by_shape = load_violations(store, parts, wend)
    del store
    cpu = sum(p['seconds'] for p in parts)
    peaks = [p['peak_mb'] for p in parts if p.get('peak_mb')]
    lines = [f'SHACL 데이터 제약 검증 (pySHACL, 분할 {len(jobs)}개): {os.path.relpath(kg_dir, ROOT)}',
             f'작업 프로세스 최대 메모리 {max(peaks) if peaks else "미측정"} MB',
             f'분할 계산 시간 합계 {cpu / 60:.1f}분 (lot 분할 {sum(p["seconds"] for p in parts if p["kind"] == "lot") / 60:.1f}분, '
             f'장비 분할 {sum(p["seconds"] for p in parts if p["kind"] == "tool") / 60:.1f}분), '
             f'분할 그래프 최대 {max(p["triples"] for p in parts):,} 트리플',
             f'위반 {n_viol:,}건, 적재 {n_trip:,} 트리플', '']
    lines += [f'  {s:28s} {c:>7,d}' for s, c in sorted(by_shape.items())]
    if not by_shape:
        lines.append('  (위반 없음)')
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(kg_dir, 'shacl_report.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
