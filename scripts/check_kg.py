"""지식 그래프(SPARQL)가 원천 SQL DB(SQL)와 같은 답을 내는지 대조한다. 기획서 4.4 "두 구현이 일치한 문항만 채택"의 기반.

대조 항목
  1. 개체 수: 클래스별 인스턴스 수 = 원천 테이블 행 수 (StepExecution은 MES와 YMS 이력의 합집합)
  2. 속성 사슬: passedThroughTool 쌍 수 = (lot, 장비) 고유 쌍 수
  3. Q-time 위반: 기록 구간 안에서 종료 step에 진입한 구간 통과 수와 위반 수
  4. LTL 준수: dedication 대상 step 실행 수와 다른 장비 사용 수
  5. E10 상태 구간 수 (상태별)
  6. SHACL 위반 사실(validate_kg.py 결과, named graph): Q-time 위반 전체·기록 구간 안 종료·대기 중, 그 밖의 위반 0건
  7. commonality: 주입 정답의 (오퍼레이션, 장비) 경유 lot 수와 불량 lot 수 (정답 JSON이 있을 때)

예) python scripts/check_kg.py data/derived/db/lvhm_e_cr_s0.sqlite
    KG는 data/derived/kg/<DB 이름>/store. 결과는 같은 폴더의 check_report.txt. 하나라도 다르면 종료 코드 1
"""
import argparse
import glob
import json
import os
import sqlite3
import sys

