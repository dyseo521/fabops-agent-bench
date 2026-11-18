"""진단 판정식의 대안 비교. 문헌에 정답 정의가 없는 유형에서 정의를 바꿔도 답이 같은 문항의 비율을 낸다.

전부 원천 SQL DB에서 pandas로 계산한다(KG를 쓰지 않는다). 과제 사전 등록 전의 판정식 확정 근거(연구노트 9.3절).
결과는 data/derived/profile/<이름>_diag_alternatives.json.

  1) CQT 초과 구간: 위반 1건에서 책임 step을 고르는 세 정의
       A   대기(앞 step track-out ~ 이 step track-in)가 가장 긴 step
       A2  체류(대기 + 공정) - 평균 공정 시간이 가장 큰 step. 종료 step은 대기만
       B   비례 예산 대비 초과가 가장 큰 step. 예산 = CQT 한도 x (step의 RPT / 구간 RPT 합). 종료 step의 예산은 0
           (Kopp 2020 WSC의 flow factor 배분을 사후 판정에 옮긴 운영화. 원문 식과 대조 필요)
  2) CQT 요인 라벨: 책임 step(A)의 대기 구간을 그 lot을 처리한 장비의 상태 구간과 겹쳐 라벨
       고장(UDT), PM(SDT/PM), 배치 대기(배치 장비가 SBY), 순수 대기(그 밖). 겹침 기준 25, 50, 75%
  3) 병목 식별: 하루 단위 toolgroup 순위의 세 정의
       M1  장비별 평균 active period(연속 비 SBY 구간) 길이의 toolgroup 평균 (Roser 2001의 장비 단위 정의를 평균)
       M2  toolgroup의 전 장비가 동시에 active인 구간의 평균 길이
       M3  toolgroup의 active 시간 비율(1 - SBY 비중)
  4) 가동률 저하 원인: 전일 대비 PRD 비중이 10%p 이상 떨어진 toolgroup-일에서 증가폭 1위 손실 항목
       4분해(고장, PM, setup, 유휴) 대 5분해(유휴를 '대기 lot 없음'과 '대기 lot 있으나 미처리'로 분리)
  5) 장비 정지의 영향 범위: critical lot 집합(잔여 slack < 예상 정지 시간)이 예상 정지 시간 0.5배·2배에서 같은가

예) python scripts/diag_alternatives.py lvhm_e_cr_s0 lvhm_e_cr_s0_w75
"""
import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, 'data', 'derived')
THETAS = (0.25, 0.5, 0.75)


def sec(s):
    return pd.to_datetime(s).astype('int64') / 1e9


def load(name):
    db = sqlite3.connect(os.path.join(DERIVED, 'db', name + '.sqlite'))
    q = lambda s: pd.read_sql(s, db)
    meta = dict(db.execute('SELECT key, value FROM db_meta').fetchall())
    ex = q("""SELECT lot_id, route_id, step_seq, toolgroup, eqp_id, track_in_ts, track_out_ts FROM mes_lot_step
              UNION SELECT lot_id, route_id, step_seq, toolgroup, eqp_id, track_in_ts, track_out_ts FROM yms_process_history""")
    ex = ex[ex.track_in_ts.notna()].copy()
    ex['tin'] = sec(ex.track_in_ts)
    ex['tout'] = np.where(ex.track_out_ts.notna(), sec(ex.track_out_ts.fillna(ex.track_in_ts)), np.nan)
    D = dict(meta=meta, ex=ex, lot=q('SELECT lot_id, route_id, wafer_qty, complete_ts FROM mes_lot'),
             steps=q('SELECT route_id, step_seq, toolgroup, process_unit, proc_time_mean_min FROM rte_route_step'),
             qdef=q('SELECT * FROM rte_qtime'), snap=q('SELECT * FROM mes_qtime_snapshot'),
             state=q('SELECT eqp_id, state, reason, start_ts, end_ts FROM eqp_state_log'),
             tool=q('SELECT eqp_id, toolgroup FROM eqp_tool'),
             tg=q('SELECT toolgroup, area, tool_count, is_batch, is_delay_station FROM eqp_toolgroup'))
    D['wait'] = q(f"""SELECT toolgroup, queue_in_ts, track_in_ts FROM mes_lot_step
                      WHERE queue_in_ts IS NOT NULL AND track_in_ts IS NOT NULL
                      UNION ALL SELECT toolgroup, queue_in_ts, '{meta['window_end_ts']}' FROM mes_wait_open""")
    D['w0'], D['w1'] = pd.Timestamp(meta['window_start_ts']).timestamp(), pd.Timestamp(meta['window_end_ts']).timestamp()
    return D


