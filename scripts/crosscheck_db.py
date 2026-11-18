"""두 번째 독립 참조 구현: 원 CSV(시뮬레이션 로그) 위 pandas 계산을 원천 SQL DB 위 SQL 계산과 대조한다.

SQL 경로는 build_source_db.py의 ETL(시각 변환, 장비 이름 매핑, 이력 합치기)을 거친다. pandas 경로는 ETL을 거치지 않고
원 CSV와 smat_tool_map.csv에서 바로 계산한다. 두 결과가 같으면 ETL이 정답을 바꾸지 않았다는 근거가 된다.
(문헌조사.md 8.3: 정답 검증. 기획서 7.2)

대조 (건수가 아니라 다중집합으로 비교)
  1. Q-time 구간 통과: (lot, 진입 step, 종료 step, 위반 여부). 종료 step 진입이 기록 구간 안인 것
  2. YMS 공정 이력: 기록 구간에 완료된 lot의 (lot, step, 장비) 행
  3. commonality: 주입 정답 (오퍼레이션, 장비)의 경유 lot 수와 불량 lot 수. wafer sort는 DB에만 있어 DB에서 읽는다

예) python scripts/crosscheck_db.py lvhm_e_cr_s0
    simlog/<이름>, db/<이름>.sqlite, gt/<이름>_defect_s*.json을 쓴다. 다르면 종료 코드 1
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import Counter

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_simlog import load_cqt  # noqa: E402  (route 파일 파서. SQL 경로와 독립)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, 'data', 'derived')


def pandas_side(log_dir, meta):
    lf, end = meta['log_from_s'], meta['sim_end_s']
    cols = ['lot_id', 'route', 'step_seq', 'step_name', 'tool_id', 'track_in_s', 'track_out_s']
    ev = pd.read_csv(os.path.join(log_dir, 'lot_step_event.csv'), usecols=cols)
    hist = pd.read_csv(os.path.join(log_dir, 'lot_step_history.csv'), usecols=cols)
    lots = pd.read_csv(os.path.join(log_dir, 'lots.csv'))
    done = set(lots[lots.done_s.notna() & (lots.done_s >= lf)].lot_id)
    ex = pd.concat([ev, hist[hist.lot_id.isin(done)]]).drop_duplicates(['lot_id', 'step_seq', 'track_in_s'])

    # 1. Q-time
    cqt = load_cqt(meta['dataset'])
    entry = cqt.merge(ex.rename(columns={'step_seq': 'entry_seq', 'track_out_s': 'out'}), on=['route', 'entry_seq'])
    entry = entry[entry.out.notna()][['lot_id', 'entry_seq', 'exit_seq', 'cqt_s', 'out']]
    snap = pd.read_csv(os.path.join(log_dir, 'snapshot_cqt.csv')).rename(columns={
        'entry_step_seq': 'entry_seq', 'exit_step_seq': 'exit_seq', 'entry_track_out_s': 'out'})
    entry = pd.concat([entry, snap[['lot_id', 'entry_seq', 'exit_seq', 'cqt_s', 'out']]]).drop_duplicates(
        ['lot_id', 'entry_seq', 'out'])
    x = ex[['lot_id', 'step_seq', 'track_in_s']].rename(columns={'step_seq': 'exit_seq', 'track_in_s': 'tin'})
    p = entry.merge(x, on=['lot_id', 'exit_seq'])
    p = p[p.tin > p.out].groupby(['lot_id', 'entry_seq', 'exit_seq', 'cqt_s', 'out']).tin.min().reset_index()
    p = p[(p.tin >= lf) & (p.tin <= end)]
    q = Counter(zip(p.lot_id, p.entry_seq, p.exit_seq, (p.tin - p.out) > p.cqt_s))

    # 2. YMS 이력 (원 장비 ID -> SMAT 이름. Delay는 장비 없음)
    tmap = dict(pd.read_csv(os.path.join(DERIVED, 'smat_tool_map.csv'), usecols=['sim_tool_id', 'eqp_id']).values)
    h = ex[ex.lot_id.isin(done)].copy()
    h['eqp'] = h.tool_id.map(tmap)
    y = Counter(zip(h.lot_id, h.step_seq, h.eqp.where(h.eqp.notna(), None)))
    return q, y, h


def sql_side(db):
    db.executescript("""
        CREATE TEMP TABLE ex AS
          SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM mes_lot_step
          UNION SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM yms_process_history;
        CREATE INDEX temp.ix_ex ON ex (lot_id, step_seq, track_in_ts);""")
    w0, w1 = [r[0] for r in db.execute(
        "SELECT value FROM db_meta WHERE key IN ('window_start_ts', 'window_end_ts') ORDER BY key DESC")]
    rows = db.execute(f"""
        WITH entry AS (
          SELECT e.lot_id, q.entry_step_seq, q.exit_step_seq, q.qtime_limit_h, e.track_out_ts AS t_out
          FROM ex e JOIN rte_qtime q ON q.route_id = e.route_id AND q.entry_step_seq = e.step_seq
          WHERE e.track_out_ts IS NOT NULL
          UNION
          SELECT w.lot_id, w.entry_step_seq, w.exit_step_seq, q.qtime_limit_h, w.entry_track_out_ts
          FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)
          JOIN rte_qtime q ON q.route_id = l.route_id AND q.entry_step_seq = w.entry_step_seq),
        pass AS (SELECT entry.*, (SELECT MIN(x.track_in_ts) FROM ex x WHERE x.lot_id = entry.lot_id
                 AND x.step_seq = entry.exit_step_seq AND x.track_in_ts > entry.t_out) AS t_in FROM entry)
        SELECT lot_id, entry_step_seq, exit_step_seq, (julianday(t_in) - julianday(t_out)) * 24 > qtime_limit_h
        FROM pass WHERE t_in >= '{w0}' AND t_in <= '{w1}'""").fetchall()
    q = Counter((l, e, x, bool(v)) for l, e, x, v in rows)
    y = Counter(db.execute('SELECT lot_id, step_seq, eqp_id FROM yms_process_history').fetchall())
    return q, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name', help='simlog/db/gt 공통 이름. 예: lvhm_e_cr_s0')
    a = ap.parse_args()
    log_dir = os.path.join(DERIVED, 'simlog', a.name)
    meta = json.load(open(os.path.join(log_dir, 'run_meta.json'), encoding='utf-8'))
    db = sqlite3.connect(os.path.join(DERIVED, 'db', a.name + '.sqlite'))
    pq, py, hist = pandas_side(log_dir, meta)
    sq, sy = sql_side(db)
    out, fail = [], []

    def cmp(label, pv, sv, detail=''):
        ok = pv == sv
        out.append(f"{'OK ' if ok else 'DIFF'} {label:46s} {detail}")
        if not ok:
            fail.append(label)

    nv = lambda c: sum(n for k, n in c.items() if k[3])
    cmp('Q-time passes (lot, entry, exit, violated)', pq, sq,
        f'pandas {sum(pq.values()):,} / 위반 {nv(pq):,}   SQL {sum(sq.values()):,} / 위반 {nv(sq):,}')
    cmp('YMS history rows (lot, step, tool)', py, sy, f'pandas {sum(py.values()):,}   SQL {sum(sy.values()):,}')

    ws = pd.read_sql('SELECT lot_id, defect_pattern FROM yms_wafer_sort', db)
    for gt_path in sorted(glob.glob(os.path.join(DERIVED, 'gt', f'{a.name}_defect_s*.json'))):
        gt = json.load(open(gt_path, encoding='utf-8'))
        for e in gt['excursions']:
            bad = ws[ws.defect_pattern == e['pattern']].groupby('lot_id').size()
            bad = set(bad[bad >= 3].index)
            lots = set(hist[(hist.step_name == e['operation']) & (hist.eqp == e['eqp_id'])].lot_id)
            g = e['diagnosis']['gt_pair']
            tag = os.path.basename(gt_path)[:-5].split('_')[-1] + ' ' + e['id']
            cmp(f'commonality {tag} (op, tool) lots / bad lots', (len(lots), len(lots & bad)), (g['n_lot'], g['n_bad']),
                f"pandas {len(lots)} / {len(lots & bad)}   SQL {g['n_lot']} / {g['n_bad']}")

    text = '\n'.join([f'pandas(원 CSV) 대 SQL(원천 DB): {a.name}', ''] + out +
                     ['', '판정: ' + ('일치' if not fail else '불일치 - ' + ', '.join(fail))])
    print(text)
    with open(os.path.join(DERIVED, 'db', a.name + '_crosscheck.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
