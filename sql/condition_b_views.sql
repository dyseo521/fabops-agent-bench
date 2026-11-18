-- 조건 B (SQL + 동등 뷰). 기획서 v3 7.1, 3장 "사전 계산의 이득을 온톨로지 이득으로 귀속" 차단
-- KG가 materialize한 관계와 같은 내용을 SQL 뷰로 준다. B 대 C의 차이만 온톨로지 표현의 효과로 본다
--
-- KG 대응
--   v_step_execution          fab:StepExecution (MES 기록 구간 + YMS 완료 lot 이력, 같은 실행은 하나로)
--   v_lot_tool                fab:passedThroughTool       = hasEvent / processedOn
--   v_lot_toolgroup           fab:passedThroughToolGroup  = hasEvent / processedOn / memberOf
--   v_lot_bay                 fab:passedThroughBay        = hasEvent / processedOn / locatedIn
--   v_lot_operation           fab:passedThroughOperation  = hasEvent / atStep / ofOperation
--   v_step_execution.operation  fab:executedOperation     = atStep / ofOperation
--   v_ltl_dedication_closure  fab:ltlDedicatedTo 전이 폐포
-- 역관계(hasEvent/ofLot 등)는 SQL 조인이 양방향이라 뷰가 필요 없다
-- 제약 위반 사실(조건 D의 get_violations)은 넣지 않는다

CREATE VIEW v_step_execution AS
SELECT lot_id, route_id, step_seq, operation, toolgroup, eqp_id, run_id, run_lot_count,
       queue_in_ts, track_in_ts, setup_min, track_out_ts, next_arrival_ts
FROM mes_lot_step
UNION ALL
SELECT y.lot_id, y.route_id, y.step_seq, y.operation, y.toolgroup, y.eqp_id, NULL, NULL,
       NULL, y.track_in_ts, NULL, y.track_out_ts, NULL
FROM yms_process_history y
WHERE NOT EXISTS (SELECT 1 FROM mes_lot_step m
                  WHERE m.lot_id = y.lot_id AND m.step_seq = y.step_seq AND m.track_in_ts = y.track_in_ts);

CREATE VIEW v_lot_tool AS
SELECT DISTINCT lot_id, eqp_id FROM v_step_execution WHERE eqp_id IS NOT NULL;

CREATE VIEW v_lot_toolgroup AS
SELECT DISTINCT e.lot_id, t.toolgroup FROM v_step_execution e JOIN eqp_tool t USING (eqp_id);

CREATE VIEW v_lot_bay AS
SELECT DISTINCT e.lot_id, t.bay_id FROM v_step_execution e JOIN eqp_tool t USING (eqp_id);

CREATE VIEW v_lot_operation AS
SELECT DISTINCT lot_id, operation FROM v_step_execution;

CREATE VIEW v_ltl_dedication_closure AS
WITH RECURSIVE c(route_id, source_step_seq, target_step_seq) AS (
  SELECT route_id, source_step_seq, target_step_seq FROM rte_ltl_dedication
  UNION
  SELECT c.route_id, c.source_step_seq, r.target_step_seq
  FROM c JOIN rte_ltl_dedication r ON r.route_id = c.route_id AND r.source_step_seq = c.target_step_seq)
SELECT route_id, source_step_seq, target_step_seq FROM c;