def impact_scope(name):
    """유형 4. 고장 시각 t에 열린 Q-time 구간 안에 있고 남은 step에 정지 toolgroup이 있는 lot(노출 lot) 중
    잔여 slack < 예상 정지 시간인 lot(critical lot). 예상 정지 시간을 0.5배, 2배로 바꿔도 집합이 같은가."""
    import oracle
    from asof_state import FabState
    scs = [s for s in json.load(open(os.path.join(DERIVED, 'scenarios', name + '.json'), encoding='utf-8'))['scenarios']
           if s['down_type'] == 'BREAKDOWN']
    fs = FabState(os.path.join(DERIVED, 'db', name + '.sqlite'))
    rows = []
    for sc in scs:
        t, group = sc['time'], sc['toolgroup']
        exp_h = oracle.expected_down_h(fs, group, 'BREAKDOWN', sc['pm_trigger'])
        w = fs.wip(t)
        o = fs.qtime_open(t).merge(w[['lot_id', 'step_seq', 'state']], on='lot_id')
        need = fs.steps[fs.steps.toolgroup == group]
        o['first'] = o.step_seq + (o.state != 'WAIT').astype(int)
        m = o.merge(need[['route_id', 'step_seq']].rename(columns={'step_seq': 's'}), on='route_id')
        inside = o[o.lot_id.isin(m[(m.s >= m['first']) & (m.s <= m.exit_step_seq)].lot_id)].drop_duplicates('lot_id')
        if not len(inside):
            continue
        live = inside[inside.slack_h > 0]                       # 이미 기한을 넘긴 lot은 이번 정지의 영향이 아니다
        sets = {k: frozenset(live[live.slack_h < exp_h * k].lot_id) for k in (0.5, 1.0, 2.0)}
        rows.append(dict(exposed=len(inside), overdue=int((inside.slack_h <= 0).sum()), crit=len(sets[1.0]),
                         same=sets[0.5] == sets[1.0] == sets[2.0], same_half=sets[0.5] == sets[1.0], same_double=sets[1.0] == sets[2.0]))
    R = pd.DataFrame(rows)
    nz = R[R.crit > 0]
    return dict(breakdown_scenarios=len(scs), with_exposed_lots=len(R), exposed_median=float(R.exposed.median()), exposed_max=int(R.exposed.max()),
                with_overdue_lots=int((R.overdue > 0).sum()), with_critical_lots=len(nz),
                critical_median=float(nz.crit.median()) if len(nz) else 0, critical_max=int(nz.crit.max()) if len(nz) else 0,
                critical_set_same_under_half_and_double=round(float(R.same.mean()), 3),
                same_under_half=round(float(R.same_half.mean()), 3), same_under_double=round(float(R.same_double.mean()), 3),
                among_nonempty_same=round(float(nz.same.mean()), 3) if len(nz) else None)


