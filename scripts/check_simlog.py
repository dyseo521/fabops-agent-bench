"""이벤트 로그(fab_event_logger.py 출력)의 정합성을 점검한다. 2주차 go/no-go 기준의 재계산 근거.

점검 항목
  1. 시각 순서     queue_enter <= track_in <= track_out <= next_arrival
  2. 장비 귀속     이벤트의 toolgroup = 장비 마스터의 toolgroup
  3. 다운 중 투입  장비 정지 구간 안의 track-in (다운 차단 실행이면 0이어야 함)
  4. LTL 준수      dedication이 걸린 lot이 대상 step에서 같은 장비를 썼는가
  5. CQT 위반      진입 step track-out ~ 종료 step track-in > CQT. 위반은 주입하지 않으며 자연 발생분만 센다.
                   기록 시작 전에 진입한 구간은 snapshot_cqt.csv의 진입 시각으로 판정한다

예) python scripts/check_simlog.py data/derived/simlog/lvhm_e_cr_s0
결과는 같은 폴더의 check_report.txt에도 저장한다. 1~4 중 하나라도 어긋나면 종료 코드 1.
"""
import argparse
import csv
import json
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASETS = os.path.join(ROOT, 'data', 'PySCFabSim-release', 'datasets')
EPS = 1e-3
UNIT_S = {'sec': 1, 's': 1, 'min': 60, 'hr': 3600, 'h': 3600, 'day': 86400}


def load_cqt(dataset):
    """route 파일에서 CQT 정의를 읽는다. 행의 step = 진입 step, STEP_CQT = 종료 step."""
    rows = []
    d = os.path.join(DATASETS, dataset)
    for fn in sorted(os.listdir(d)):
        if not (fn.startswith('route_') and fn.endswith('.txt')):
            continue
        with open(os.path.join(d, fn), encoding='utf-8') as f:
            for r in csv.DictReader(f, delimiter='\t'):
                if r.get('CQT', '').strip():
                    rows.append(dict(route=fn[:-4], entry_seq=int(r['STEP']), exit_seq=int(r['STEP_CQT']),
                                     cqt_s=float(r['CQT']) * UNIT_S[r['CQTUNITS'].strip()]))
    return pd.DataFrame(rows)


