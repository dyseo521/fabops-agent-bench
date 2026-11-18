"""시나리오(원천 SQL DB + 정답·시나리오 파일)의 규모와 분포를 요약한다. 논문 3장(벤치마크) 표의 근거.

수치는 전부 원천 DB에서 SQL·pandas로 계산한다(KG를 쓰지 않는다). 결과는 data/derived/profile/<이름>.json.

예) python scripts/profile_benchmark.py lvhm_e_cr_s0 lvhm_e_cr_s1
"""
import argparse
import json
import os
import sqlite3
from collections import Counter

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, 'data', 'derived')
BAD_MIN = 3


def profile(name):
    db = sqlite3.connect(os.path.join(DERIVED, 'db', name + '.sqlite'))
    q = lambda s: pd.read_sql(s, db)
    one = lambda s: db.execute(s).fetchone()[0]
    meta = dict(db.execute('SELECT key, value FROM db_meta').fetchall())
    w0, w1 = meta['window_start_ts'], meta['window_end_ts']
    days = (pd.Timestamp(w1) - pd.Timestamp(w0)).total_seconds() / 86400
    P = dict(name=name, window=[w0, w1], seed=int(meta['seed']), dispatcher=meta['dispatcher'])

    # 1. lot
    lot = q('SELECT * FROM mes_lot')
    done = lot[lot.complete_ts.notna() & (lot.complete_ts >= w0)]
    ct = (pd.to_datetime(done.complete_ts) - pd.to_datetime(done.release_ts)).dt.total_seconds() / 86400
    P['lots'] = dict(total=len(lot), by_type=lot.lot_type.value_counts().to_dict(),
                     engineering=int(lot.lot_type.str.startswith('ENG').sum()),
                     wip_at_start=one('SELECT COUNT(*) FROM mes_wip_snapshot'),
                     released_in_window=int((lot.release_ts >= w0).sum()), completed_in_window=len(done),
                     cycle_days_mean=round(ct.mean(), 1), cycle_days_p10=round(ct.quantile(.1), 1),
                     cycle_days_p90=round(ct.quantile(.9), 1))

    # 2. step 이벤트
    ev = q("""SELECT s.toolgroup, g.area, g.is_delay_station, s.queue_in_ts, s.track_in_ts, s.track_out_ts
              FROM mes_lot_step s JOIN eqp_toolgroup g USING (toolgroup)""")
    wait_h = (pd.to_datetime(ev.track_in_ts) - pd.to_datetime(ev.queue_in_ts)).dt.total_seconds() / 3600
    P['steps'] = dict(events=len(ev), per_day=round(len(ev) / days), delay_steps=int(ev.is_delay_station.sum()),
                      by_area=ev[ev.is_delay_station == 0].area.value_counts().to_dict(),
                      queue_h_median=round(wait_h.median(), 2), queue_h_p90=round(wait_h.quantile(.9), 2),
                      sampling_skips=one('SELECT COUNT(*) FROM mes_sampling_skip'), rework=one('SELECT COUNT(*) FROM mes_rework'))

    # 3. 장비
    down = q("""SELECT d.down_type, d.pm_trigger, g.area, (julianday(d.end_ts) - julianday(d.start_ts)) * 24 AS h,
                       d.start_ts >= '%s' AS in_window
                FROM eqp_down_event d JOIN eqp_tool t USING (eqp_id) JOIN eqp_toolgroup g USING (toolgroup)""" % w0)
    st = q('SELECT state, reason, (julianday(end_ts) - julianday(start_ts)) * 24 AS h FROM eqp_state_log')
    tot = st.h.sum()
    P['equipment'] = dict(tools=one('SELECT COUNT(*) FROM eqp_tool'),
                          toolgroups=one('SELECT COUNT(*) FROM eqp_toolgroup WHERE is_delay_station = 0'),
                          bays=one('SELECT COUNT(DISTINCT bay_id) FROM eqp_tool'),
                          down_events={k: dict(n=len(g), started_in_window=int(g.in_window.sum()),
                                               hours_median=round(g.h.median(), 1), hours_p90=round(g.h.quantile(.9), 1))
                                       for k, g in down.groupby(down.down_type + down.pm_trigger.fillna('').radd('/').str.rstrip('/'))},
                          e10_share_pct={k: round(v / tot * 100, 1) for k, v in st.groupby('state').h.sum().items()},
                          sdt_by_reason_pct={k: round(v / tot * 100, 1) for k, v in st[st.state == 'SDT'].groupby('reason').h.sum().items()})

    # 4. Q-time. 종료 step 진입이 기록 구간 안인 구간 통과
    db.executescript("""
        CREATE TEMP TABLE IF NOT EXISTS ex AS
          SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM mes_lot_step
          UNION SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM yms_process_history;
        CREATE INDEX IF NOT EXISTS temp.ix_ex ON ex (lot_id, step_seq, track_in_ts);""")
    qp = q(f"""
        WITH entry AS (
          SELECT e.lot_id, q.route_id, q.entry_step_seq, q.exit_step_seq, q.qtime_limit_h, e.track_out_ts AS t_out
          FROM ex e JOIN rte_qtime q ON q.route_id = e.route_id AND q.entry_step_seq = e.step_seq
          WHERE e.track_out_ts IS NOT NULL
          UNION
          SELECT w.lot_id, l.route_id, w.entry_step_seq, w.exit_step_seq, q.qtime_limit_h, w.entry_track_out_ts
          FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)
          JOIN rte_qtime q ON q.route_id = l.route_id AND q.entry_step_seq = w.entry_step_seq),
        pass AS (SELECT entry.*, (SELECT MIN(x.track_in_ts) FROM ex x WHERE x.lot_id = entry.lot_id
                 AND x.step_seq = entry.exit_step_seq AND x.track_in_ts > entry.t_out) AS t_in FROM entry)
        SELECT p.*, (julianday(t_in) - julianday(t_out)) * 24 - qtime_limit_h AS over_h, s.area AS exit_area
        FROM pass p JOIN rte_route_step s ON s.route_id = p.route_id AND s.step_seq = p.exit_step_seq
        WHERE t_in >= '{w0}' AND t_in <= '{w1}'""")
    v = qp[qp.over_h > 0]
    P['qtime'] = dict(definitions=one('SELECT COUNT(*) FROM rte_qtime'), passes=len(qp), violations=len(v),
                      rate_pct=round(len(v) / len(qp) * 100, 1), lots_with_violation=int(v.lot_id.nunique()),
                      constraints_violated=int(v[['route_id', 'entry_step_seq']].drop_duplicates().shape[0]),
                      over_h_median=round(v.over_h.median(), 1), over_h_p90=round(v.over_h.quantile(.9), 1),
                      over_h_max=round(v.over_h.max(), 1),
                      by_limit_h={str(int(k)): dict(passes=len(g), violations=int((g.over_h > 0).sum()))
                                  for k, g in qp.groupby('qtime_limit_h')},
                      violations_by_exit_area=v.exit_area.value_counts().to_dict(),
                      snapshot_open_at_start=one('SELECT COUNT(*) FROM mes_qtime_snapshot'))

    # 5. LTL
    P['ltl'] = dict(rules=one('SELECT COUNT(*) FROM rte_ltl_dedication'), records=one('SELECT COUNT(*) FROM mes_ltl_dedication'),
                    lots=one('SELECT COUNT(DISTINCT lot_id) FROM mes_ltl_dedication'),
                    target_runs=one("""SELECT COUNT(*) FROM mes_ltl_dedication d JOIN ex e
                                       ON e.lot_id = d.lot_id AND e.step_seq = d.target_step_seq"""),
                    dedicated_tools=one('SELECT COUNT(DISTINCT eqp_id) FROM mes_ltl_dedication'))

    # 6. YMS
    ws = q('SELECT lot_id, defect_pattern FROM yms_wafer_sort')
    cnt = ws[ws.defect_pattern != 'none'].groupby(['lot_id', 'defect_pattern']).size()
    bad = cnt[cnt >= BAD_MIN].reset_index().defect_pattern.value_counts().to_dict()
    P['yms'] = dict(sorted_lots=int(ws.lot_id.nunique()), wafers=len(ws),
                    history_rows=one('SELECT COUNT(*) FROM yms_process_history'),
                    wafers_by_pattern=ws.defect_pattern.value_counts().to_dict(), bad_lots_by_pattern=bad)
    gt_path = os.path.join(DERIVED, 'gt', f"{name}_defect_s{meta['seed']}.json")
    if os.path.exists(gt_path):
        gt = json.load(open(gt_path, encoding='utf-8'))
        P['injection'] = [dict(id=e['id'], pattern=e['pattern'], operation=e['operation'], eqp_id=e['eqp_id'],
                               span_days=e['span_days'], affected=len(e['affected_lots']), impacted=len(e['impacted_lots']),
                               bad_lots=len(e['bad_lots']), bad_from_excursion=e['bad_from_excursion'],
                               fisher_rank=e['diagnosis']['fisher_rank'], fisher_p=e['diagnosis']['gt_pair']['p'],
                               lift=e['diagnosis']['gt_pair']['lift'], freq_rank=e['diagnosis']['freq_rank'])
                          for e in gt['excursions']]

    # 7. 조치 시나리오와 오라클
    sc_path = os.path.join(DERIVED, 'scenarios', name + '.json')
    if os.path.exists(sc_path):
        scs = json.load(open(sc_path, encoding='utf-8'))['scenarios']
        tpl = Counter((s['template'], s['down_type']) for s in scs)
        P['scenarios'] = dict(total=len(scs), by_template={t: dict(breakdown=tpl[(t, 'BREAKDOWN')], pm=tpl[(t, 'PM')])
                                                           for t in sorted({s['template'] for s in scs})})
    or_path = os.path.join(DERIVED, 'scenarios', name + '_oracle.json')
    if os.path.exists(or_path):
        o = json.load(open(or_path, encoding='utf-8'))
        dec, acts, size = Counter(), Counter(), {}
        for r in o['results']:
            dec[(r['template'], r['decision'])] += 1
            for x in r['alternatives'][0]:
                acts[x['type'] + ('/' + x['request'] if x.get('request') else '')] += 1
            if r['decision'] == 'ACT':
                size.setdefault(r['template'], []).append(len(r['alternatives'][0]))
        P['oracle'] = dict(params=o['params'],
                           by_template={t: dict(act=dec[(t, 'ACT')], no_action=dec[(t, 'NO_ACTION')],
                                                actions_median=(sorted(size[t])[len(size[t]) // 2] if t in size else 0),
                                                actions_max=(max(size[t]) if t in size else 0))
                                        for t in sorted({k[0] for k in dec})},
                           expected_actions_by_type=dict(acts),
                           two_alternatives=sum(1 for r in o['results'] if len(r['alternatives']) > 1))
    gc_path = os.path.join(DERIVED, 'scenarios', name + '_gate_check.json')
    if os.path.exists(gc_path):
        g = json.load(open(gc_path, encoding='utf-8'))
        P['gate_check'] = dict(scenarios=len(g['sample']), failures=len(g['failures']),
                               verdict=g['summary'].strip().splitlines()[-1])
    return P


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('names', nargs='+')
    a = ap.parse_args()
    os.makedirs(os.path.join(DERIVED, 'profile'), exist_ok=True)
    for name in a.names:
        P = profile(name)
        with open(os.path.join(DERIVED, 'profile', name + '.json'), 'w', encoding='utf-8') as f:
            json.dump(P, f, ensure_ascii=False, indent=1, default=str)
        print(json.dumps(P, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