def util_drop(D):
    """유형 5. 전일 대비 PRD 비중이 10%p 이상 떨어진 toolgroup-일에서 증가폭이 가장 큰 손실 항목.
    4분해(고장, PM, setup, 유휴)와 5분해(유휴를 '대기 lot 없음'과 '대기 lot 있으나 미처리'로 분리)의 1위 비교."""
    st = D['state'].merge(D['tool'], on='eqp_id')
    st['s'], st['e'] = sec(st.start_ts), sec(st.end_ts)
    st['lab'] = np.where(st.state == 'PRD', 'PRD', np.where(st.state == 'SBY', 'SBY', np.where(st.state == 'UDT', 'UDT',
                np.where((st.state == 'SDT') & (st.reason == 'PM'), 'PM', np.where(st.state == 'SDT', 'SETUP', 'OTHER')))))
    real = set(D['tg'][D['tg'].is_delay_station == 0].toolgroup)
    st = st[st.toolgroup.isin(real)]
    wt = D['wait'].copy()
    wt['a'], wt['b'] = sec(wt.queue_in_ts), sec(wt.track_in_ts)
    cover = {}
    for tg, g in wt[wt.b > wt.a].sort_values('a').groupby('toolgroup'):   # 대기 lot이 있던 시간대의 합집합
        us, ue = [], []
        for a_, b_ in zip(g.a.values, g.b.values):
            if us and a_ <= ue[-1]:
                ue[-1] = max(ue[-1], b_)
            else:
                us.append(a_)
                ue.append(b_)
        us, ue = np.array(us), np.array(ue)
        cover[tg] = (us, ue, np.concatenate([[0.0], np.cumsum(ue - us)]))

    def covered(tg, x):
        if tg not in cover:
            return np.zeros(len(x))
        us, ue, cum = cover[tg]
        i = np.searchsorted(us, x, 'right') - 1
        part = np.where(i >= 0, np.minimum(x, ue[np.maximum(i, 0)]) - us[np.maximum(i, 0)], 0.0)
        return np.where(i >= 0, cum[np.maximum(i, 0)] + part, 0.0)

    days = int(round((D['w1'] - D['w0']) / 86400))
    share = {}
    for d in range(days):
        a, b = D['w0'] + d * 86400, D['w0'] + (d + 1) * 86400
        g = st[(st.e > a) & (st.s < b)].copy()
        g['s'], g['e'] = g.s.clip(lower=a), g.e.clip(upper=b)
        g['dur'] = g.e - g.s
        for tg, x in g.groupby('toolgroup'):
            tot = x.dur.sum()
            sh = (x.groupby('lab').dur.sum() / tot).to_dict()
            idle = x[x.lab == 'SBY']
            q = float((covered(tg, idle.e.values) - covered(tg, idle.s.values)).sum() / tot) if len(idle) else 0.0
            sh['IDLE_Q'], sh['IDLE_NOQ'] = q, sh.get('SBY', 0.0) - q
            share[(tg, d)] = sh
    rows = []
    for (tg, d), cur in share.items():
        prev = share.get((tg, d - 1))
        if prev is None or prev.get('PRD', 0) - cur.get('PRD', 0) < 0.10:
            continue
        inc = lambda keys: sorted(((cur.get(k, 0) - prev.get(k, 0), k) for k in keys), reverse=True)
        i4, i5 = inc(('UDT', 'PM', 'SETUP', 'SBY')), inc(('UDT', 'PM', 'SETUP', 'IDLE_NOQ', 'IDLE_Q'))
        rows.append(dict(tg=tg, day=d, top4=i4[0][1], margin4=i4[0][0] - i4[1][0], top5=i5[0][1], margin5=i5[0][0] - i5[1][0],
                         drop=prev.get('PRD', 0) - cur.get('PRD', 0)))
    R = pd.DataFrame(rows)
    consistent = (R.top4 == R.top5) | ((R.top4 == 'SBY') & R.top5.str.startswith('IDLE'))
    return dict(candidates=len(R), top4=R.top4.value_counts().to_dict(), top5=R.top5.value_counts().to_dict(),
                top_consistent_4_vs_5=round(float(consistent.mean()), 3),
                non_idle_top_changes_to_idle=int(((R.top4 != 'SBY') & R.top5.str.startswith('IDLE')).sum()),
                idle_top_changes_to_non_idle=int(((R.top4 == 'SBY') & ~R.top5.str.startswith('IDLE')).sum()),
                margin4_ge_5pp=round(float((R.margin4 >= 0.05).mean()), 3), margin5_ge_5pp=round(float((R.margin5 >= 0.05).mean()), 3))