def check(log_dir):
    meta = json.load(open(os.path.join(log_dir, 'run_meta.json'), encoding='utf-8'))
    ev = pd.read_csv(os.path.join(log_dir, 'lot_step_event.csv'))
    tools = pd.read_csv(os.path.join(log_dir, 'tools.csv'))
    down = pd.read_csv(os.path.join(log_dir, 'tool_down.csv'))
    ded = pd.read_csv(os.path.join(log_dir, 'lot_dedication.csv'))
    sded = pd.read_csv(os.path.join(log_dir, 'snapshot_dedication.csv'))
    skip = pd.read_csv(os.path.join(log_dir, 'lot_step_skipped.csv'))
    log_from, sim_end = meta['log_from_s'], meta['sim_end_s']
    out, fail = [], []

    def say(s=''):
        out.append(s)

    say(f"로그: {os.path.relpath(log_dir, ROOT)}")
    say(f"실행: {meta['dataset']}, {meta['days']:.0f}일 중 {meta['log_from_day']:.0f}일부터 기록, "
        f"dispatcher={meta['dispatcher']}, seed={meta['seed']}, 다운 차단={meta['strict_downtime']}")
    say(f"규모: step 이벤트 {len(ev):,} / 건너뛴 step {len(skip):,} / 장비 정지 {len(down):,} / lot {ev.lot_id.nunique():,}")

    # 1. 시각 순서
    done = ev[ev.track_out_s.notna()]
    bad = ((ev.track_in_s < ev.queue_enter_s - EPS).sum()
           + (done.track_out_s < done.track_in_s - EPS).sum()
           + (done.next_arrival_s < done.track_out_s - EPS).sum())
    say(f"\n[1] 시각 순서 오류: {bad}")
    if bad:
        fail.append('시각 순서')

    # 2. 장비 귀속
    tg = ev.merge(tools[['tool_id', 'toolgroup']], on='tool_id', how='left', suffixes=('', '_m'))
    bad = (tg.toolgroup != tg.toolgroup_m).sum()
    say(f"[2] toolgroup 불일치: {bad}")
    if bad:
        fail.append('장비 귀속')

    # 3. 다운 중 투입
    m = ev[['event_id', 'tool_id', 'track_in_s']].merge(down[['tool_id', 'kind', 'start_s', 'end_s']], on='tool_id')
    hit = m[(m.track_in_s > m.start_s + EPS) & (m.track_in_s < m.end_s - EPS)]
    say(f"[3] 장비 정지 중 track-in: {hit.event_id.nunique()}  "
        f"(정지 {len(down):,}건: " + ', '.join(f'{k} {n}' for k, n in down.kind.value_counts().items()) + ')')
    if len(hit) and meta['strict_downtime']:
        fail.append('다운 중 투입')

    # 4. LTL 준수: 기록 구간 중 설정된 것 + 시작 시점에 유효했던 것
    pairs = pd.concat([ded[['lot_id', 'target_step_seq', 'tool_id']], sded], ignore_index=True).drop_duplicates()
    tgt = pairs.merge(ev[['lot_id', 'step_seq', 'tool_id']], left_on=['lot_id', 'target_step_seq'],
                      right_on=['lot_id', 'step_seq'], suffixes=('', '_used'))
    bad = (tgt.tool_id != tgt.tool_id_used).sum()
    say(f"[4] LTL dedication: 설정 {len(pairs):,}건 중 대상 step 도달 {len(tgt):,}건, 다른 장비 사용 {bad}")
    if bad:
        fail.append('LTL')

    # 5. CQT 위반 (자연 발생). 기록 시작 전에 진입한 구간은 snapshot_cqt.csv의 진입 시각을 쓴다
    cqt = load_cqt(meta['dataset'])
    e = ev[['lot_id', 'route', 'step_seq', 'track_in_s', 'track_out_s']]
    seg = cqt.merge(e.rename(columns={'step_seq': 'entry_seq', 'track_out_s': 'entry_out'}),
                    on=['route', 'entry_seq'])[['lot_id', 'route', 'entry_seq', 'exit_seq', 'cqt_s', 'entry_out']]
    seg = seg[seg.entry_out.notna()]
    scqt_path = os.path.join(log_dir, 'snapshot_cqt.csv')
    n_snap = 0
    if os.path.exists(scqt_path):
        sc = pd.read_csv(scqt_path).rename(columns={'entry_step_seq': 'entry_seq', 'exit_step_seq': 'exit_seq',
                                                    'entry_track_out_s': 'entry_out'})
        sc = sc[sc.entry_out < log_from]  # 진입이 기록 구간 밖인 것만. 안쪽은 이벤트에 이미 있다
        lots = pd.read_csv(os.path.join(log_dir, 'lots.csv'), usecols=['lot_id', 'route'])
        sc = sc.merge(lots, on='lot_id', how='left')
        n_snap = len(sc)
        seg = pd.concat([seg, sc[seg.columns]], ignore_index=True).drop_duplicates(['lot_id', 'entry_seq', 'entry_out'])
    x = e.rename(columns={'step_seq': 'exit_seq', 'track_in_s': 'exit_in'})[['lot_id', 'exit_seq', 'exit_in']]
    seg = seg.merge(x, on=['lot_id', 'exit_seq'], how='left')
    seg = seg[seg.exit_in.isna() | (seg.exit_in > seg.entry_out)]  # rework로 같은 step이 반복되면 진입 이후 첫 종료만
    seg = seg.sort_values('exit_in').drop_duplicates(['lot_id', 'entry_seq', 'entry_out'], keep='first')
    closed = seg[seg.exit_in.notna()].copy()
    closed['over_s'] = closed.exit_in - closed.entry_out - closed.cqt_s
    viol = closed[closed.over_s > 0]
    open_ = seg[seg.exit_in.isna()]
    open_over = (sim_end - open_.entry_out > open_.cqt_s).sum()
    skipped_exit = open_.merge(skip[['lot_id', 'step_seq']], left_on=['lot_id', 'exit_seq'],
                               right_on=['lot_id', 'step_seq']).shape[0]
    say(f"[5] CQT 정의 {len(cqt)}건. 기록 구간의 구간 통과 {len(closed):,}건 중 위반 {len(viol):,}건 "
        f"({len(viol) / max(len(closed), 1) * 100:.1f}%)")
    if len(viol):
        h = viol.over_s / 3600
        say(f"    초과 시간(h): 중앙값 {h.median():.2f}, 90% {h.quantile(.9):.2f}, 최대 {h.max():.2f}. "
            f"위반 lot {viol.lot_id.nunique():,}, 위반 발생 CQT 정의 {viol[['route', 'entry_seq']].drop_duplicates().shape[0]}")
    say(f"    기록 종료 시 구간 안에 있는 lot {len(open_):,} (이미 초과 {open_over:,}, 종료 step이 sampling으로 생략 {skipped_exit})")
    say(f"    기록 시작 시 구간 안에 있던 lot {n_snap:,} (snapshot_cqt.csv"
        + (")" if os.path.exists(scqt_path) else " 없음. 이 구간들은 진입 시각을 몰라 위반 판정에서 빠진다)"))

    say('\n판정: ' + ('통과' if not fail else '실패 - ' + ', '.join(fail)))
    return out, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log_dir')
    a = ap.parse_args()
    log_dir = a.log_dir if os.path.isabs(a.log_dir) else os.path.join(ROOT, a.log_dir)
    out, fail = check(log_dir)
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(log_dir, 'check_report.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