import pyoxigraph as ox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PFX = """PREFIX fab: <https://w3id.org/ontosemc/fab#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""
D = 'https://w3id.org/ontosemc/data/'
PAT = {'Center': 'Center', 'Donut': 'Donut', 'Edge-Loc': 'EdgeLoc', 'Edge-Ring': 'EdgeRing', 'Loc': 'Loc',
       'Near-full': 'NearFull', 'Random': 'Random', 'Scratch': 'Scratch'}


def sparql(store, q):
    rows = list(store.query(PFX + q))
    return [tuple(None if v is None else (int(v.value) if v.value.lstrip('-').isdigit() else v.value) for v in r)
            for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    a = ap.parse_args()
    db_path = a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db)
    name = os.path.splitext(os.path.basename(db_path))[0]
    kg_dir = os.path.join(ROOT, 'data', 'derived', 'kg', name)
    store = ox.Store(os.path.join(kg_dir, 'store'))
    db = sqlite3.connect(db_path)
    sql = lambda q: db.execute(q).fetchall()
    # MES와 YMS 이력의 합집합. KG에서는 같은 실행이 한 개체로 합쳐진다
    db.executescript("""
        CREATE TEMP TABLE ex AS
          SELECT lot_id, route_id, step_seq, eqp_id, track_in_ts, track_out_ts FROM mes_lot_step
          UNION SELECT lot_id, route_id, step_seq, eqp_id, track_in_ts, track_out_ts FROM yms_process_history;
        CREATE INDEX temp.ix_ex ON ex (lot_id, step_seq, track_in_ts);""")
    w0, w1 = [r[0] for r in sql("SELECT value FROM db_meta WHERE key IN ('window_start_ts', 'window_end_ts') ORDER BY key DESC")]
    rows, fail = [], []

    def cmp(label, s, k):
        ok = s == k
        rows.append(f"{'OK ' if ok else 'DIFF'} {label:44s} SQL {str(s):>22s}   SPARQL {str(k):>22s}")
        if not ok:
            fail.append(label)

    # 1. 개체 수
    for cls, q in [('Lot', 'SELECT COUNT(*) FROM mes_lot'),
                   ('StepExecution', 'SELECT COUNT(*) FROM (SELECT DISTINCT lot_id, step_seq, track_in_ts FROM ex)'),
                   ('Tool', 'SELECT COUNT(*) FROM eqp_tool'),
                   ('ToolGroup', 'SELECT COUNT(*) FROM eqp_toolgroup WHERE is_delay_station = 0'),
                   ('DelayStation', 'SELECT COUNT(*) FROM eqp_toolgroup WHERE is_delay_station = 1'),
                   ('Bay', 'SELECT COUNT(*) FROM eqp_bay'),
                   ('ProcessStep', 'SELECT COUNT(*) FROM rte_route_step'),
                   ('Operation', 'SELECT COUNT(DISTINCT operation) FROM rte_route_step'),
                   ('QTimeConstraint', 'SELECT COUNT(*) FROM rte_qtime'),
                   ('DownEvent', 'SELECT COUNT(*) FROM eqp_down_event'),
                   ('EquipmentStateInterval', 'SELECT COUNT(*) FROM eqp_state_log'),
                   ('PMPlan', 'SELECT COUNT(*) FROM eqp_pm_plan'),
                   ('LtlDedication', 'SELECT COUNT(*) FROM mes_ltl_dedication'),
                   ('SamplingSkip', 'SELECT COUNT(*) FROM (SELECT DISTINCT lot_id, step_seq, skip_ts FROM mes_sampling_skip)'),
                   ('WipSnapshotEntry', 'SELECT COUNT(*) FROM mes_wip_snapshot'),
                   ('QTimeSnapshotEntry', 'SELECT COUNT(*) FROM mes_qtime_snapshot'),
                   ('OpenWait', 'SELECT COUNT(*) FROM mes_wait_open'),
                   ('Wafer', 'SELECT COUNT(*) FROM yms_wafer_sort')]:
        cmp(f'instances {cls}', sql(q)[0][0], sparql(store, f'SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?x a fab:{cls} }}')[0][0])

    cmp('wafers with a defect pattern', sql("SELECT COUNT(*) FROM yms_wafer_sort WHERE defect_pattern <> 'none'")[0][0],
        sparql(store, 'SELECT (COUNT(*) AS ?n) WHERE { ?w fab:exhibits ?p }')[0][0])
    # SQL NULL이 문자열 'None'으로 매핑되면 tool/None 같은 가짜 개체가 생긴다(morph-kgc na_values)
    cmp("mapping artifacts ('None' terms)", 0, sparql(store, """SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o
          FILTER (STRENDS(STR(?o), "/None") || STRENDS(STR(?o), "#None") || STR(?o) = "None" || STRENDS(STR(?s), "/None")) }""")[0][0])

    # 2. 속성 사슬
    cmp('chain passedThroughTool (lot, tool)', sql('SELECT COUNT(*) FROM (SELECT DISTINCT lot_id, eqp_id FROM ex WHERE eqp_id IS NOT NULL)')[0][0],
        sparql(store, 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughTool ?t }')[0][0])
    cmp('chain passedThroughBay (lot, bay)', sql('SELECT COUNT(*) FROM (SELECT DISTINCT e.lot_id, t.bay_id FROM ex e JOIN eqp_tool t USING (eqp_id))')[0][0],
        sparql(store, 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughBay ?b }')[0][0])

    # 3. Q-time 위반: 종료 step 진입이 기록 구간 안. 진입 시각은 실행(MES, YMS) 또는 기록 시작 스냅샷
    s = sql(f"""
        WITH entry AS (
          SELECT e.lot_id, q.exit_step_seq, q.qtime_limit_h, e.track_out_ts AS t_out
          FROM ex e JOIN rte_qtime q ON q.route_id = e.route_id AND q.entry_step_seq = e.step_seq
          WHERE e.track_out_ts IS NOT NULL
          UNION
          SELECT w.lot_id, q.exit_step_seq, q.qtime_limit_h, w.entry_track_out_ts
          FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)
          JOIN rte_qtime q ON q.route_id = l.route_id AND q.entry_step_seq = w.entry_step_seq),
        pass AS (SELECT entry.*, (SELECT MIN(x.track_in_ts) FROM ex x WHERE x.lot_id = entry.lot_id
                 AND x.step_seq = entry.exit_step_seq AND x.track_in_ts > entry.t_out) AS t_in FROM entry)
        SELECT COUNT(*), SUM((julianday(t_in) - julianday(t_out)) * 24 > qtime_limit_h)
        FROM pass WHERE t_in >= '{w0}' AND t_in <= '{w1}'""")[0]
    k = sparql(store, """
        SELECT (COUNT(*) AS ?n) (SUM(IF(?t1 > ?deadline, 1, 0)) AS ?v) WHERE {
          { SELECT ?lot ?q ?tout (MIN(?tin) AS ?t1) WHERE {
              { ?q fab:entryStep ?es . ?e fab:atStep ?es ; fab:ofLot ?lot ; fab:trackOutTime ?tout . }
              UNION
              { ?s fab:openConstraint ?q ; fab:snapshotLot ?lot ; fab:entryTrackOutTime ?tout . }
              ?q fab:exitStep ?xs .
              ?x fab:atStep ?xs ; fab:ofLot ?lot ; fab:trackInTime ?tin .
              FILTER (?tin > ?tout)
            } GROUP BY ?lot ?q ?tout }
          ?q fab:maxHours ?max .
          ?f fab:windowStart ?w0 ; fab:windowEnd ?w1 .
          FILTER (?t1 >= ?w0 && ?t1 <= ?w1)
          BIND (?tout + STRDT(CONCAT("PT", STR(xsd:integer(?max)), "H"), xsd:dayTimeDuration) AS ?deadline)
        }""")[0]
    cmp('Q-time passes in window', s[0], k[0])
    cmp('Q-time violations in window', s[1], k[1])

    # 4. LTL 준수
    s = sql("""SELECT COUNT(*), SUM(e.eqp_id <> d.eqp_id) FROM mes_ltl_dedication d
               JOIN ex e ON e.lot_id = d.lot_id AND e.step_seq = d.target_step_seq""")[0]
    k = sparql(store, """SELECT (COUNT(*) AS ?n) (SUM(IF(?used != ?tool, 1, 0)) AS ?bad) WHERE {
          ?d fab:dedicatedLot ?lot ; fab:dedicatedStep ?st ; fab:dedicatedTool ?tool .
          ?x fab:ofLot ?lot ; fab:atStep ?st ; fab:processedOn ?used }""")[0]
    cmp('LTL target executions', s[0], k[0])
    cmp('LTL executions on another tool', s[1], k[1])

    # 5. E10 상태 구간 수
    sd = dict(sql('SELECT state, COUNT(*) FROM eqp_state_log GROUP BY state'))
    kd = {st.rsplit('#', 1)[-1]: n for st, n in sparql(store, """SELECT ?s (COUNT(?i) AS ?n) WHERE {
          ?i a fab:EquipmentStateInterval ; fab:e10State ?s } GROUP BY ?s""")}
    for st in sorted(sd):
        cmp(f'E10 intervals {st}', sd[st], kd.get(st))

    # 5b. SHACL 위반 사실(validate_kg.py, named graph) 대 SQL. 위반 그래프가 있을 때만
    vg = '<https://w3id.org/ontosemc/graph/violations>'
    if sparql(store, f'SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH {vg} {{ ?s ?p ?o }} }}')[0][0]:
        s = sql(f"""
            WITH entry AS (
              SELECT e.lot_id, q.exit_step_seq, q.qtime_limit_h, e.track_out_ts AS t_out
              FROM ex e JOIN rte_qtime q ON q.route_id = e.route_id AND q.entry_step_seq = e.step_seq
              WHERE e.track_out_ts IS NOT NULL
              UNION
              SELECT w.lot_id, q.exit_step_seq, q.qtime_limit_h, w.entry_track_out_ts
              FROM mes_qtime_snapshot w JOIN mes_lot l USING (lot_id)
              JOIN rte_qtime q ON q.route_id = l.route_id AND q.entry_step_seq = w.entry_step_seq),
            pass AS (SELECT entry.*, (SELECT MIN(x.track_in_ts) FROM ex x WHERE x.lot_id = entry.lot_id
                     AND x.step_seq = entry.exit_step_seq AND x.track_in_ts > entry.t_out) AS t_in FROM entry),
            v AS (SELECT * FROM pass WHERE (t_in IS NOT NULL AND (julianday(t_in) - julianday(t_out)) * 24 > qtime_limit_h)
                                        OR (t_in IS NULL AND (julianday('{w1}') - julianday(t_out)) * 24 > qtime_limit_h))
            SELECT COUNT(*), SUM(t_in >= '{w0}' AND t_in <= '{w1}'), SUM(t_in IS NULL) FROM v""")[0]
        k = sparql(store, f"""SELECT (COUNT(?v) AS ?n) (SUM(?inw) AS ?w) (SUM(?wait) AS ?s) WHERE {{
              GRAPH {vg} {{ ?v a fab:QTimeViolation ; fab:stillWaiting ?sw }}
              # 종료 실행이 없는 위반(대기 중)에서 ?x가 비면 trackInTime 전체와 곱해진다. 한 OPTIONAL 안에서 잇는다
              OPTIONAL {{ GRAPH {vg} {{ ?v fab:exitExecution ?x }} ?x fab:trackInTime ?tin }}
              ?f fab:windowStart ?w0 ; fab:windowEnd ?w1 .
              BIND (IF(BOUND(?tin) && ?tin >= ?w0 && ?tin <= ?w1, 1, 0) AS ?inw)
              BIND (IF(?sw, 1, 0) AS ?wait) }}""")[0]
        cmp('SHACL Q-time violations (all history)', s[0], k[0])
        cmp('SHACL Q-time violations, exit in window', s[1], k[1])
        cmp('SHACL Q-time violations, still waiting', s[2], k[2])
        for cls in ['LtlViolation', 'QualificationViolation', 'AvailabilityViolation', 'IntegrityViolation']:
            cmp(f'SHACL {cls}', 0, sparql(store, f'SELECT (COUNT(?v) AS ?n) WHERE {{ GRAPH {vg} {{ ?v a fab:{cls} }} }}')[0][0])

    # 6. commonality 정답 쌍 (정답 JSON이 있으면)
    for gt_path in sorted(glob.glob(os.path.join(ROOT, 'data', 'derived', 'gt', f'{name}_defect_s*.json'))):
        gt = json.load(open(gt_path, encoding='utf-8'))
        for e in gt['excursions']:
            op, tool, pat = D + 'operation/' + e['operation'], D + 'tool/' + e['eqp_id'], PAT[e['pattern']]
            n = sparql(store, f"""SELECT (COUNT(DISTINCT ?lot) AS ?n) WHERE {{
                  ?x fab:executedOperation <{op}> ; fab:processedOn <{tool}> ; fab:ofLot ?lot .
                  FILTER EXISTS {{ ?lot fab:hasWafer ?w }} }}""")[0][0]
            nb = sparql(store, f"""SELECT (COUNT(DISTINCT ?lot) AS ?n) WHERE {{
                  ?x fab:executedOperation <{op}> ; fab:processedOn <{tool}> ; fab:ofLot ?lot .
                  {{ SELECT ?lot WHERE {{ ?lot fab:hasWafer ?w . ?w fab:exhibits fab:{pat} }}
                    GROUP BY ?lot HAVING (COUNT(?w) >= 3) }} }}""")[0][0]
            g = e['diagnosis']['gt_pair']
            tag = f"{os.path.basename(gt_path)[:-5].split('_')[-1]} {e['id']}"
            cmp(f'commonality {tag} lots via (op, tool)', g['n_lot'], n)
            cmp(f'commonality {tag} bad lots via (op, tool)', g['n_bad'], nb)

    text = '\n'.join([f'KG: {os.path.relpath(kg_dir, ROOT)}  vs  DB: {os.path.relpath(db_path, ROOT)}', ''] + rows +
                     ['', '판정: ' + ('일치' if not fail else '불일치 - ' + ', '.join(fail))])
    print(text)
    with open(os.path.join(kg_dir, 'check_report.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