def violations(D, all_passes=False):
    """종료 step 진입이 기록 구간 안인 Q-time 위반. (lot, route, entry, exit, limit_h, t_out, t_in)."""
    ex, qd = D['ex'], D['qdef']
    ent = ex[ex.tout.notna()].merge(qd, left_on=['route_id', 'step_seq'], right_on=['route_id', 'entry_step_seq'])
    ent = ent[['lot_id', 'route_id', 'entry_step_seq', 'exit_step_seq', 'qtime_limit_h', 'tout']].rename(columns={'tout': 't_out'})
    sn = D['snap'].merge(D['lot'][['lot_id', 'route_id']], on='lot_id').merge(qd, on=['route_id', 'entry_step_seq', 'exit_step_seq'])
    sn['t_out'] = sec(sn.entry_track_out_ts)
    ent = pd.concat([ent, sn[ent.columns]], ignore_index=True).drop_duplicates()
    xs = ex[['lot_id', 'step_seq', 'tin']].rename(columns={'step_seq': 'exit_step_seq', 'tin': 't_in'})
    m = ent.merge(xs, on=['lot_id', 'exit_step_seq'])
    m = m[m.t_in > m.t_out]
    m = m.loc[m.groupby(['lot_id', 'entry_step_seq', 'exit_step_seq', 't_out']).t_in.idxmin()]
    m['over_h'] = (m.t_in - m.t_out) / 3600 - m.qtime_limit_h
    keep = (m.t_in >= D['w0']) & (m.t_in <= D['w1'])
    return m[keep if all_passes else keep & (m.over_h > 0)].reset_index(drop=True)


class States:
    """장비별 상태 구간. 구간 [a, b]에서 상태별 겹침 시간."""
    def __init__(self, state):
        st = state.copy()
        st['s'], st['e'] = sec(st.start_ts), sec(st.end_ts)
        st['lab'] = np.where(st.state == 'UDT', 'UDT', np.where((st.state == 'SDT') & (st.reason == 'PM'), 'PM', st.state))
        self.by = {k: (g.s.values, g.e.values, g.lab.values) for k, g in st.sort_values('s').groupby('eqp_id')}

    def share(self, eqp, a, b):
        out = Counter()
        if eqp not in self.by or b <= a:
            return out
        s, e, lab = self.by[eqp]
        ov = np.minimum(e, b) - np.maximum(s, a)
        for l, o in zip(lab[ov > 0], ov[ov > 0]):
            out[l] += o
        tot = b - a
        return Counter({k: v / tot for k, v in out.items()})


