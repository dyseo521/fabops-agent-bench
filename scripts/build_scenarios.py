"""조치 시나리오 후보를 시뮬레이터가 발생시킨 장비 정지(고장, PM)에서 뽑고 템플릿으로 분류한다(기획서 v3 4.3).

인시던트를 저자가 만들지 않는다. 기록 구간의 eqp_down_event가 전부 후보다.
시나리오 시각: 고장은 정지 시작 시각, PM은 시작 PM_LEAD_H시간 전(PM 도래). 재생 평가를 위해 시작이 기록 종료
TAIL_H시간 전보다 늦은 정지는 뺀다. 분류는 시나리오 시각의 as-of 상태(asof_state.py)만 쓴다.

템플릿 (우선순위 순)
  SINGLE_TOOL  정지 장비가 toolgroup의 유일한 장비
  LTL          정지 장비에 전용(dedication)이 걸린 lot이 있음
  PM_CQT       PM이고, 그 toolgroup이 열린 Q-time 구간의 남은 step 또는 진입 대기 lot의 구간 안 step을 맡음
  CQT          고장이고, 위와 같은 Q-time 노출이 있음
  GENERAL      그 toolgroup에서 대기 중인 lot이 있음 (같은 toolgroup의 다른 가동 장비가 처리 가능)
  NO_IMPACT    위 어느 것도 아님

예) python scripts/build_scenarios.py data/derived/db/lvhm_e_cr_s0.sqlite
    -> data/derived/scenarios/lvhm_e_cr_s0.json
"""
import argparse
import json
import os
from collections import Counter

import pandas as pd

from asof_state import FabState

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PM_LEAD_H = 4
TAIL_H = 24


def fmt(ts):
    return ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def exposure(fs, t, group):
    """toolgroup이 t의 Q-time 구간에 걸리는 lot: 구간 안(남은 step에 group) / 진입 대기(구간 안 step에 group)."""
    steps = fs.steps
    need = steps[steps.toolgroup == group][['route_id', 'step_seq']]
    w = fs.wip(t)
    o = fs.qtime_open(t).merge(w[['lot_id', 'step_seq', 'state']], on='lot_id')
    o['first'] = o.step_seq + (o.state != 'WAIT').astype(int)  # 처리 중·반송 중이면 현재 step은 이미 장비가 정해짐
    m = o.merge(need.rename(columns={'step_seq': 's'}), on='route_id')
    inside = m[(m.s >= m['first']) & (m.s <= m.exit_step_seq)].lot_id.unique()
    wait = w[w.state == 'WAIT'].merge(fs.qdefs, left_on=['route_id', 'step_seq'], right_on=['route_id', 'entry_step_seq'])
    m2 = wait.merge(need.rename(columns={'step_seq': 's'}), on='route_id')
    entry = m2[(m2.s > m2.entry_step_seq) & (m2.s <= m2.exit_step_seq)].lot_id.unique()
    return sorted(inside), sorted(entry), o


def classify(fs, d, t):
    tool = d.eqp_id
    group = fs.tool.set_index('eqp_id').toolgroup[tool]
    n_tools = int(fs.tg.set_index('toolgroup').tool_count[group])
    w = fs.wip(t)
    queue = w[(w.state == 'WAIT') & (w.toolgroup == group)].lot_id.unique()
    ded = fs.dedications(t)
    dedicated = sorted(ded[ded.eqp_id == tool].lot_id.unique())
    inside, entry, o = exposure(fs, t, group)
    st = fs.tools(t)
    up_peers = int(((st.toolgroup == group) & (st.eqp_id != tool) & st.state.isin(['PRD', 'SBY'])).sum())
    if n_tools == 1:
        tpl = 'SINGLE_TOOL'
    elif dedicated:
        tpl = 'LTL'
    elif d.down_type == 'PM' and (inside or entry):
        tpl = 'PM_CQT'
    elif inside or entry:
        tpl = 'CQT'
    elif len(queue):
        tpl = 'GENERAL'
    else:
        tpl = 'NO_IMPACT'
    return dict(template=tpl, toolgroup=group, tool_count=n_tools, up_peers_at_t=up_peers, queue_lots=len(queue),
                dedicated_lots=len(dedicated), cqt_inside_lots=len(inside), cqt_entry_lots=len(entry),
                overdue_lots_inside=int((o[o.lot_id.isin(inside)].slack_h < 0).sum()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    a = ap.parse_args()
    db_path = a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db)
    name = os.path.splitext(os.path.basename(db_path))[0]
    fs = FabState(db_path)
    w0, w1 = pd.Timestamp(fs.meta['window_start_ts']), pd.Timestamp(fs.meta['window_end_ts'])
    out = []
    for d in fs.down.sort_values('start_ts').itertuples():
        start = pd.Timestamp(d.start_ts)
        t = start - pd.Timedelta(hours=PM_LEAD_H) if d.down_type == 'PM' else start
        if t < w0 or start > w1 - pd.Timedelta(hours=TAIL_H):
            continue
        sc = dict(id=f'{name}_{d.down_id}', time=fmt(t), down_id=d.down_id, eqp_id=d.eqp_id, down_type=d.down_type,
                  pm_trigger=d.pm_trigger, down_start=d.start_ts)
        sc.update(classify(fs, d, fmt(t)))
        out.append(sc)
    os.makedirs(os.path.join(ROOT, 'data', 'derived', 'scenarios'), exist_ok=True)
    path = os.path.join(ROOT, 'data', 'derived', 'scenarios', name + '.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(dict(db=os.path.relpath(db_path, ROOT).replace('\\', '/'), pm_lead_h=PM_LEAD_H, tail_h=TAIL_H,
                       scenarios=out), f, ensure_ascii=False, indent=1)
    c = Counter(s['template'] for s in out)
    print(f'{os.path.relpath(path, ROOT)}: 후보 {len(out)}건')
    for k in ['SINGLE_TOOL', 'LTL', 'PM_CQT', 'CQT', 'GENERAL', 'NO_IMPACT']:
        sub = [s for s in out if s['template'] == k]
        bd = sum(s['down_type'] == 'BREAKDOWN' for s in sub)
        print(f'  {k:12s} {c[k]:5d}  (고장 {bd}, PM {len(sub) - bd})')


if __name__ == '__main__':
    main()
