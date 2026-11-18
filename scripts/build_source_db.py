"""이벤트 로그와 팹 모델로 원천 SQL DB(SQLite)를 만든다. 정답 산출(참조 구현)과 R2RML 매핑의 출발점.

시스템 구분은 테이블 접두사로 표시한다. 시각은 전부 'YYYY-MM-DD HH:MM:SS.sss' 문자열이다.
  rte_  라우트·제약   rte_route, rte_route_step, rte_qtime, rte_ltl_dedication
  eqp_  장비·PM       eqp_bay, eqp_toolgroup, eqp_tool, eqp_pm_plan, eqp_reliability, eqp_down_event, eqp_state_log
  mes_  MES           mes_lot, mes_lot_step, mes_sampling_skip, mes_rework, mes_ltl_dedication,
                      mes_wip_snapshot, mes_qtime_snapshot
  yms_  YMS           yms_process_history (기록 구간에 완료된 lot의 전 공정 이력. wafer sort 결과와 짝)
  db_meta             기록 구간과 생성 파라미터
wafer sort 결과(yms_wafer_sort)는 불량 주입 단계(inject_defects.py)에서 만든다. 주입 정답은 이 DB에 넣지 않는다.
완료 lot의 기록 구간 이전 이력은 lot_step_history.csv(run_fab_sim.py --history-from-day)에서 온다.

장비 ID는 SMAT2022 Equipment 시트의 실제 이름으로 바꾼다. toolgroup 안에서 SMAT 이름 정렬 순서 = 시뮬레이터 순번
(결정적 매핑). Delay_32는 물리 장비가 아니므로 eqp_tool에 넣지 않고 mes_lot_step.eqp_id를 비운다.
매핑은 data/derived/smat_tool_map.csv에도 남긴다.

예) python scripts/build_source_db.py data/derived/simlog/lvhm_e_cr_s0
    -> data/derived/db/lvhm_e_cr_s0.sqlite
"""
import argparse
import csv
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASETS = os.path.join(ROOT, 'data', 'PySCFabSim-release', 'datasets')
SMAT_XLSX = os.path.join(ROOT, 'data', 'SMAT2022', 'General Data', 'dataset 4', 'SMAT_2022_Model_Data_-_LVHM_E.xlsx')
TOOL_MAP = os.path.join(ROOT, 'data', 'derived', 'smat_tool_map.csv')
T0 = datetime(2018, 1, 1)  # PySCFabSim 시각 0 = order.txt의 최초 START
DELAY_TG = 'Delay_32'
TG_ALIAS = {'DefMEt_FE_118': 'DefMet_FE_118'}  # SMT2020 측 오타
UNIT_MIN = {'sec': 1 / 60, 's': 1 / 60, 'min': 1, 'hr': 60, 'h': 60, 'day': 1440}