def cqt(D, V, detail=False):
    """유형 1, 2. 위반마다 구간 안 실행 step 사슬을 만들고 세 정의의 책임 step과 요인 라벨을 낸다."""
    ex, steps = D['ex'], D['steps'].set_index(['route_id', 'step_seq'])
    lots = D['lot'].set_index('lot_id')
    by_lot = {k: g.sort_values('tin') for k, g in ex[ex.lot_id.isin(V.lot_id.unique())].groupby('lot_id')}
    is_batch = dict(zip(D['tg'].toolgroup, D['tg'].is_batch))
    S = States(D['state'])
    rows = []
    for v in V.itertuples():
        g = by_lot[v.lot_id]
        ch = g[(g.tin > v.t_out) & (g.tin <= v.t_in) & (g.step_seq > v.entry_step_seq) & (g.step_seq <= v.exit_step_seq)]
        complete = (v.t_out >= D['w0']) or pd.notna(lots.complete_ts.get(v.lot_id))
        if len(ch) == 0 or not complete or ch.iloc[-1].step_seq != v.exit_step_seq:
            rows.append(dict(ok=False))
            continue
        prev = np.concatenate([[v.t_out], ch.tout.values[:-1]])
        wait = (ch.tin.values - prev) / 3600                      # 대기(이동 포함)
        proc = np.nan_to_num((ch.tout.values - ch.tin.values) / 3600)
        proc[-1] = 0.0                                            # 종료 step의 공정 시간은 구간 밖
        wq = int(lots.wafer_qty.get(v.lot_id, 25))
        rpt = np.array([(steps.proc_time_mean_min.get((v.route_id, int(s)), 0.0) / 60) *
                        (wq if steps.process_unit.get((v.route_id, int(s))) == 'WAFER' else 1) for s in ch.step_seq])
        rpt[-1] = 0.0
        a = int(np.argmax(wait))
        a2 = int(np.argmax(wait + proc - rpt))
        budget = v.qtime_limit_h * rpt / rpt.sum() if rpt.sum() > 0 else np.zeros(len(rpt))
        b = int(np.argmax(wait + proc - budget))
        r = ch.iloc[a]
        sh = S.share(r.eqp_id, prev[a], r.tin) if pd.notna(r.eqp_id) else Counter()
        labs = {}
        for th in THETAS:
            cand = {'UDT': sh.get('UDT', 0), 'PM': sh.get('PM', 0), 'BATCH': sh.get('SBY', 0) if is_batch.get(r.toolgroup) else 0}
            k = max(cand, key=cand.get)
            labs[th] = k if cand[k] >= th else 'QUEUE'
        rows.append(dict(ok=True, lot_id=v.lot_id, route_id=v.route_id, entry=int(v.entry_step_seq), exit=int(v.exit_step_seq),
                         limit_h=float(v.qtime_limit_h), t_out=float(v.t_out), t_in=float(v.t_in), wait_h=float(wait[a]),
                         eqp=r.eqp_id, n_steps=len(ch), a=int(ch.step_seq.iloc[a]), a2=int(ch.step_seq.iloc[a2]), b=int(ch.step_seq.iloc[b]),
                         tg=r.toolgroup, wait_share=float(wait[a] / max(1e-9, (v.t_in - v.t_out) / 3600)),
                         over_h=float(v.over_h), **{f'lab{int(th * 100)}': labs[th] for th in THETAS}))
    R = pd.DataFrame(rows)
    if detail:
        return R[R.ok].copy()
    ok = R[R.ok].copy()
    multi = ok[ok.n_steps >= 2]
    out = dict(violations=len(V), usable=len(ok), excluded_incomplete_chain=int((~R.ok).sum()),
               single_step_segments=int((ok.n_steps == 1).sum()), multi_step=len(multi),
               steps_in_chain_median=float(multi.n_steps.median()) if len(multi) else 0,
               agree_A_A2=round(float((multi.a == multi.a2).mean()), 3) if len(multi) else None,
               agree_A_B=round(float((multi.a == multi.b).mean()), 3) if len(multi) else None,
               agree_A2_B=round(float((multi.a2 == multi.b).mean()), 3) if len(multi) else None,
               agree_all_three=round(float(((multi.a == multi.a2) & (multi.a == multi.b)).mean()), 3) if len(multi) else None,
               top_wait_share_median=round(float(multi.wait_share.median()), 3) if len(multi) else None)
    lab = {}
    for th in THETAS:
        c = ok[f'lab{int(th * 100)}'].value_counts()
        lab[str(th)] = {k: int(c.get(k, 0)) for k in ('UDT', 'PM', 'BATCH', 'QUEUE')}
    out['factor_labels_by_threshold'] = lab
    out['label_same_25_50_75'] = round(float(((ok.lab25 == ok.lab50) & (ok.lab50 == ok.lab75)).mean()), 3)
    # toolgroup별 1위 요인(순수 대기 제외 여부 두 가지)이 기준에 따라 바뀌는가. 위반 20건 이상인 toolgroup만
    stab, n_tg = 0, 0
    for tg, g in ok.groupby('tg'):
        if len(g) < 20:
            continue
        n_tg += 1
        tops = {th: g[f'lab{int(th * 100)}'].value_counts().idxmax() for th in THETAS}
        stab += len(set(tops.values())) == 1
    out['toolgroups_ge20'] = n_tg
    out['toolgroups_top_factor_stable'] = stab
    out['violations_by_responsible_toolgroup_top5'] = ok.tg.value_counts().head(5).to_dict()
    return out


