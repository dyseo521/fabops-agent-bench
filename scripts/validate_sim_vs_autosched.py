"""PySCFabSim 결과를 SMT2020 공식 배포본의 AutoSched 참조 결과(*.rep)와 대조한다.

1) run    : 시뮬레이션을 길게 돌려 lot 완료 기록과 toolgroup별 시간 집계를 JSON으로 저장
2) report : 저장된 JSON들을 참조 결과와 나란히 비교

예) python scripts/validate_sim_vs_autosched.py run --days 200 --dispatcher cr --tag cr_strict
    python scripts/validate_sim_vs_autosched.py report
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(ROOT, 'data', 'PySCFabSim-release')
REFS = {'SMT2020_LVHM_E': ('dataset 4', 'LVHM_E_Model'), 'SMT2020_LVHM': ('dataset 2', 'LVHM_Model'),
        'SMT2020_HVLM': ('dataset 1', 'HVLM_Model')}
OUT = os.path.join(ROOT, 'data', 'derived', 'sim_validation')
REF = None
DAY = 86400.0


def run(a):
    sys.path.insert(0, SIM)
    from simulation.dispatching.dispatcher import dispatcher_map
    from simulation.file_instance import FileInstance
    from simulation.greedy import get_lots_to_dispatch_by_machine
    from simulation.plugins.interface import IPlugin
    from simulation.randomizer import Randomizer
    from simulation.read import read_all

    class Collector(IPlugin):
        def __init__(self):
            self.done = []

        def on_lot_done(self, instance, lot):
            self.done.append(dict(part=lot.part_name, name=lot.name, release=lot.release_at,
                                  done=instance.current_time, initial=lot.release_at < 0,
                                  wait=lot.waiting_time, proc=lot.processing_time))

    files = read_all(os.path.join(SIM, 'datasets', a.dataset))
    Randomizer().random.seed(a.seed)
    col = Collector()
    run_to = DAY * a.days
    inst = FileInstance(files, run_to, True, [col])
    inst.strict_downtime = not a.upstream_downtime
    disp = dispatcher_map[a.dispatcher]
    wip_samples = []
    next_sample = 0.0
    t0 = datetime.now()
    while not inst.done:
        done = inst.next_decision_point()
        inst.print_progress_in_days()
        if inst.current_time >= next_sample:
            wip_samples.append((inst.current_time / DAY, len(inst.active_lots)))
            next_sample += DAY
        if done or inst.current_time > run_to:
            break
        machine, lots = get_lots_to_dispatch_by_machine(inst, disp)
        if lots is None:
            inst.usable_machines.remove(machine)
        else:
            inst.dispatch(machine, lots)
    fam = defaultdict(lambda: dict(n=0, util=0.0, setup=0.0, pm=0.0, down=0.0))
    for m in inst.machines:
        f = fam[m.family]
        f['n'] += 1
        f['util'] += m.utilized_time
        f['setup'] += m.setuped_time
        f['pm'] += m.pmed_time
        f['down'] += m.bred_time
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, a.tag + '.json'), 'w', encoding='utf-8') as fo:
        json.dump(dict(args=vars(a), sim_days=inst.current_time / DAY, elapsed=str(datetime.now() - t0),
                       done=col.done, wip=wip_samples, fam=fam), fo)
    sys.stderr.write(f'\nsaved {a.tag}\n')


def hms(s):
    h, m, sec = s.split(':')
    return int(h) + int(m) / 60 + int(sec) / 3600


def read_rep(name, period='Period_1'):
    raw = open(os.path.join(REF, name), 'rb').read()
    enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
    lines = [l for l in raw.decode(enc, 'ignore').splitlines() if l.strip() and not l.startswith('~')]
    hdr = lines[0].split('\t')
    return [dict(zip(hdr, l.split('\t'))) for l in lines[1:] if l.split('\t')[0] == period]


def report(a):
    import statistics as st
    global REF, OUT
    REF = os.path.join(ROOT, 'data', 'SMT2020_official', 'SMT_2020 - Final', 'AutoSched', *REFS[a.dataset])
    perf = read_rep('perf.rep')[0]
    parts = {r['PART']: r for r in read_rep('part.rep')}
    fams = {r['STNFAM']: r for r in read_rep('stnfam.rep')}
    runs = {}
    for p in sorted(glob.glob(os.path.join(OUT, '*.json'))):
        r = json.load(open(p, encoding='utf-8'))
        if r['args']['dataset'] == a.dataset:
            runs[os.path.basename(p)[:-5]] = r
    tags = list(runs)
    print(f'참조: AutoSched AP, {a.dataset}, Period_1 (2019년 1년, warm-up 1년 후)')
    print('대조: PySCFabSim,', ', '.join(f"{t}({runs[t]['sim_days']:.0f}일)" for t in tags))
    w0 = a.warmup

    def fresh(r):  # warm-up 이후 투입되어 완료된 lot. 초기 WIP 제외
        return [d for d in r['done'] if d['release'] >= w0 * DAY]

    print('\n[1] 전체')
    print(f"{'지표':24s}{'AutoSched':>12s}" + ''.join(f'{t:>14s}' for t in tags))
    row = [f"{'완료 lot/일':24s}{int(perf['LOTCOMPS']) / 365:12.1f}"]
    for t in tags:
        r = runs[t]
        n = sum(1 for d in r['done'] if d['done'] >= w0 * DAY)
        row.append(f"{n / (r['sim_days'] - w0):14.1f}")
    print(''.join(row))
    row = [f"{'평균 WIP(lot)':24s}{float(perf['WIPLOTAVG']):12.0f}"]
    for t in tags:
        w = [x[1] for x in runs[t]['wip'] if x[0] >= w0]
        row.append(f'{st.mean(w):14.0f}')
    print(''.join(row))
    row = [f"{'평균 사이클(일)':24s}{hms(perf['CYCLEAVG']) / 24:12.1f}"]
    for t in tags:
        c = [(d['done'] - d['release']) / DAY for d in fresh(runs[t])]
        row.append(f'{st.mean(c):14.1f}' if c else f"{'-':>14s}")
    print(''.join(row))

    print(f'\n[2] 제품별 평균 사이클 타임(일). warm-up {w0}일 이후 투입·완료된 lot')
    print(f"{'part':10s}{'AutoSched':>11s}" + ''.join(f'{t:>14s}{"n":>6s}' for t in tags))
    for part in sorted(parts, key=lambda k: (k.startswith('part_E'), k)):
        line = f"{part:10s}{hms(parts[part]['CYCLEAVG']) / 24:11.1f}"
        for t in tags:
            c = [(d['done'] - d['release']) / DAY for d in fresh(runs[t]) if d['part'] == part]
            line += (f'{st.mean(c):14.1f}{len(c):6d}' if c else f"{'-':>14s}{0:6d}")
        print(line)

    print('\n[3] toolgroup별 시간 비율(%). 전 구간 평균. 장비 대수 가중 평균과 상관')
    for key, ref_col, label in [('util', 'UTIL%', '가동률(UTIL)'), ('down', 'DOWN%', '고장(DOWN)'), ('pm', 'PM%', 'PM')]:
        line = f'{label:16s}'
        tot_n = sum(v['n'] for v in runs[tags[0]]['fam'].values() if True)
        ref_w = sum(float(fams[f][ref_col]) * v['n'] for f, v in runs[tags[0]]['fam'].items() if f in fams) / \
            sum(v['n'] for f, v in runs[tags[0]]['fam'].items() if f in fams)
        line += f'AutoSched {ref_w:6.2f}  '
        for t in tags:
            r = runs[t]
            T = r['sim_days'] * DAY
            xs, ys, ws = [], [], []
            for f, v in r['fam'].items():
                if f not in fams:
                    continue
                val = v[key] + (v['setup'] if key == 'util' else 0)
                xs.append(float(fams[f][ref_col]))
                ys.append(100 * val / (v['n'] * T))
                ws.append(v['n'])
            mean = sum(y * w for y, w in zip(ys, ws)) / sum(ws)
            mx, my = st.mean(xs), st.mean(ys)
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            corr = cov / ((sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5 or 1)
            mae = st.mean(abs(x - y) for x, y in zip(xs, ys))
            line += f'| {t}: {mean:6.2f} r={corr:.2f} MAE={mae:.1f}  '
        print(line)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--dataset', default='SMT2020_LVHM_E')
    r.add_argument('--days', type=float, default=200)
    r.add_argument('--dispatcher', default='cr')
    r.add_argument('--seed', type=int, default=0)
    r.add_argument('--upstream-downtime', action='store_true')
    r.add_argument('--tag', required=True)
    p = sub.add_parser('report')
    p.add_argument('--warmup', type=float, default=60)
    p.add_argument('--dataset', default='SMT2020_LVHM_E')
    a = ap.parse_args()
    run(a) if a.cmd == 'run' else report(a)
