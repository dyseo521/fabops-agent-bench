"""조치 게이트: 제안 조치를 SHACL(ontology/shapes_gate.ttl)로 검증한다. 기획서 v3 1.4 "조치 게이트", 6.1 validate_action.

데이터 그래프 = 시나리오 시각 t 기준 문맥 + 제안 조치(RDF). 문맥은 KG(pyoxigraph)에서만 뽑는다.
  공통      장비 타입과 소속 toolgroup (초기화 때 1회)
  정지      인시던트 장비와 조치에 나온 장비의 정지 구간 중 t에 걸치거나 t 뒤 48시간 안에 시작하는 것
  lot       조치 대상 lot(과 dedication 해제 요청 장비에 전용된 lot)의 실행, dedication, Q-time 스냅샷
  라우트    그 lot들의 라우트 step(번호, toolgroup)과 Q-time 정의
  시나리오  t, 인시던트 장비·정지, 예상 정지 시간(고장: area MTTR. PM: 같은 toolgroup·같은 trigger PM 계획의
            평균 소요, 없으면 toolgroup 전체 PM 계획 평균)
문맥 추출은 트리플 패턴 조회(quads_for_pattern)로 한다. SPARQL CONSTRUCT(VALUES + UNION + OPTIONAL)는 Oxigraph 0.3에서
조치 1건에 8~13초가 걸렸다. 패턴 조회로 바꾼 뒤 수치는 ontology/README.md 8절.

오라클(oracle.py, 원천 DB 위 pandas)과 코드·질의·데이터 경로를 공유하지 않는다. 시나리오에서는 식별자와 시각만 받는다
(sc['id'], sc['time'], sc['eqp_id'], sc['down_id']).

조치 형식(JSON): type, lot, tool, to_tool, priority, to, request, defer_hours (oracle.py 출력과 같음)
"""
import os
import time
from datetime import datetime, timedelta

import pyoxigraph as ox
import rdflib
from pyshacl import validate
from rdflib.namespace import RDF, XSD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAB = rdflib.Namespace('https://w3id.org/ontosemc/fab#')
SH = rdflib.Namespace('http://www.w3.org/ns/shacl#')
D = 'https://w3id.org/ontosemc/data/'
PM_HORIZON_H = 48
DG = ox.DefaultGraph()


def _n(name):
    return ox.NamedNode(str(FAB[name]))


TYPE = ox.NamedNode(str(RDF.type))
EXEC_P = {str(FAB[p]) for p in ('ofLot', 'atStep', 'queueInTime', 'trackInTime', 'trackOutTime')}
LOT_P = {str(FAB[p]) for p in ('priority', 'followsRoute', 'completeTime')}
DED_P = {str(FAB[p]) for p in ('dedicatedLot', 'dedicatedStep', 'dedicatedTool', 'eventTime')}
SNAP_P = {str(FAB[p]) for p in ('snapshotLot', 'openConstraint', 'entryTrackOutTime')}
OPENW_P = {str(FAB[p]) for p in ('waitLot', 'waitStep', 'waitSince')}
STEP_P = {str(FAB[p]) for p in ('onRoute', 'stepSeq', 'requiresToolGroup')}
QT_P = {str(FAB[p]) for p in ('entryStep', 'exitStep', 'maxHours')}
DOWN_T = {str(FAB.Breakdown), str(FAB.PreventiveMaintenance)}


def _rd(t):
    if isinstance(t, ox.NamedNode):
        return rdflib.URIRef(t.value)
    if isinstance(t, ox.BlankNode):
        return rdflib.BNode(t.value)
    return rdflib.Literal(t.value, lang=t.language) if t.language else \
        rdflib.Literal(t.value, datatype=rdflib.URIRef(t.datatype.value))


def _dt(s):
    """xsd:dateTime 어휘형 -> datetime. KG의 소수부는 자릿수가 줄어 있을 수 있다."""
    s = str(s).replace(' ', 'T')
    if '.' in s:
        head, frac = s.split('.')
        s = f'{head}.{(frac + "000000")[:6]}'
    return datetime.fromisoformat(s)


