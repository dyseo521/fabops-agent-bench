"""6주차 go/no-go: 오라클 조치 집합이 SHACL 게이트를 전부 통과하는가. 함께 함정 조치를 게이트가 반려하는지 본다.

  정답 조치  오라클 alternatives의 모든 조치 + allowed 표본. 전부 통과해야 한다 (기획서 v3 9장 6주차 기준)
  함정 조치  정책을 어기는 조치를 시나리오 상태에서 만든다. 전부 반려되어야 한다 (게이트 감도)
    P1   대기 lot을 정지 중인 인시던트 장비로 재배정 (고장)
    P2   대기 lot을 다른 toolgroup의 가동 장비로 재배정
    P3   전용 step에서 대기 중인 LTL 전용 lot을 같은 toolgroup의 다른 가동 장비로 재배정
    P4   Q-time 진입 step이 아닌 곳에서 대기 중인 lot hold (불필요한 hold)
    P5a  열린 Q-time 구간 밖이고 dedication도 없는 lot의 우선순위 상향 (근거 없는 상향)
    P5b  열린 Q-time 구간 밖의 lot 처분 검토 표시
    P7   PM 연기 48시간 요청 (허용 창 24시간 초과)
    REP  가동 중인 장비 수리 escalate (PM 도래 시나리오)
템플릿마다 시나리오를 표본 추출한다(시드 고정).

시나리오 1건이 끝날 때마다 결과를 scenarios/<이름>_gate_check_parts_<서명>.jsonl에 덧붙인다. 중단되면 같은 명령으로
이어서 실행된다. 서명은 표본 옵션, 오라클 출력, 게이트 코드·shape에서 만든다(어느 하나가 바뀌면 처음부터 다시 한다).

예) python scripts/check_gate_oracle.py data/derived/db/lvhm_e_cr_s1.sqlite --per-template 12
    python scripts/check_gate_oracle.py data/derived/db/lvhm_e_cr_s1.sqlite --per-template 100000   # 전수
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict

from asof_state import FabState
from gate import Gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = ('PRD', 'SBY')
CHECK_VERSION = 2  # 함정 조치나 표본 규칙을 바꾸면 올린다(체크포인트 서명에 들어간다). 2: P5a 함정 추가, 시나리오별 난수


def traps(fs, sc):
    t, tool, group = sc['time'], sc['eqp_id'], sc['toolgroup']
    w, st, ded = fs.wip(t), fs.tools(t), fs.dedications(t)
    out = []
    dlots = set(ded[ded.eqp_id == tool].lot_id)
    queued = sorted(set(w[(w.state == 'WAIT') & (w.toolgroup == group)].lot_id) - dlots)
    if queued and sc['down_type'] == 'BREAKDOWN':
        out.append(('P1', dict(type='ReassignLot', lot=queued[0], to_tool=tool)))
    other = st[(st.toolgroup != group) & st.state.isin(UP)].eqp_id.tolist()
    if queued and other:
        out.append(('P2', dict(type='ReassignLot', lot=queued[0], to_tool=sorted(other)[0])))
    peers = sorted(st[(st.toolgroup == group) & (st.eqp_id != tool) & st.state.isin(UP)].eqp_id)
    on_ded = w[w.state == 'WAIT'].merge(ded[ded.eqp_id == tool], left_on=['lot_id', 'step_seq'],
                                        right_on=['lot_id', 'target_step_seq'])
    if len(on_ded) and peers:
        out.append(('P3', dict(type='ReassignLot', lot=on_ded.lot_id.iloc[0], to_tool=peers[0])))
    entries = set(zip(fs.qdefs.route_id, fs.qdefs.entry_step_seq))
    plain = sorted(l for l, r, s in zip(w.lot_id, w.route_id, w.step_seq)
                   if (r, s) not in entries and l in set(w[w.state == 'WAIT'].lot_id))
    if plain:
        out.append(('P4', dict(type='HoldLot', lot=plain[0])))
    no_q = sorted(set(w.lot_id) - set(fs.qtime_open(t).lot_id))
    if no_q:
        out.append(('P5b', dict(type='FlagDisposition', lot=no_q[0])))
    # 열린 Q-time 구간도 dedication도 없고 우선순위가 hot 미만인 lot (우선순위 조건이 아니라 전제 조건으로 반려되는지 본다)
    plain_q = sorted((set(no_q) - set(ded.lot_id)) & set(w[w.priority < 20].lot_id))
    if plain_q:
        out.append(('P5a', dict(type='Reprioritize', lot=plain_q[0], priority=20)))
    if sc['down_type'] == 'PM':
        out.append(('P7', dict(type='Escalate', to='MAINTENANCE', request='PM_DEFER', tool=tool, defer_hours=48.0)))
        broken = ((fs.down.eqp_id == tool) & (fs.down.down_type == 'BREAKDOWN') & (fs.down.start_ts <= t)
                  & (fs.down.end_ts > t)).any()
        if not broken:  # PM 예정 장비가 t에 고장 중이면 수리 escalate는 정당하다
            out.append(('REP', dict(type='Escalate', to='MAINTENANCE', request='REPAIR', tool=tool)))
    return out


def key(a):
    return json.dumps({k: v for k, v in a.items() if k != 'lots'}, sort_keys=True)


def _mem_mb(peak=False):
    """이 프로세스의 메모리(MB). psutil이 없으면 None."""
    try:
        import psutil
    except ImportError:
        return None
    m = psutil.Process().memory_info()
    return round((getattr(m, 'peak_wset', m.rss) if peak else m.rss) / 2 ** 20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--per-template', type=int, default=12)
    ap.add_argument('--allowed-sample', type=int, default=5)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--tag', default='', help='출력 파일 이름 뒤에 붙인다(예: _smoke). 기존 결과를 덮어쓰지 않을 때')
    ap.add_argument('--max-new', type=int, help='이번 실행에서 새로 처리할 시나리오 수 상한. 남으면 종료 코드 3으로 끝난다. '
                                                '프로세스를 주기적으로 새로 띄워 메모리 사용을 묶을 때 쓴다')
    a = ap.parse_args()
    db_path = a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db)
    name = os.path.splitext(os.path.basename(db_path))[0]
    sdir = os.path.join(ROOT, 'data', 'derived', 'scenarios')
    oracle_path = os.path.join(sdir, name + '_oracle.json')
    scs = {s['id']: s for s in json.load(open(os.path.join(sdir, name + '.json'), encoding='utf-8'))['scenarios']}
    orc = {r['id']: r for r in json.load(open(oracle_path, encoding='utf-8'))['results']}
    rng = random.Random(a.seed)
    by_tpl = defaultdict(list)
    for sid in sorted(orc):
        by_tpl[scs[sid]['template']].append(sid)
    sample = [sid for tpl in sorted(by_tpl) for sid in sorted(rng.sample(by_tpl[tpl], min(a.per_template, len(by_tpl[tpl]))))]

    # 체크포인트. 서명이 같은 이전 실행의 결과는 다시 계산하지 않는다
    # 이 파일 전체를 서명에 넣으면 로그 문구만 바꿔도 체크포인트가 무효가 된다. 함정·표본 규칙을 바꾸면 CHECK_VERSION을 올린다
    h = hashlib.sha1(json.dumps([name, a.per_template, a.allowed_sample, a.seed, CHECK_VERSION]).encode())
    for p in (oracle_path, os.path.join(ROOT, 'ontology', 'shapes_gate.ttl'), os.path.join(ROOT, 'scripts', 'gate.py')):
        h.update(open(p, 'rb').read())
    part_path = os.path.join(sdir, f'{name}_gate_check_parts_{h.hexdigest()[:10]}.jsonl')
    done = {}
    if os.path.exists(part_path):
        for line in open(part_path, encoding='utf-8'):
            try:
                rec = json.loads(line)
                done[rec['sid']] = rec
            except ValueError:  # 중단으로 잘린 마지막 줄
                pass
    todo = [sid for sid in sample if sid not in done]
    print(f'시나리오 {len(sample)}개, 완료 {len(sample) - len(todo)}, 남음 {len(todo)}', flush=True)
    left = 0
    if a.max_new is not None and len(todo) > a.max_new:
        left = len(todo) - a.max_new
        todo = todo[:a.max_new]

    t0 = time.time()
    if todo:
        fs = FabState(db_path)
        gate = Gate(os.path.join(ROOT, 'data', 'derived', 'kg', name))
        with open(part_path, 'a', encoding='utf-8') as part:
            for i, sid in enumerate(todo):
                sc, o = scs[sid], orc[sid]
                exp = {key(x): x for alt in o['alternatives'] for x in alt}
                srng = random.Random(f'{a.seed}:{sid}')  # 시나리오별 난수. 이어서 실행해도 같은 허용 조치 표본이 나온다
                allowed = srng.sample(o['allowed'], min(a.allowed_sample, len(o['allowed'])))
                good = list(exp.values()) + [x for x in allowed if key(x) not in exp]
                bad = traps(fs, sc)
                res = gate.check(sc, good + [b for _, b in bad])
                rec = dict(sid=sid, template=sc['template'], good=[bool(r) for r in res[:len(good)]],
                           bad=[[tag, bool(r)] for (tag, _), r in zip(bad, res[len(good):])],
                           timing=dict(gate.last_timing) if gate.last_timing else None, failures=[])
                for x, r in zip(good, res[:len(good)]):
                    if r:
                        rec['failures'].append(dict(scenario=sid, kind='expected_or_allowed_rejected', action=x, reasons=r))
                for (tag, x), r in zip(bad, res[len(good):]):
                    if not r:
                        rec['failures'].append(dict(scenario=sid, kind=f'trap_{tag}_passed', action=x))
                part.write(json.dumps(rec, ensure_ascii=False) + '\n')
                part.flush()
                done[sid] = rec
                if (i + 1) % 10 == 0:
                    print(f'  {i + 1}/{len(todo)} 시나리오, {time.time() - t0:.0f}초, 메모리 {_mem_mb()} MB', flush=True)
    if left:
        print(f'남은 시나리오 {left}개. 같은 명령을 다시 실행하면 이어서 한다. 최대 메모리 {_mem_mb(peak=True)} MB', flush=True)
        sys.exit(3)

    pos, neg = Counter(), Counter()
    failures, lat = [], []
    for sid in sample:
        rec = done[sid]
        for rejected in rec['good']:
            pos[(rec['template'], 'reject' if rejected else 'pass')] += 1
        for tag, rejected in rec['bad']:
            neg[(tag, 'reject' if rejected else 'pass')] += 1
        failures += rec['failures']
        if rec['timing']:
            lat.append(rec['timing'])

    lines = [f'게이트-오라클 점검: {name}, 시나리오 {len(sample)}개 (템플릿당 최대 {a.per_template})', '',
             '정답·허용 조치 (전부 통과해야 함)']
    for tpl in sorted(by_tpl):
        p, r = pos[(tpl, 'pass')], pos[(tpl, 'reject')]
        lines.append(f'  {tpl:12s} 통과 {p:4d}  반려 {r:3d}')
    lines += ['', '함정 조치 (전부 반려되어야 함)']
    for tag in ['P1', 'P2', 'P3', 'P4', 'P5a', 'P5b', 'P7', 'REP']:
        lines.append(f"  {tag:4s} 반려 {neg[(tag, 'reject')]:4d}  통과 {neg[(tag, 'pass')]:3d}")
    if lat:  # 게이트 지연: 문맥 추출(KG 조회) + SHACL 검증. 묶음 1회 = 시나리오 1건의 조치 전체
        tot = sorted(x['context_s'] + x['shacl_s'] for x in lat)
        per = sorted((x['context_s'] + x['shacl_s']) / x['actions'] for x in lat)
        q = lambda v, f: v[min(len(v) - 1, int(f * len(v)))]
        lines += ['', f'게이트 지연 (묶음 {len(lat)}회, 조치 {sum(x["actions"] for x in lat)}건)',
                  f'  묶음당  중앙값 {q(tot, .5):.2f}초, 90% {q(tot, .9):.2f}초, 최대 {tot[-1]:.2f}초',
                  f'  조치당  중앙값 {q(per, .5) * 1000:.0f}ms, 90% {q(per, .9) * 1000:.0f}ms',
                  f'  문맥 추출 비중 {sum(x["context_s"] for x in lat) / sum(tot) * 100:.0f}%, '
                  f'문맥 트리플 중앙값 {sorted(x["triples"] for x in lat)[len(lat) // 2]:,}']
    ok = not failures
    lines += ['', '판정: ' + ('통과 (go)' if ok else f'실패 {len(failures)}건 (no-go)')]
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(sdir, f'{name}_gate_check{a.tag}.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(sample=sample, failures=failures, latency=lat, summary=text), f, ensure_ascii=False, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