def bottleneck(D):
    """유형 3. 하루 단위 toolgroup 병목 순위의 세 정의와 정의 간 일치."""
    st = D['state'].merge(D['tool'], on='eqp_id')
    st['s'], st['e'] = sec(st.start_ts), sec(st.end_ts)
    st['active'] = st.state != 'SBY'
    real = set(D['tg'][D['tg'].is_delay_station == 0].toolgroup)
    n_tools = D['tool'].groupby('toolgroup').eqp_id.nunique().to_dict()
    days = int(round((D['w1'] - D['w0']) / 86400))
    res = []
    for d in range(days):
        a, b = D['w0'] + d * 86400, D['w0'] + (d + 1) * 86400
        g = st[(st.e > a) & (st.s < b) & st.toolgroup.isin(real)].copy()
        g['s'], g['e'] = g.s.clip(lower=a), g.e.clip(upper=b)
        m1, m2, m3 = {}, {}, {}
        for tg, x in g.groupby('toolgroup'):
            per_tool = []
            for _, y in x.sort_values('s').groupby('eqp_id'):
                runs, cur = [], 0.0
                for act, s, e in zip(y.active, y.s, y.e):   # 연속 active 구간을 이어 붙인다
                    if act:
                        cur += e - s
                    elif cur > 0:
                        runs.append(cur)
                        cur = 0.0
                if cur > 0:
                    runs.append(cur)
                per_tool.append(np.mean(runs) if runs else 0.0)
            m1[tg] = float(np.mean(per_tool)) / 3600
            # 전 장비가 active인 구간: idle 장비 수가 0인 시간대
            ev = sorted([(s, 1) for s in x[~x.active].s] + [(e, -1) for e in x[~x.active].e])
            idle, t, runs = 0, a, []
            for tt, dv in ev:
                if idle == 0 and tt > t:
                    runs.append(tt - t)
                idle += dv
                t = tt
            if idle == 0 and b > t:
                runs.append(b - t)
            full = n_tools.get(tg, 0) == x.eqp_id.nunique()
            m2[tg] = float(np.mean(runs)) / 3600 if runs and full else 0.0
            m3[tg] = float(((x.e - x.s) * x.active).sum() / (x.e - x.s).sum())
        top = lambda m, k: [t for t, _ in sorted(m.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
        tied = lambda m: sum(1 for v in m.values() if v >= max(m.values()) * 0.999)   # 최댓값과 같은 toolgroup 수
        keys = sorted(m1)
        rho = lambda x, y: round(float(pd.Series([x[k] for k in keys]).corr(pd.Series([y[k] for k in keys]), method='spearman')), 3)
        res.append(dict(day=d, top1=dict(M1=top(m1, 1)[0], M2=top(m2, 1)[0], M3=top(m3, 1)[0]),
                        top3=dict(M1=top(m1, 3), M2=top(m2, 3), M3=top(m3, 3)),
                        tied_at_max=dict(M1=tied(m1), M2=tied(m2), M3=tied(m3)),
                        max_value=dict(M1_h=round(max(m1.values()), 2), M2_h=round(max(m2.values()), 2), M3=round(max(m3.values()), 3)),
                        spearman=dict(M1_M2=rho(m1, m2), M1_M3=rho(m1, m3), M2_M3=rho(m2, m3)),
                        saturated_toolgroups=sorted(k for k, v in m3.items() if v >= 0.999)[:12],
                        tools_in_saturated={k: n_tools.get(k, 0) for k, v in m3.items() if v >= 0.999}))
    pair = lambda x, y: sum(r['top1'][x] == r['top1'][y] for r in res)
    ov = lambda x, y: float(np.mean([len(set(r['top3'][x]) & set(r['top3'][y])) / 3 for r in res]))
    return dict(days=days, top1_by_day=[r['top1'] for r in res],
                tied_at_max_by_day=[r['tied_at_max'] for r in res], max_value_by_day=[r['max_value'] for r in res],
                spearman_by_day=[r['spearman'] for r in res],
                saturated_by_day=[r['tools_in_saturated'] for r in res],
                top1_same_M1_M2=pair('M1', 'M2'), top1_same_M1_M3=pair('M1', 'M3'), top1_same_M2_M3=pair('M2', 'M3'),
                top3_overlap_M1_M2=round(ov('M1', 'M2'), 2), top3_overlap_M1_M3=round(ov('M1', 'M3'), 2),
                top3_overlap_M2_M3=round(ov('M2', 'M3'), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('names', nargs='+')
    a = ap.parse_args()
    for name in a.names:
        D = load(name)
        V = violations(D)
        out = dict(name=name, cqt=cqt(D, V), bottleneck=bottleneck(D), util_drop=util_drop(D))
        if os.path.exists(os.path.join(DERIVED, 'scenarios', name + '.json')):
            out['impact_scope'] = impact_scope(name)
        with open(os.path.join(DERIVED, 'profile', name + '_diag_alternatives.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