class Gate:
    def __init__(self, kg_dir):
        self.store = ox.Store.read_only(os.path.join(kg_dir, 'store'))
        self.shapes = rdflib.Graph().parse(os.path.join(ROOT, 'ontology', 'shapes_gate.ttl'))
        self.static = []
        for q in self.store.quads_for_pattern(None, TYPE, _n('Tool'), DG):
            self.static.append((_rd(q.subject), RDF.type, FAB.Tool))
            self.static += self._node(q.subject, {str(FAB.memberOf)})
        self._routes = {}
        self.last_timing = {}

    # ---- KG 조회 ---------------------------------------------------------
    def _node(self, s, preds):
        """주어 s의 트리플 중 술어가 preds에 있는 것."""
        return [(_rd(q.subject), _rd(q.predicate), _rd(q.object))
                for q in self.store.quads_for_pattern(s, None, None, DG) if q.predicate.value in preds]

    def _subjects(self, pred, obj):
        return [q.subject for q in self.store.quads_for_pattern(None, _n(pred), obj, DG)]

    def _value(self, s, pred):
        return next((q.object for q in self.store.quads_for_pattern(s, _n(pred), None, DG)), None)

    def _has_type(self, s, cls):
        return next(iter(self.store.quads_for_pattern(s, TYPE, _n(cls), DG)), None) is not None

    def _lot(self, lot):
        L = ox.NamedNode(lot)
        out = self._node(L, LOT_P)
        if self._has_type(L, 'Lot'):
            out.append((_rd(L), RDF.type, FAB.Lot))
        for x in self._subjects('ofLot', L):
            out += self._node(x, EXEC_P)
        for d in self._subjects('dedicatedLot', L):
            out += self._node(d, DED_P)
        for ow in self._subjects('waitLot', L):   # 기록 종료 시점에 대기 중인 lot (StepExecution이 없는 대기)
            out += self._node(ow, OPENW_P)
        for sn in self._subjects('snapshotLot', L):
            if sn.value.startswith(D + 'qtsnap/'):
                out += self._node(sn, SNAP_P)
        return out

    def _route(self, route):
        if route not in self._routes:
            out = []
            for st in self._subjects('onRoute', ox.NamedNode(route)):
                out += self._node(st, STEP_P)
                for q in self._subjects('entryStep', st):
                    out += self._node(q, QT_P)
            self._routes[route] = out
        return self._routes[route]

    def _downs(self, tool, t):
        """장비의 정지 구간 중 t에 걸치거나 t 뒤 PM_HORIZON_H 안에 시작하는 것."""
        out, t0, t1 = [], _dt(t), _dt(t) + timedelta(hours=PM_HORIZON_H)
        for d in self._subjects('onTool', ox.NamedNode(tool)):
            s, e = _dt(self._value(d, 'startTime').value), _dt(self._value(d, 'endTime').value)
            if (s <= t0 < e) or (t0 < s <= t1):
                out += self._node(d, {str(FAB.onTool), str(FAB.startTime), str(FAB.endTime)})
                out += [(_rd(d), RDF.type, rdflib.URIRef(q.object.value))
                        for q in self.store.quads_for_pattern(d, TYPE, None, DG) if q.object.value in DOWN_T]
        return out

    def _expected_down_h(self, tool, down):
        T, Dn = ox.NamedNode(tool), ox.NamedNode(down)
        g = self._value(T, 'memberOf')
        if self._has_type(Dn, 'Breakdown'):
            h = self._value(self._value(g, 'inArea'), 'mttrHours')
            return float(h.value) if h is not None else 0.0
        tr = self._value(Dn, 'pmTrigger')
        plans = [(self._value(p, 'pmTrigger'), float(self._value(p, 'durationMeanHours').value))
                 for p in self._subjects('pmPlanFor', g)]
        same = [h for ptr, h in plans if tr is not None and ptr is not None and ptr.value == tr.value]
        hs = same or [h for _, h in plans]
        return sum(hs) / len(hs) if hs else 0.0

    # ---- 문맥과 조치 -----------------------------------------------------
    def context(self, sc, actions):
        t, tool, down = sc['time'], D + 'tool/' + sc['eqp_id'], D + 'down/' + sc['down_id']
        lots = {D + 'lot/' + a['lot'] for a in actions if a.get('lot')}
        tools = {tool} | {D + 'tool/' + a[k] for a in actions for k in ('tool', 'to_tool') if a.get(k)}
        for a in actions:  # 해제 요청 장비에 전용된 lot도 문맥에 넣는다
            if a.get('request') == 'DEDICATION_WAIVER':
                wt = ox.NamedNode(D + 'tool/' + a['tool'])
                for d in self._subjects('dedicatedTool', wt):
                    lots.add(self._value(d, 'dedicatedLot').value)
        for a in actions:  # 우선순위 상향 lot의 전용 장비도 정지 구간 조회 대상에 넣는다(PM 예정 여부 판정용)
            if a['type'] == 'Reprioritize' and a.get('lot'):
                for d in self._subjects('dedicatedLot', ox.NamedNode(D + 'lot/' + a['lot'])):
                    dt_tool = self._value(d, 'dedicatedTool')
                    if dt_tool is not None:
                        tools.add(dt_tool.value)
        trip = list(self.static)
        for x in sorted(tools):
            trip += self._downs(x, t)
        routes = set()
        for lot in sorted(lots):
            lt = self._lot(lot)
            trip += lt
            routes |= {str(o) for s, p, o in lt if p == FAB.followsRoute}
        for r in sorted(routes):
            trip += self._route(r)
        scn = rdflib.URIRef(f"{D}scenario/{sc['id']}")
        trip += [(scn, RDF.type, FAB.Scenario),
                 (scn, FAB.scenarioTime, rdflib.Literal(t.replace(' ', 'T'), datatype=XSD.dateTime)),
                 (scn, FAB.incidentTool, rdflib.URIRef(tool)), (scn, FAB.incidentDown, rdflib.URIRef(down)),
                 (scn, FAB.expectedDownHours, rdflib.Literal(self._expected_down_h(tool, down), datatype=XSD.double))]
        return scn, trip

    @staticmethod
    def action_triples(scn, actions):
        out, nodes = [], []
        for i, a in enumerate(actions):
            n = rdflib.URIRef(f'{scn}/action/{i}')
            nodes.append(n)
            out += [(n, RDF.type, FAB[a['type']]), (n, FAB.inScenario, scn)]
            if a.get('lot'):
                out.append((n, FAB.actionLot, rdflib.URIRef(D + 'lot/' + a['lot'])))
            if a.get('tool'):
                out.append((n, FAB.actionTool, rdflib.URIRef(D + 'tool/' + a['tool'])))
            if a.get('to_tool'):
                out.append((n, FAB.toTool, rdflib.URIRef(D + 'tool/' + a['to_tool'])))
            if a.get('priority') is not None:
                out.append((n, FAB.newPriority, rdflib.Literal(int(a['priority']), datatype=XSD.integer)))
            if a.get('to'):
                out.append((n, FAB.escalateTo, rdflib.Literal(a['to'])))
            if a.get('request'):
                out.append((n, FAB.requestType, rdflib.Literal(a['request'])))
            if a.get('defer_hours') is not None:
                out.append((n, FAB.deferHours, rdflib.Literal(float(a['defer_hours']), datatype=XSD.double)))
        return nodes, out

    def check(self, sc, actions):
        """조치마다 반려 사유 목록. 빈 목록이면 통과. 소요 시간은 self.last_timing에 남긴다."""
        if not actions:
            return []
        t0 = time.perf_counter()
        scn, ctx = self.context(sc, actions)
        nodes, act = self.action_triples(scn, actions)
        g = rdflib.Graph()
        for t in ctx + act:
            g.add(t)
        t1 = time.perf_counter()
        _, report, _ = validate(g, shacl_graph=self.shapes, inference='none', inplace=True, allow_warnings=True)
        t2 = time.perf_counter()
        self.last_timing = dict(context_s=t1 - t0, shacl_s=t2 - t1, triples=len(g), actions=len(actions))
        reasons = {n: [] for n in nodes}
        for r in report.subjects(RDF.type, SH.ValidationResult):
            f = report.value(r, SH.focusNode)
            if f in reasons:
                reasons[f].append(str(report.value(r, SH.resultMessage)))
        return [reasons[n] for n in nodes]
