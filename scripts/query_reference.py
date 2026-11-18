"""조회 계층 정답의 참조 구현 두 벌(pandas, SQL)과 교차 대조. 기획서 4.3(조회 30문항), 4.4.

조회는 값이나 결과 집합을 묻는다. 복잡도는 조인 수와 시스템 경계 수로 나눈다(기획서 4.3).
  L1  tool_lot_count   장비가 하루 동안 투입(track-in)한 lot-step 수            MES 1개 테이블
      tool_down_hours  장비의 하루 정지 시간(UDT + SDT)                       EQP 1개 테이블
  L2  tg_tool_counts   장비군의 장비별 하루 투입 수(결과 집합)                 MES x EQP
      bay_lot_count    bay에서 하루 동안 투입된 서로 다른 lot 수               MES x EQP(장비 -> bay)
  L3  pattern_bay      불량 패턴 lot(해당 패턴 웨이퍼 3장 이상)이 지정 오퍼레이션에서 거친 bay별 lot 수   YMS x 공정 이력 x EQP
      product_qtime    종료 step의 area가 지정 area인 Q-time 위반의 제품별 건수 MES x 라우트·제약 x lot 마스터
  보류 sampling_skip   sampling으로 건너뛴 lot-step의 처리 기록 조회 -> 기록 없음(DATA_ABSENT)
스키마 밖 보류 문항(레티클 ID, 작업자, 챔버 압력 등)은 데이터가 필요 없어 여기서 만들지 않는다.

결과는 data/derived/profile/<이름>_query_reference_check.json.
예) python scripts/query_reference.py lvhm_e_cr_s0 lvhm_e_cr_s0_w75 lvhm_e_cr_s1
"""
import argparse
import json
import os
import sqlite3
import sys

import pandas as pd

import diag_alternatives as DA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, 'data', 'derived')
BAD_MIN = 3


def day_bounds(meta, d):
    w0 = pd.Timestamp(meta['window_start_ts'])
    f = lambda x: x.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
    return f(w0 + pd.Timedelta(days=d)), f(w0 + pd.Timedelta(days=d + 1))


class Pandas:
    def __init__(self, db, meta):
        q = lambda s: pd.read_sql(s, db)
        self.meta = meta
        self.step = q('SELECT lot_id, eqp_id, toolgroup, track_in_ts FROM mes_lot_step WHERE eqp_id IS NOT NULL')
        self.tool = q('SELECT eqp_id, toolgroup, bay_id FROM eqp_tool')
        self.state = q('SELECT eqp_id, state, start_ts, end_ts FROM eqp_state_log')
        self.sort = q('SELECT lot_id, defect_pattern FROM yms_wafer_sort')
        self.hist = q('SELECT lot_id, operation, eqp_id FROM yms_process_history')
        self.lot = q('SELECT lot_id, product_id FROM mes_lot')
        self.rstep = q('SELECT route_id, step_seq, area FROM rte_route_step')
        self.execs = q('SELECT lot_id, step_seq FROM mes_lot_step UNION ALL SELECT lot_id, step_seq FROM yms_process_history')

    def _day(self, d):
        a, b = day_bounds(self.meta, d)
        return self.step[(self.step.track_in_ts >= a) & (self.step.track_in_ts < b)]

    def tool_lot_count(self, eqp, d):
        return int((self._day(d).eqp_id == eqp).sum())

    def tool_down_hours(self, eqp, d):
        a, b = (pd.Timestamp(x) for x in day_bounds(self.meta, d))
        s = self.state[(self.state.eqp_id == eqp) & self.state.state.isin(['UDT', 'SDT'])]
        ov = (pd.to_datetime(s.end_ts).clip(upper=b) - pd.to_datetime(s.start_ts).clip(lower=a)).dt.total_seconds()
        return round(float(ov[ov > 0].sum()) / 3600, 2)

    def tg_tool_counts(self, tg, d):
        x = self._day(d)
        return {k: int(v) for k, v in x[x.toolgroup == tg].groupby('eqp_id').size().items()}

    def bay_lot_count(self, bay, d):
        x = self._day(d).merge(self.tool[['eqp_id', 'bay_id']], on='eqp_id')
        return int(x[x.bay_id == bay].lot_id.nunique())

    def pattern_bay(self, pattern, operation):
        c = self.sort[self.sort.defect_pattern == pattern].groupby('lot_id').size()
        bad = set(c[c >= BAD_MIN].index)
        h = self.hist[(self.hist.operation == operation) & self.hist.lot_id.isin(bad)].merge(self.tool[['eqp_id', 'bay_id']], on='eqp_id')
        return {k: int(v) for k, v in h.groupby('bay_id').lot_id.nunique().items()}

    def product_qtime(self, area, V):
        v = V.merge(self.rstep, left_on=['route_id', 'exit_step_seq'], right_on=['route_id', 'step_seq']).merge(self.lot, on='lot_id')
        return {k: int(n) for k, n in v[v.area == area].groupby('product_id').size().items()}

    def sampling_skip(self, lot, step_seq):
        """건너뛴 lot-step의 처리 기록 수. 0이어야 보류 문항이 된다."""
        return int(((self.execs.lot_id == lot) & (self.execs.step_seq == step_seq)).sum())