SCHEMA = """
CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE rte_route (route_id TEXT PRIMARY KEY, product_id TEXT, route_type TEXT, step_count INTEGER);
CREATE TABLE rte_route_step (
  route_id TEXT, step_seq INTEGER, operation TEXT, area TEXT, toolgroup TEXT, process_unit TEXT,
  proc_time_mean_min REAL, proc_time_offset_min REAL, batch_min_wafers INTEGER, batch_max_wafers INTEGER,
  setup_id TEXT, sampling_pct REAL, rework_pct REAL, rework_to_step_seq INTEGER,
  PRIMARY KEY (route_id, step_seq));
CREATE TABLE rte_qtime (route_id TEXT, entry_step_seq INTEGER, exit_step_seq INTEGER, qtime_limit_h REAL,
  PRIMARY KEY (route_id, entry_step_seq));
CREATE TABLE rte_ltl_dedication (route_id TEXT, source_step_seq INTEGER, target_step_seq INTEGER,
  PRIMARY KEY (route_id, source_step_seq));

CREATE TABLE eqp_bay (bay_id TEXT PRIMARY KEY, bay_type TEXT, neighbor_bays TEXT);
CREATE TABLE eqp_toolgroup (toolgroup TEXT PRIMARY KEY, area TEXT, tool_count INTEGER, is_batch INTEGER,
  is_cascading INTEGER, is_delay_station INTEGER);
CREATE TABLE eqp_tool (eqp_id TEXT PRIMARY KEY, toolgroup TEXT, bay_id TEXT);
CREATE TABLE eqp_pm_plan (pm_id TEXT PRIMARY KEY, toolgroup TEXT, pm_trigger TEXT, pm_interval REAL,
  interval_unit TEXT, duration_mean_h REAL, duration_offset_h REAL);
CREATE TABLE eqp_reliability (area TEXT PRIMARY KEY, mtbf_h REAL, mttr_h REAL);
CREATE TABLE eqp_down_event (down_id TEXT PRIMARY KEY, eqp_id TEXT, down_type TEXT, pm_trigger TEXT,
  start_ts TEXT, end_ts TEXT);
CREATE TABLE eqp_state_log (eqp_id TEXT, state TEXT, reason TEXT, start_ts TEXT, end_ts TEXT);

CREATE TABLE mes_lot (lot_id TEXT PRIMARY KEY, product_id TEXT, route_id TEXT, lot_type TEXT, priority INTEGER,
  wafer_qty INTEGER, release_ts TEXT, due_ts TEXT, complete_ts TEXT);
CREATE TABLE mes_lot_step (event_id INTEGER PRIMARY KEY, lot_id TEXT, route_id TEXT, step_seq INTEGER,
  operation TEXT, toolgroup TEXT, eqp_id TEXT, run_id INTEGER, run_lot_count INTEGER, queue_in_ts TEXT,
  track_in_ts TEXT, setup_min REAL, track_out_ts TEXT, next_arrival_ts TEXT);
CREATE TABLE mes_sampling_skip (lot_id TEXT, route_id TEXT, step_seq INTEGER, operation TEXT, toolgroup TEXT,
  skip_ts TEXT);
CREATE TABLE mes_rework (lot_id TEXT, route_id TEXT, from_step_seq INTEGER, to_step_seq INTEGER, rework_ts TEXT);
CREATE TABLE mes_ltl_dedication (lot_id TEXT, target_step_seq INTEGER, eqp_id TEXT, source_step_seq INTEGER,
  set_ts TEXT);
CREATE TABLE mes_wip_snapshot (lot_id TEXT PRIMARY KEY, state TEXT, step_seq INTEGER, operation TEXT,
  toolgroup TEXT, eqp_id TEXT, since_ts TEXT);
CREATE TABLE mes_qtime_snapshot (lot_id TEXT, entry_step_seq INTEGER, exit_step_seq INTEGER,
  entry_track_out_ts TEXT);
-- 기록 종료 시점에 대기 중인 lot과 그 대기 step. mes_lot_step에는 track-in한 step만 있다
CREATE TABLE mes_wait_open (lot_id TEXT PRIMARY KEY, step_seq INTEGER, operation TEXT, toolgroup TEXT, queue_in_ts TEXT);

CREATE TABLE yms_process_history (lot_id TEXT, route_id TEXT, step_seq INTEGER, operation TEXT, toolgroup TEXT,
  eqp_id TEXT, track_in_ts TEXT, track_out_ts TEXT);

CREATE INDEX ix_step_lot ON mes_lot_step (lot_id, step_seq);
CREATE INDEX ix_step_eqp ON mes_lot_step (eqp_id, track_in_ts);
CREATE INDEX ix_step_route ON mes_lot_step (route_id, step_seq);
CREATE INDEX ix_state_eqp ON eqp_state_log (eqp_id, start_ts);
CREATE INDEX ix_down_eqp ON eqp_down_event (eqp_id, start_ts);
CREATE INDEX ix_hist_lot ON yms_process_history (lot_id, step_seq);
CREATE INDEX ix_hist_eqp ON yms_process_history (eqp_id, track_in_ts);
"""


