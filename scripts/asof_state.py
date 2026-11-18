"""시각 t 기준 팹 상태(as-of)를 원천 SQL DB에서 뽑는다. 조치 시나리오 추출과 독립 오라클이 함께 쓴다.

원천 DB는 기록 구간 전체(미래 포함)를 담고 있다. 조치 시나리오에서 에이전트와 오라클은 t 이후 정보를 쓰면 안 된다.
여기 함수는 t 이전에 확정된 사실만 쓴다. 예외: t에 장비가 정지 중이라는 상태는 쓰지만 복구 시각은 쓰지 않는다.
게이트(SHACL)는 이 모듈을 쓰지 않는다. 게이트는 KG에서 SPARQL로 상태를 따로 계산한다(구현 독립).

시각은 DB와 같은 'YYYY-MM-DD HH:MM:SS.sss' 문자열이다. 사전순 비교가 시간순 비교와 같다.
"""
import sqlite3

import pandas as pd


class FabState:
    def __init__(self, db_path):
        db = sqlite3.connect(db_path)
        q = lambda s: pd.read_sql(s, db)
        self.meta = dict(db.execute('SELECT key, value FROM db_meta').fetchall())
        self.steps = q('SELECT * FROM rte_route_step')
        self.qdefs = q('SELECT * FROM rte_qtime')
        self.ltl = q('SELECT * FROM rte_ltl_dedication')
        self.tool = q('SELECT * FROM eqp_tool')
        self.tg = q('SELECT * FROM eqp_toolgroup')
        self.rel = q('SELECT * FROM eqp_reliability')
        self.pm_plan = q('SELECT * FROM eqp_pm_plan')
        self.down = q('SELECT * FROM eqp_down_event')
        self.state = q('SELECT * FROM eqp_state_log')
        self.lot = q('SELECT * FROM mes_lot')
        self.step_ev = q('SELECT * FROM mes_lot_step')
        self.ded = q('SELECT d.*, l.route_id FROM mes_ltl_dedication d JOIN mes_lot l USING (lot_id)')
        ex = q("""SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM mes_lot_step
                  UNION SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM yms_process_history""")
        snap = q("""SELECT w.lot_id, l.route_id, w.entry_step_seq, w.entry_track_out_ts AS t_out
                    FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)""")
        # Q-time 구간 통과: 진입 종료 시각과 그 뒤 첫 종료 step 시작 시각(없으면 NaN). t에 열린 구간은 t_out <= t < t_in
        ent = self.qdefs.merge(ex.rename(columns={'step_seq': 'entry_step_seq', 'track_out_ts': 't_out'}),
                               on=['route_id', 'entry_step_seq'])[['lot_id', 'route_id', 'entry_step_seq',
                                                                    'exit_step_seq', 'qtime_limit_h', 't_out']]
        ent = ent[ent.t_out.notna()]
        snap = snap.merge(self.qdefs, on=['route_id', 'entry_step_seq'])[ent.columns]
        ent = pd.concat([ent, snap]).drop_duplicates(['lot_id', 'entry_step_seq', 't_out'])
        x = ex.rename(columns={'step_seq': 'exit_step_seq', 'track_in_ts': 't_in'})[['lot_id', 'exit_step_seq', 't_in']]
        p = ent.merge(x, on=['lot_id', 'exit_step_seq'], how='left')
        p.loc[~(p.t_in > p.t_out), 't_in'] = None
        self.qpass = p.groupby(['lot_id', 'route_id', 'entry_step_seq', 'exit_step_seq', 'qtime_limit_h', 't_out'],
                               dropna=False).t_in.min().reset_index()
        self.qpass['deadline'] = (pd.to_datetime(self.qpass.t_out)
                                  + pd.to_timedelta(self.qpass.qtime_limit_h, unit='h'))
        # 실행 시작 시각 색인 (dedication 대상 step의 시작 여부 판정용)
        self.started = ex[['lot_id', 'step_seq', 'track_in_ts']]
        # 기록 종료 시점에 대기 중인 lot. 이 lot들의 대기 step은 mes_lot_step에 행이 없다(행은 track-in 때 생긴다)
        self.wait_open = q("""SELECT w.lot_id, l.route_id, w.step_seq, w.operation, w.toolgroup, w.queue_in_ts
                              FROM mes_wait_open w JOIN mes_lot l USING (lot_id)""")
        db.close()

    def wip(self, t):
        """t에 공정 중인 lot과 현재 step. state: WAIT(대기), RUN(처리 중), MOVE(반송 중)."""
        s = self.step_ev
        w = s[(s.queue_in_ts <= t) & (s.next_arrival_ts.isna() | (s.next_arrival_ts > t))].copy()
        w['state'] = 'MOVE'
        w.loc[w.track_out_ts.isna() | (w.track_out_ts > t), 'state'] = 'RUN'
        w.loc[w.track_in_ts > t, 'state'] = 'WAIT'
        w.loc[w.state == 'WAIT', 'eqp_id'] = None
        # t에 대기 중이었고 기록 종료까지 투입되지 못한 lot (연구노트 U-13)
        o = self.wait_open[(self.wait_open.queue_in_ts <= t) & ~self.wait_open.lot_id.isin(w.lot_id)].copy()
        o['state'] = 'WAIT'
        w = pd.concat([w, o.reindex(columns=w.columns)], ignore_index=True)
        return w.merge(self.lot[['lot_id', 'lot_type', 'priority', 'wafer_qty']], on='lot_id')

    def tools(self, t):
        """t의 장비 상태(E10)와 정지 종류. 복구 시각은 주지 않는다."""
        st = self.state[(self.state.start_ts <= t) & (self.state.end_ts > t)][['eqp_id', 'state', 'reason']]
        dn = self.down[(self.down.start_ts <= t) & (self.down.end_ts > t)][['eqp_id', 'down_type', 'start_ts']]
        return (self.tool.merge(st, on='eqp_id', how='left')
                .merge(dn.rename(columns={'start_ts': 'down_start'}), on='eqp_id', how='left'))

    def dedications(self, t):
        """t에 유효한 LTL dedication: t 이전에 설정되었고 대상 step이 아직 시작되지 않은 것."""
        d = self.ded[self.ded.set_ts.isna() | (self.ded.set_ts <= t)]
        st = self.started[self.started.track_in_ts <= t][['lot_id', 'step_seq']].drop_duplicates()
        d = d.merge(st.rename(columns={'step_seq': 'target_step_seq'}), on=['lot_id', 'target_step_seq'],
                    how='left', indicator=True)
        d = d[d._merge == 'left_only'].drop(columns='_merge')
        done = self.lot[self.lot.complete_ts.notna() & (self.lot.complete_ts <= t)].lot_id
        return d[~d.lot_id.isin(done)]

    def qtime_open(self, t):
        """t에 열린 Q-time 구간. slack_h = 기한 - t (음수면 이미 초과)."""
        p = self.qpass
        o = p[(p.t_out <= t) & (p.t_in.isna() | (p.t_in > t))].copy()
        o['slack_h'] = (o.deadline - pd.Timestamp(t)).dt.total_seconds() / 3600
        return o.drop(columns=['t_in'])