SQL = dict(
    tool_lot_count='SELECT COUNT(*) FROM mes_lot_step WHERE eqp_id = :eqp AND track_in_ts >= :a AND track_in_ts < :b',
    tool_down_hours="""SELECT COALESCE(SUM((MIN(julianday(end_ts), julianday(:b)) - MAX(julianday(start_ts), julianday(:a))) * 24), 0)
                       FROM eqp_state_log WHERE eqp_id = :eqp AND state IN ('UDT', 'SDT') AND end_ts > :a AND start_ts < :b""",
    tg_tool_counts="""SELECT eqp_id, COUNT(*) FROM mes_lot_step WHERE toolgroup = :tg AND eqp_id IS NOT NULL
                      AND track_in_ts >= :a AND track_in_ts < :b GROUP BY eqp_id""",
    bay_lot_count="""SELECT COUNT(DISTINCT s.lot_id) FROM mes_lot_step s JOIN eqp_tool t USING (eqp_id)
                     WHERE t.bay_id = :bay AND s.track_in_ts >= :a AND s.track_in_ts < :b""",
    pattern_bay="""SELECT t.bay_id, COUNT(DISTINCT h.lot_id) FROM yms_process_history h JOIN eqp_tool t USING (eqp_id)
                   WHERE h.operation = :op AND h.lot_id IN (SELECT lot_id FROM yms_wafer_sort WHERE defect_pattern = :pattern
                                                            GROUP BY lot_id HAVING COUNT(*) >= :bad_min)
                   GROUP BY t.bay_id""",
    product_qtime="""
        WITH ex AS (SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM mes_lot_step WHERE track_in_ts IS NOT NULL
                    UNION SELECT lot_id, route_id, step_seq, track_in_ts, track_out_ts FROM yms_process_history WHERE track_in_ts IS NOT NULL),
        entry AS (SELECT e.lot_id, q.route_id, q.entry_step_seq, q.exit_step_seq, q.qtime_limit_h, e.track_out_ts AS t_out
                  FROM ex e JOIN rte_qtime q ON q.route_id = e.route_id AND q.entry_step_seq = e.step_seq WHERE e.track_out_ts IS NOT NULL
                  UNION
                  SELECT w.lot_id, l.route_id, w.entry_step_seq, w.exit_step_seq, q.qtime_limit_h, w.entry_track_out_ts
                  FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)
                  JOIN rte_qtime q ON q.route_id = l.route_id AND q.entry_step_seq = w.entry_step_seq AND q.exit_step_seq = w.exit_step_seq),
        pass AS (SELECT entry.*, (SELECT MIN(x.track_in_ts) FROM ex x WHERE x.lot_id = entry.lot_id AND x.step_seq = entry.exit_step_seq
                                  AND x.track_in_ts > entry.t_out) AS t_in FROM entry)
        SELECT l.product_id, COUNT(*) FROM pass p JOIN mes_lot l USING (lot_id)
        JOIN rte_route_step s ON s.route_id = p.route_id AND s.step_seq = p.exit_step_seq
        WHERE p.t_in IS NOT NULL AND (julianday(p.t_in) - julianday(p.t_out)) * 24 > p.qtime_limit_h
          AND p.t_in >= :w0 AND p.t_in <= :w1 AND s.area = :area GROUP BY l.product_id""",
    sampling_skip="""SELECT (SELECT COUNT(*) FROM mes_lot_step WHERE lot_id = :lot AND step_seq = :seq)
                          + (SELECT COUNT(*) FROM yms_process_history WHERE lot_id = :lot AND step_seq = :seq)""",
)