def ts(s):
    if s in ('', None):
        return None
    return (T0 + timedelta(seconds=float(s))).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def num(x, cast=float):
    return None if x in ('', None) else cast(float(x))


def read_tsv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def read_csv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def tool_map(tools):
    """시뮬레이터 장비(T0000) -> SMAT2022 장비 이름. toolgroup 내 순번 대응."""
    wb = openpyxl.load_workbook(SMAT_XLSX, read_only=True, data_only=True)
    eq = defaultdict(list)
    for r in wb['Equipment'].iter_rows(min_row=2, values_only=True):
        if r[0]:
            eq[r[1]].append(dict(name=r[0], bay=r[2], x=r[5], y=r[6]))
    bays = [dict(bay_id=r[0], bay_type=r[2], neighbor_bays=r[4])
            for r in wb['Bay'].iter_rows(min_row=2, values_only=True) if r[0]]
    wb.close()
    for v in eq.values():
        v.sort(key=lambda d: d['name'])
    rows, by_tg = [], defaultdict(list)
    for t in tools:
        by_tg[t['toolgroup']].append(t)
    for tg, lst in by_tg.items():
        if tg == DELAY_TG:
            continue
        smat = eq.get(TG_ALIAS.get(tg, tg))
        if smat is None or len(smat) != len(lst):
            raise SystemExit(f'SMAT2022 장비 수 불일치: {tg} sim={len(lst)} smat={len(smat or [])}')
        for t in sorted(lst, key=lambda d: int(d['seq_in_group'])):
            s = smat[int(t['seq_in_group']) - 1]
            rows.append(dict(sim_tool_id=t['tool_id'], toolgroup=tg, seq_in_group=t['seq_in_group'],
                             eqp_id=s['name'], bay_id=s['bay'], x=s['x'], y=s['y']))
    return rows, bays


def state_log(eqp_of, events, downs, t_from, t_to):
    """E10 상태 구간. 우선순위 UDT(고장) > SDT(PM) > SDT(setup) > PRD > SBY."""
    iv = defaultdict(list)  # eqp -> (start, end, rank)
    runs = {}
    for e in events:
        eqp = eqp_of.get(e['tool_id'])
        if eqp is None:
            continue
        t_in, setup = float(e['track_in_s']), float(e['setup_s'] or 0)
        t_out = float(e['track_out_s']) if e['track_out_s'] else t_to
        k = (eqp, e['dispatch_id'])
        a, b = runs.get(k, (t_in, t_out))
        runs[k] = (min(a, t_in), max(b, t_out))
        if setup > 0:
            iv[eqp].append((t_in, t_in + setup, 2))
    for (eqp, _), (a, b) in runs.items():
        iv[eqp].append((a, b, 1))
    for d in downs:
        eqp = eqp_of.get(d['tool_id'])
        if eqp is not None:
            iv[eqp].append((float(d['start_s']), float(d['end_s']), 4 if d['kind'] == 'BREAKDOWN' else 3))
    label = {0: ('SBY', None), 1: ('PRD', None), 2: ('SDT', 'SETUP'), 3: ('SDT', 'PM'), 4: ('UDT', 'BREAKDOWN')}
    out = []
    for eqp in sorted(set(eqp_of.values())):
        pts = []
        for a, b, r in iv.get(eqp, []):
            a, b = max(a, t_from), min(b, t_to)
            if b > a:
                pts += [(a, 1, r), (b, -1, r)]
        pts.sort()
        cnt = [0] * 5
        cur, start, i = 0, t_from, 0
        segs = []
        while i <= len(pts):
            t = pts[i][0] if i < len(pts) else t_to
            if t > start:
                segs.append([cur, start, t])
                start = t
            if i == len(pts):
                break
            while i < len(pts) and pts[i][0] == t:
                cnt[pts[i][2]] += pts[i][1]
                i += 1
            cur = max([r for r in range(1, 5) if cnt[r] > 0], default=0)
        merged = []
        for s in segs:
            if merged and merged[-1][0] == s[0] and abs(merged[-1][2] - s[1]) < 1e-6:
                merged[-1][2] = s[2]
            else:
                merged.append(s)
        out += [(eqp, *label[r], ts(a), ts(b)) for r, a, b in merged]
    return out


def build(log_dir, out_path):
    meta = json.load(open(os.path.join(log_dir, 'run_meta.json'), encoding='utf-8'))
    ds = os.path.join(DATASETS, meta['dataset'])
    t_from, t_to = meta['log_from_s'], meta['sim_end_s']
    L = {n: read_csv(os.path.join(log_dir, n + '.csv')) for n in (
        'tools', 'lots', 'lot_step_event', 'lot_step_skipped', 'lot_rework', 'lot_dedication', 'tool_down',
        'snapshot_wip', 'snapshot_dedication', 'snapshot_cqt')}

    tmap, bays = tool_map(L['tools'])
    os.makedirs(os.path.dirname(TOOL_MAP), exist_ok=True)
    with open(TOOL_MAP, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(tmap[0]))
        w.writeheader()
        w.writerows(tmap)
    eqp_of = {r['sim_tool_id']: r['eqp_id'] for r in tmap}

    if os.path.exists(out_path):
        os.remove(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    db = sqlite3.connect(out_path)
    db.executescript(SCHEMA)

    def put(table, rows):
        rows = list(rows)
        if rows:
            db.executemany(f'INSERT INTO {table} VALUES ({",".join("?" * len(rows[0]))})', rows)
        return len(rows)

    counts = {}
    # 라우트·제약
    parts = {r['ROUTEFILE'][:-4]: r for r in read_tsv(os.path.join(ds, 'part.txt'))}
    routes, steps, qt, ltl, batch_tg = [], [], [], [], set()
    for rid, p in sorted(parts.items()):
        rs = read_tsv(os.path.join(ds, p['ROUTEFILE']))
        routes.append((rid, p['PARTFAM'], 'ENGINEERING' if p['PARTGRP'] == 'Engineering' else 'PRODUCTION', len(rs)))
        for r in rs:
            u = UNIT_MIN[r['PTUNITS']]
            unit = {'per_piece': 'WAFER', 'per_lot': 'LOT', 'per_batch': 'BATCH'}[r['PTPER']]
            if unit == 'BATCH':
                batch_tg.add(r['STNFAM'])
            steps.append((rid, int(r['STEP']), r['DESC'], r['IGNORE'], r['STNFAM'], unit,
                          num(r['PTIME']) * u, (num(r['PTIME2']) or 0) * u, num(r['BATCHMN'], int),
                          num(r['BATCHMX'], int), r['SETUP'] or None,
                          num(r['StepPercent']) if r['StepPercent'] else 100.0,
                          num(r['REWORK']), num(r['RWKSTEP'], int)))
            if r['CQT']:
                qt.append((rid, int(r['STEP']), int(r['STEP_CQT']), float(r['CQT']) * UNIT_MIN[r['CQTUNITS']] / 60))
            if r['SVESTN'] == 'yes':
                ltl.append((rid, int(r['STEP']), int(r['FORSTEP'])))
    counts['rte_route'] = put('rte_route', routes)
    counts['rte_route_step'] = put('rte_route_step', steps)
    counts['rte_qtime'] = put('rte_qtime', qt)
    counts['rte_ltl_dedication'] = put('rte_ltl_dedication', ltl)

    # 장비·PM
    counts['eqp_bay'] = put('eqp_bay', [(b['bay_id'], b['bay_type'], b['neighbor_bays']) for b in bays])
    tg_rows = defaultdict(list)
    for t in L['tools']:
        tg_rows[t['toolgroup']].append(t)
    counts['eqp_toolgroup'] = put('eqp_toolgroup', [
        (tg, ts_[0]['area'], len(ts_), int(tg in batch_tg), int(any(t['cascading'] == '1' for t in ts_)),
         int(tg == DELAY_TG)) for tg, ts_ in sorted(tg_rows.items())])
    counts['eqp_tool'] = put('eqp_tool', [(r['eqp_id'], r['toolgroup'], r['bay_id'])
                                          for r in sorted(tmap, key=lambda d: d['eqp_id'])])
    attach = read_tsv(os.path.join(ds, 'attach.txt'))
    pm_tg = {a['CALNAME']: a['RESNAME'] for a in attach if a['CALTYPE'] == 'pm'}
    counts['eqp_pm_plan'] = put('eqp_pm_plan', [
        (r['PMCALNAME'], pm_tg.get(r['PMCALNAME']),
         'CALENDAR' if r['PMCALTYPE'] == 'mtbpm_by_cal' else 'WAFER_COUNT', float(r['MTBPM']),
         'day' if r['MTBPMUNITS'] == 'day' else 'wafer', float(r['MTTR']) * UNIT_MIN[r['MTTRUNITS']] / 60,
         (num(r['MTTR2']) or 0) * UNIT_MIN[r['MTTRUNITS']] / 60)
        for r in read_tsv(os.path.join(ds, 'pmcal.txt'))])
    dn_area = {a['CALNAME']: a['RESNAME'] for a in attach if a['CALTYPE'] == 'down'}
    counts['eqp_reliability'] = put('eqp_reliability', [
        (dn_area[r['DOWNCALNAME']], float(r['MTTF']) * UNIT_MIN[r['MTTFUNITS']] / 60,
         float(r['MTTR']) * UNIT_MIN[r['MTTRUNITS']] / 60) for r in read_tsv(os.path.join(ds, 'downcal.txt'))])
    counts['eqp_down_event'] = put('eqp_down_event', [
        (d['down_id'], eqp_of[d['tool_id']], 'BREAKDOWN' if d['kind'] == 'BREAKDOWN' else 'PM',
         {'PM_TIME': 'CALENDAR', 'PM_COUNTER': 'WAFER_COUNT'}.get(d['kind']), ts(d['start_s']), ts(d['end_s']))
        for d in L['tool_down'] if d['tool_id'] in eqp_of])
    counts['eqp_state_log'] = put('eqp_state_log', state_log(eqp_of, L['lot_step_event'], L['tool_down'], t_from, t_to))

    # MES
    prod = {rid: p['PARTFAM'] for rid, p in parts.items()}
    counts['mes_lot'] = put('mes_lot', [
        (l['lot_id'], prod.get(l['route']), l['route'], l['lot_type'], int(l['priority']), int(l['wafers']),
         ts(l['release_s']), ts(l['due_s']), ts(l['done_s'])) for l in L['lots']])
    counts['mes_lot_step'] = put('mes_lot_step', [
        (int(e['event_id']), e['lot_id'], e['route'], int(e['step_seq']), e['step_name'], e['toolgroup'],
         eqp_of.get(e['tool_id']), int(e['dispatch_id']), int(e['batch_size']), ts(e['queue_enter_s']),
         ts(e['track_in_s']), float(e['setup_s'] or 0) / 60, ts(e['track_out_s']), ts(e['next_arrival_s']))
        for e in L['lot_step_event']])
    counts['mes_sampling_skip'] = put('mes_sampling_skip', [
        (s['lot_id'], s['route'], int(s['step_seq']), s['step_name'], s['toolgroup'], ts(s['time_s']))
        for s in L['lot_step_skipped']])
    counts['mes_rework'] = put('mes_rework', [
        (r['lot_id'], r['route'], int(r['from_step_seq']), int(r['to_step_seq']), ts(r['time_s']))
        for r in L['lot_rework']])
    ded = {(d['lot_id'], d['target_step_seq']): (d['lot_id'], int(d['target_step_seq']), eqp_of[d['tool_id']],
                                                  None, None) for d in L['snapshot_dedication']}
    for d in L['lot_dedication']:
        ded[(d['lot_id'], d['target_step_seq'])] = (d['lot_id'], int(d['target_step_seq']), eqp_of[d['tool_id']],
                                                     int(d['source_step_seq']), ts(d['time_s']))
    counts['mes_ltl_dedication'] = put('mes_ltl_dedication', sorted(ded.values(), key=lambda r: (r[0], r[1])))
    counts['mes_wip_snapshot'] = put('mes_wip_snapshot', [
        (w['lot_id'], 'RUN' if w['state'] == 'PROCESSING' else 'WAIT', int(w['step_seq']), w['step_name'],
         w['toolgroup'], eqp_of.get(w['tool_id']), ts(w['since_s'])) for w in L['snapshot_wip']])
    end_path = os.path.join(log_dir, 'snapshot_wip_end.csv')
    if not os.path.exists(end_path):
        raise SystemExit('snapshot_wip_end.csv가 없다. 로깅 플러그인이 바뀌기 전에 만든 로그다. run_fab_sim.py로 다시 생성한다')
    counts['mes_wait_open'] = put('mes_wait_open', [
        (w['lot_id'], int(w['step_seq']), w['step_name'], w['toolgroup'], ts(w['since_s'])) for w in read_csv(end_path)])
    counts['mes_qtime_snapshot'] = put('mes_qtime_snapshot', [
        (q['lot_id'], int(q['entry_step_seq']), int(q['exit_step_seq']), ts(q['entry_track_out_s']))
        for q in L['snapshot_cqt']])

    # YMS 공정 이력: 기록 구간에 완료된 lot의 전 이력 = 구간 이전 이력 + 구간 내 이벤트
    done = {l['lot_id'] for l in L['lots'] if l['done_s'] and float(l['done_s']) >= t_from}
    hist_path = os.path.join(log_dir, 'lot_step_history.csv')
    hist = []
    if os.path.exists(hist_path):  # 수백만 행이라 한 줄씩 걸러 읽는다
        with open(hist_path, encoding='utf-8') as f:
            hist = [h for h in csv.DictReader(f) if h['lot_id'] in done]
    hist += [e for e in L['lot_step_event'] if e['lot_id'] in done]
    hist.sort(key=lambda h: (h['lot_id'], float(h['track_in_s'])))
    counts['yms_process_history'] = put('yms_process_history', [
        (h['lot_id'], h['route'], int(h['step_seq']), h['step_name'], h['toolgroup'], eqp_of.get(h['tool_id']),
         ts(h['track_in_s']), ts(h['track_out_s'])) for h in hist])

    info = dict(window_start_ts=ts(t_from), window_end_ts=ts(t_to), dataset=meta['dataset'],
                dispatcher=meta['dispatcher'], seed=meta['seed'], strict_downtime=meta['strict_downtime'],
                source_log=os.path.relpath(log_dir, ROOT).replace('\\', '/'),
                built_at=datetime.now().isoformat(timespec='seconds'))
    put('db_meta', [(k, str(v)) for k, v in info.items()])
    db.commit()
    db.close()
    return info, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log_dir')
    ap.add_argument('--out', help='기본값 data/derived/db/<로그 폴더 이름>.sqlite')
    a = ap.parse_args()
    log_dir = a.log_dir if os.path.isabs(a.log_dir) else os.path.join(ROOT, a.log_dir)
    out = a.out or os.path.join(ROOT, 'data', 'derived', 'db', os.path.basename(os.path.normpath(log_dir)) + '.sqlite')
    info, counts = build(log_dir, out)
    print(f"{os.path.relpath(out, ROOT)}  기록 구간 {info['window_start_ts']} ~ {info['window_end_ts']}")
    for k, v in counts.items():
        print(f'  {k:22s} {v:>9,d}')


if __name__ == '__main__':
    main()