def candidates(db, meta, days):
    """유형별 후보 개체. 답이 비지 않는 것만 낸다."""
    q = lambda s, p=(): db.execute(s, p).fetchall()
    w0, w1 = day_bounds(meta, 0)[0], meta['window_end_ts']
    C = dict(tool_lot_count=[], tool_down_hours=[], tg_tool_counts=[], bay_lot_count=[], pattern_bay=[], product_qtime=[], sampling_skip=[])
    for d in range(days):
        a, b = day_bounds(meta, d)
        C['tool_lot_count'] += [dict(eqp=e, d=d) for e, in q('SELECT DISTINCT eqp_id FROM mes_lot_step WHERE eqp_id IS NOT NULL AND track_in_ts >= ? AND track_in_ts < ? ORDER BY 1', (a, b))]
        C['tool_down_hours'] += [dict(eqp=e, d=d) for e, in q("SELECT DISTINCT eqp_id FROM eqp_state_log WHERE state IN ('UDT', 'SDT') AND end_ts > ? AND start_ts < ? ORDER BY 1", (a, b))]
        C['tg_tool_counts'] += [dict(tg=g, d=d) for g, in q('SELECT DISTINCT toolgroup FROM mes_lot_step WHERE eqp_id IS NOT NULL AND track_in_ts >= ? AND track_in_ts < ? ORDER BY 1', (a, b))]
        C['bay_lot_count'] += [dict(bay=x, d=d) for x, in q('SELECT DISTINCT bay_id FROM eqp_tool WHERE bay_id IS NOT NULL ORDER BY 1')]
    pats = [p for p, in q("SELECT defect_pattern FROM (SELECT lot_id, defect_pattern FROM yms_wafer_sort WHERE defect_pattern <> 'none' GROUP BY 1, 2 HAVING COUNT(*) >= ?) GROUP BY 1 HAVING COUNT(*) >= 5 ORDER BY 1", (BAD_MIN,))]
    ops = [o for o, in q('SELECT operation FROM yms_process_history GROUP BY 1 HAVING COUNT(DISTINCT eqp_id) >= 4 ORDER BY 1')]
    C['pattern_bay'] = [dict(pattern=p, op=o) for p in pats for o in ops[::7]]
    C['product_qtime'] = [dict(area=x) for x, in q('SELECT DISTINCT area FROM rte_route_step WHERE area IS NOT NULL ORDER BY 1')]
    C['sampling_skip'] = [dict(lot=l, seq=s, op=o) for l, s, o in q('SELECT lot_id, step_seq, operation FROM mes_sampling_skip WHERE skip_ts >= ? AND skip_ts <= ? ORDER BY 1, 2', (w0, w1))][::40]
    return C


def answer_pandas(P, V, kind, c):
    if kind == 'tool_lot_count':
        return P.tool_lot_count(c['eqp'], c['d'])
    if kind == 'tool_down_hours':
        return P.tool_down_hours(c['eqp'], c['d'])
    if kind == 'tg_tool_counts':
        return P.tg_tool_counts(c['tg'], c['d'])
    if kind == 'bay_lot_count':
        return P.bay_lot_count(c['bay'], c['d'])
    if kind == 'pattern_bay':
        return P.pattern_bay(c['pattern'], c['op'])
    if kind == 'product_qtime':
        return P.product_qtime(c['area'], V)
    return P.sampling_skip(c['lot'], c['seq'])


def answer_sql(db, meta, kind, c):
    p = dict(c)
    if 'd' in c:
        p['a'], p['b'] = day_bounds(meta, c['d'])
    p.update(w0=meta['window_start_ts'], w1=meta['window_end_ts'], bad_min=BAD_MIN)
    rows = db.execute(SQL[kind], p).fetchall()
    if kind in ('tg_tool_counts', 'pattern_bay', 'product_qtime'):
        return {k: int(v) for k, v in rows}
    return round(rows[0][0], 2) if kind == 'tool_down_hours' else int(rows[0][0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('names', nargs='+')
    ap.add_argument('--per-kind', type=int, default=150, help='유형별 대조 건수 상한')
    a = ap.parse_args()
    all_ok = True
    for name in a.names:
        db = sqlite3.connect(os.path.join(DERIVED, 'db', name + '.sqlite'))
        D = DA.load(name)
        meta = D['meta']
        days = int(round((D['w1'] - D['w0']) / 86400))
        P, V = Pandas(db, meta), DA.violations(D)
        C = candidates(db, meta, days)
        rep = dict(name=name)
        ok = True
        for kind, cs in C.items():
            step = max(1, len(cs) // a.per_kind)
            sub = cs[::step][:a.per_kind]
            diff = empty = 0
            for c in sub:
                x, y = answer_pandas(P, V, kind, c), answer_sql(db, meta, kind, c)
                # 두 구현이 소수 둘째 자리 반올림에서 갈리면 차이가 0.01이 된다(예: 0.625시간 -> 0.62와 0.63).
                # 부동소수점에서 abs(0.62 - 0.63)은 0.01보다 조금 크므로 여유를 둔다. 채점 허용 오차는 0.05다(D-66)
                same = abs(x - y) <= 0.01 + 1e-9 if isinstance(x, float) else x == y
                diff += not same
                empty += (not x)
            rep[kind] = dict(candidates=len(cs), checked=len(sub), mismatch=diff, empty_answers=empty)
            ok = ok and diff == 0
        rep['verdict'] = '일치' if ok else '불일치'
        all_ok = all_ok and ok
        with open(os.path.join(DERIVED, 'profile', name + '_query_reference_check.json'), 'w', encoding='utf-8') as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(json.dumps(rep, ensure_ascii=False))
    sys.exit(0 if all_ok else 1)      # 불일치가 있으면 뒤 단계가 이어지지 않게 한다


if __name__ == '__main__':
    main()
