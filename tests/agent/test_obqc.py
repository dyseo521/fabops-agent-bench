"""질의 검증기(OBQC): 실제 질의의 오탐 0건, 코드별 검출, 결과 형식, T-Box 폐포, 속도.

말뭉치 tests/agent/data/sparql_corpus.json은 scripts/sparql_reference.py가 dev 과제를 풀며 KG에 보낸 질의 전문이다
(KG.q를 감싸 기록. sparql_reference.py는 수정하지 않았다). dev 과제의 참조 풀이이므로 에이전트와 프롬프트에 넣지 않는다.
"""
import json
import os
import re
import statistics
import time

import pytest
from rdflib import URIRef

from agent.tools import obqc, sparql_common

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'sparql_corpus.json')
KEYS = {'ok', 'findings', 'checked', 'not_checked', 'unknown_terms'}
FIELDS = {'code', 'severity', 'pattern', 'rule', 'message', 'hint'}
ERROR_CODES = {'SYNTAX', 'NOT_SELECT', 'GRAPH_NOT_ALLOWED', 'UNKNOWN_TERM', 'DISJOINT_TYPES', 'OBJECT_VS_DATATYPE', 'FUNCTIONAL_CONFLICT'}
WARNING_CODES = {'REVERSED_DIRECTION', 'DATETIME_AS_STRING', 'UNKNOWN_NAMESPACE', 'VACUOUS_NEGATION'}
FAB = sparql_common.FAB
VG = sparql_common.VIOLATION_GRAPH


@pytest.fixture(scope='module')
def chk():
    return obqc.checker()


@pytest.fixture(scope='module')
def corpus():
    with open(CORPUS, encoding='utf-8') as f:
        return json.load(f)


def errors(res):
    return [f['code'] for f in res['findings'] if f['severity'] == 'error']


def warnings(res):
    return [f['code'] for f in res['findings'] if f['severity'] == 'warning']


def well_formed(res):
    assert set(res) == KEYS
    assert isinstance(res['ok'], bool) and isinstance(res['checked'], int)
    assert res['ok'] == (not any(f['severity'] == 'error' for f in res['findings']))
    for f in res['findings']:
        assert set(f) == FIELDS and all(isinstance(v, str) for v in f.values())
        assert f['severity'] in ('error', 'warning')
        assert f['code'] in (ERROR_CODES if f['severity'] == 'error' else WARNING_CODES)
        assert f['message'] and '—' not in f['message'] + f['hint']
    assert res['not_checked'][:4] == list(obqc.NOT_CHECKED)
    assert res['unknown_terms'] == sorted(set(res['unknown_terms']))
    return res


# ---- 1. 실제 질의에서 오류가 나지 않는다 ---------------------------------------------
def test_corpus_is_real_and_distinct(corpus):
    qs = [q['query'] for q in corpus['queries']]
    assert corpus['exit_code'] == 0, 'sparql_reference.py가 전 과제 일치로 끝난 실행에서 기록해야 한다'
    assert len(qs) == corpus['distinct'] == len(set(qs)) >= 150
    assert all(q.startswith('PREFIX fab:') for q in qs)         # sparql_reference.PFX 머리말 포함
    solvers = {q['origin'].split('<')[-1] for q in corpus['queries']}       # 질의를 보낸 풀이 함수(t_*)와 KG 초기화
    assert len(solvers) >= 19 and {'t_dedwait', 't_cqt_factor', 't_product_qtime', 't_commonality', 't_bottleneck'} <= solvers


def test_corpus_queries_get_no_error(chk, corpus):
    bad, unread, with_warning, codes = [], [], [], {}
    for q in corpus['queries']:
        res = well_formed(chk.check(q['query']))
        if not res['ok']:
            bad.append((q['origin'], [(f['code'], f['pattern'], f['rule']) for f in res['findings'] if f['severity'] == 'error']))
        if len(res['not_checked']) > 4 or res['checked'] == 0:
            unread.append((q['origin'], res['not_checked'][4:]))
        if warnings(res):
            with_warning.append(q['origin'])
            for c in warnings(res):
                codes[c] = codes.get(c, 0) + 1
    print(f"\n말뭉치 {len(corpus['queries'])}건: 오류 {len(bad)}건, 경고가 붙은 질의 {len(with_warning)}건 {codes}")
    assert bad == [], f'정확한 질의에 오류를 냈다: {bad[:3]}'
    assert unread == [], f'점검하지 못한 질의가 있다: {unread[:3]}'      # 전부 rdflib로 읽고 경로 제한 없이 점검했다
    assert with_warning == [], f'정확한 질의에 경고를 냈다: {with_warning[:3]} {codes}'


def test_corpus_mutations_are_caught(chk, corpus):
    """말뭉치 통과가 공허하지 않다. 같은 질의를 한 군데만 틀리게 바꾸면 전부 잡는다."""
    found = lambda q, code: code in [f['code'] for f in chk.check(q)['findings']]
    renamed = typed = untyped = 0
    for item in corpus['queries']:
        q = item['query']
        head, body = q[:len(q) - len(q.split('\n', 3)[3])], q.split('\n', 3)[3]        # PFX 머리말 3줄과 본문
        m = re.search(r'fab:[a-z][A-Za-z0-9]*', body)
        if m:                               # 속성 하나를 없는 이름으로 바꾼다
            renamed += 1
            assert found(head + body.replace(m.group(0), 'fab:noSuchProperty', 1), 'UNKNOWN_TERM'), item['origin']
        m = re.search(r'(\?\w+) fab:identifier \?\w+ ; a fab:Lot \.|(\?\w+) a fab:Lot ;', q)
        if m:                               # fab:Lot으로 명시한 변수에 fab:StepExecution의 속성을 붙인다
            typed += 1
            res = chk.check(q[:m.start()] + f'{m.group(1) or m.group(2)} fab:processedOn ?zzTool . ' + q[m.start():])
            assert 'DISJOINT_TYPES' in errors(res), item['origin']
        if '"^^xsd:dateTime' in q:          # 시각 리터럴의 형 표기를 지운다
            untyped += 1
            assert found(q.replace('"^^xsd:dateTime', '"'), 'DATETIME_AS_STRING'), item['origin']
    assert renamed >= 170 and typed >= 25 and untyped >= 100, (renamed, typed, untyped)


def test_corpus_check_time(chk, corpus):
    times = []
    for q in corpus['queries']:
        t = time.perf_counter()
        chk.check(q['query'])
        times.append(time.perf_counter() - t)
    print(f'\n말뭉치 점검 시간: 중앙값 {statistics.median(times) * 1000:.1f}ms, 최대 {max(times) * 1000:.1f}ms')
    assert statistics.median(times) < 0.1
    assert max(times) < 3.0


# ---- 2. 오류 코드별 검출 -----------------------------------------------------------
ERROR_CASES = {
    'SYNTAX': [
        'SELECT ?x WHERE { ?x a fab:Lot ',
        'SELECT ?x, ?y WHERE { ?x fab:hasEvent ?y }',
        'SELECT ?x WHERE { ?x a foo:Lot }',
        'SELECT ?x WHERE { ?x skos:definition ?d }',            # rdflib는 skos:를 기본으로 알지만 실행 엔진은 모른다
        'SELECT ?x WHERE { ?x fab:hasEvent ?e } GROUP BY ?x HAVING COUNT(?e) > 3',
        'SELECT ?x WHERE { ?x fab:trackInTime ?t . FILTER(?t BETWEEN 1 AND 2) }',
        'SELECT ?x WHERE { ?x a fab:Lot } LIMIT 10;',
        '',
        '   \n ',
    ],
    'NOT_SELECT': [
        'CONSTRUCT { ?x a fab:Lot } WHERE { ?x a fab:Lot }',
        'DESCRIBE fab:Lot',
        'INSERT DATA { fab:a a fab:Lot }',
        'DELETE WHERE { ?x a fab:Lot }',
        'PREFIX fab: <https://w3id.org/ontosemc/fab#>\n# 주석\nconstruct { ?x a fab:Lot } where { ?x a fab:Lot }',
        'DROP GRAPH <https://w3id.org/ontosemc/graph/violations>',
        'WITH <http://example.org/g> DELETE { ?x a fab:Lot } WHERE { ?x a fab:Lot }',
    ],
    'GRAPH_NOT_ALLOWED': [
        f'SELECT ?v WHERE {{ GRAPH <{VG}> {{ ?v a fab:QTimeViolation }} }}',
        'SELECT ?v WHERE { GRAPH ?g { ?v a fab:QTimeViolation } }',
        f'SELECT ?x FROM <{VG}> WHERE {{ ?x a fab:QTimeViolation }}',
        f'SELECT ?x FROM NAMED <{VG}> WHERE {{ ?x a fab:Lot }}',
        'SELECT ?x WHERE { SERVICE <http://example.org/sparql> { ?x a fab:Lot } }',
        f'SELECT ?l WHERE {{ ?l a fab:Lot OPTIONAL {{ GRAPH <{VG}> {{ ?v fab:violationLot ?l }} }} }}',
        f'ASK {{ ?l a fab:Lot FILTER EXISTS {{ GRAPH <{VG}> {{ ?v fab:violationLot ?l }} }} }}',
    ],
    'UNKNOWN_TERM': [
        'SELECT ?x WHERE { ?x fab:processedBy ?t }',
        'SELECT ?x WHERE { ?x a fab:Equipment }',
        'SELECT ?x WHERE { ?x fab:trackintime ?t }',
        'SELECT ?x WHERE { ?x a fab:SBY }',                     # 개체를 클래스로 썼다
        'SELECT ?x WHERE { ?x fab:Lot ?y }',                    # 클래스를 속성으로 썼다
        'SELECT ?x WHERE { ?x a fab:Lot ; fab:hasEvent/fab:onEquipment ?t }',
        'SELECT ?x WHERE { ?x fab:followingStep+ ?y }',
        'SELECT ?x WHERE { ?x (fab:nextStep|fab:previousStep) ?y }',
        'SELECT ?x WHERE { ?x a fab:Lot OPTIONAL { ?x fab:holdReason ?r } }',
        'SELECT ?x WHERE { ?x a fab:Lot FILTER NOT EXISTS { ?x fab:hasHold ?h } }',
        'SELECT ?x WHERE { { SELECT ?x WHERE { ?x a fab:Machine } LIMIT 5 } }',
        'SELECT ?x WHERE { { ?x a fab:Lot } UNION { ?x a fab:Batch } }',
        'SELECT (COUNT(?x) AS ?n) WHERE { ?x <https://w3id.org/ontosemc/fab#lotId> "A" }',
    ],
    'DISJOINT_TYPES': [
        'SELECT ?lot WHERE { ?lot a fab:Lot ; fab:processedOn ?tool }',
        'SELECT ?x WHERE { ?x fab:waferOf ?l ; fab:ofLot ?l2 }',
        'SELECT ?t WHERE { ?e fab:processedOn ?t . ?t fab:toolCount ?n }',
        'SELECT ?x WHERE { ?x a fab:ProductiveLot ; fab:atStep ?s }',                   # 하위 클래스로 전파된 서로소
        'SELECT ?x WHERE { ?x a fab:ProductiveLot , fab:EngineeringLot }',
        'SELECT ?lot ?t WHERE { ?lot a fab:Lot OPTIONAL { ?lot fab:processedOn ?t } }',    # 성립할 수 없는 OPTIONAL
        'SELECT ?lot WHERE { ?lot a fab:Lot . { SELECT ?lot WHERE { ?lot fab:processedOn ?t } } }',
        'SELECT ?n WHERE { ?l fab:hasEvent/fab:waferNo ?n }',                           # 경로의 중간 노드
        'SELECT ?l WHERE { ?l a fab:Lot FILTER NOT EXISTS { ?l fab:hasEvent ?e . ?e fab:waferNo ?n } }',   # 안쪽만으로 모순
        'SELECT ?x WHERE { ?x a fab:Lot . { ?x fab:priority ?p } UNION { ?x fab:waferNo ?p } }',     # 성립할 수 없는 UNION 가지
        'SELECT ?g WHERE { <https://w3id.org/ontosemc/data/tool/T1> fab:memberOf ?g ; fab:toolCount ?n }',
        'SELECT ?x WHERE { ?x a fab:Lot FILTER EXISTS { ?x fab:atStep ?s } }',
        'SELECT ?d WHERE { ?d a fab:Breakdown . ?d a fab:PreventiveMaintenance }',
        'SELECT ?x WHERE { ?x a fab:Lot . OPTIONAL { ?x fab:priority ?p } ?x fab:stepSeq ?n }',   # OPTIONAL로 나뉜 두 BGP
    ],
    'OBJECT_VS_DATATYPE': [
        'SELECT ?x WHERE { ?x fab:processedOn "Litho_01" }',
        'SELECT ?x WHERE { ?x fab:identifier fab:Litho }',
        'SELECT ?x WHERE { ?x fab:identifier ?id . ?id fab:memberOf ?g }',
        'SELECT ?x WHERE { ?x fab:lotType ?v . ?y fab:processedOn ?v }',
        'SELECT ?x WHERE { ?x fab:identifier ?id . ?id a fab:Tool }',
        'SELECT ?x WHERE { ?x fab:ofLot/fab:identifier/fab:hasEvent ?e }',
        'SELECT ?i WHERE { ?i fab:e10State "SBY" }',
        'SELECT ?x WHERE { ?x fab:trackInTime <https://w3id.org/ontosemc/data/t> }',
    ],
    'FUNCTIONAL_CONFLICT': [
        'SELECT ?i WHERE { ?i fab:e10State fab:SBY ; fab:e10State fab:PRD }',
        'SELECT ?s WHERE { ?s fab:stepSeq 1 . ?s fab:stepSeq 2 }',
        'SELECT ?t WHERE { ?t fab:memberOf <https://w3id.org/ontosemc/data/tg/A> ; fab:memberOf <https://w3id.org/ontosemc/data/tg/B> }',
        'SELECT ?i WHERE { ?i fab:e10State fab:UDT . OPTIONAL { ?i fab:stateReason ?r } ?i fab:e10State fab:SDT }',
        'SELECT ?w WHERE { ?w fab:exhibits fab:Center . ?w fab:waferOf ?l . ?w fab:exhibits fab:Donut }',
    ],
}
WARNING_CASES = {
    'REVERSED_DIRECTION': [
        'SELECT ?x WHERE { ?tool a fab:Tool . ?x fab:ofLot ?lot . ?tool fab:processedOn ?x }',
        'SELECT ?i WHERE { ?i fab:stateOf ?tool . fab:SBY fab:e10State ?i }',
        'SELECT ?c WHERE { ?c a fab:QTimeConstraint . ?s fab:stepSeq ?n . ?c fab:hasQTimeConstraint ?s }',
        'SELECT ?l WHERE { ?l fab:priority ?p . ?t fab:locatedIn ?b . ?t fab:passedThroughTool ?l }',
    ],
    'DATETIME_AS_STRING': [
        'SELECT ?x WHERE { ?x fab:trackInTime ?t FILTER(?t >= "2018-03-02" && ?t < "2018-03-03 12:00:00") }',
        'SELECT ?x WHERE { ?x fab:trackInTime ?t FILTER(STR(?t) > "2018-03-02T00:00:00") }',
        'SELECT ?l (MIN(?t) AS ?first) WHERE { ?x fab:ofLot ?l ; fab:trackInTime ?t } GROUP BY ?l HAVING (MIN(?t) > "2018-03-02")',
        'SELECT ?x WHERE { ?x fab:trackInTime ?t BIND(STR(?t) AS ?s) FILTER(?s < "2018-03-03") }',
        'SELECT ?d WHERE { ?d fab:startTime ?s FILTER(?s IN ("2018-03-02T00:00:00", "2018-03-03T00:00:00")) }',
        'SELECT ?l WHERE { ?l fab:releaseTime ?r FILTER("2018-03-01" <= ?r) }',
    ],
    'UNKNOWN_NAMESPACE': [
        'SELECT ?x WHERE { ?x a <http://example.org/Lot> }',
        'SELECT ?x WHERE { ?x <http://example.org/p> ?y }',
        'PREFIX ex: <http://example.org/> SELECT ?x WHERE { ?x a fab:Lot ; ex:owner ?o }',
    ],
    'VACUOUS_NEGATION': [
        'SELECT ?l WHERE { ?l a fab:Lot FILTER NOT EXISTS { ?l fab:processedOn ?t } }',
        'SELECT ?l WHERE { ?l a fab:Lot MINUS { ?l fab:exhibits ?p } }',
        'SELECT ?x WHERE { ?x a fab:ProductiveLot FILTER NOT EXISTS { ?x a fab:EngineeringLot } }',     # 중복이지만 결과가 맞는 조건
    ],
}


def test_enough_cases_per_code():
    assert set(ERROR_CASES) == ERROR_CODES and all(len(v) >= 3 for v in ERROR_CASES.values())
    assert set(WARNING_CASES) == WARNING_CODES and all(len(v) >= 2 for v in WARNING_CASES.values())


@pytest.mark.parametrize('code,query', [(c, q) for c, qs in ERROR_CASES.items() for q in qs])
def test_error_code_is_reported(chk, code, query):
    res = well_formed(chk.check(query))
    assert code in errors(res), res['findings']
    assert res['ok'] is False


@pytest.mark.parametrize('code,query', [(c, q) for c, qs in WARNING_CASES.items() for q in qs])
def test_warning_code_is_reported_without_blocking(chk, code, query):
    res = well_formed(chk.check(query))
    assert code in warnings(res), res['findings']
    assert res['ok'] is True and errors(res) == []


def test_findings_cite_axioms_and_fixes(chk):
    f, = chk.check('SELECT ?lot WHERE { ?lot a fab:Lot ; fab:processedOn ?tool }')['findings']
    assert f['rule'] == 'fab:processedOn rdfs:domain fab:StepExecution; fab:Lot owl:disjointWith fab:StepExecution'
    assert '?lot a fab:Lot' in f['pattern'] and '?lot fab:processedOn ?tool' in f['pattern']
    assert 'fab:hasEvent' in f['hint'] and 'fab:ofLot' in f['hint']         # 두 클래스를 잇는 속성을 알려 준다
    f, = chk.check('SELECT ?x WHERE { ?x a fab:ProductiveLot ; fab:atStep ?s }')['findings']
    assert 'fab:ProductiveLot rdfs:subClassOf fab:Lot' in f['rule'] and 'fab:Lot owl:disjointWith fab:StepExecution' in f['rule']
    f, = chk.check('SELECT ?x WHERE { ?x fab:trackintime ?t }')['findings']
    assert 'fab:trackInTime' in f['hint']                                   # 가까운 이름을 제안한다
    f, = chk.check('SELECT ?x WHERE { ?e fab:atStep ?s . ?l a fab:Lot . ?e fab:hasEvent ?l }')['findings'][-1:]
    assert f['code'] == 'REVERSED_DIRECTION' and '?l fab:hasEvent ?e' in f['hint'] and 'fab:ofLot' in f['hint']     # 역속성 제안
    f, = chk.check('SELECT ?x WHERE { ?x fab:trackInTime ?t FILTER(?t >= "2018-03-02") }')['findings']
    assert f['rule'] == 'fab:trackInTime rdfs:range xsd:dateTime' and '"2018-03-02T00:00:00"^^xsd:dateTime' in f['hint']
    f, = chk.check(f'SELECT ?v WHERE {{ GRAPH <{VG}> {{ ?v a fab:QTimeViolation }} }}')['findings']
    assert f['code'] == 'GRAPH_NOT_ALLOWED' and '기본 그래프에서 함께 조회된다' in f['hint']      # 위반 어휘가 아니라 그래프 지정만 막는다
    f, = chk.check('SELECT ?x WHERE {\n ?x a fab:Lot .\n ?x fab:priority ?p ?q\n}')['findings']
    assert f['code'] == 'SYNTAX' and '3행' in f['message']                   # 줄 번호는 모델이 쓴 질의 기준이다


# ---- 3. 오류가 아닌 것(아슬아슬하게 맞는 질의) -----------------------------------------
CLEAN = {
    'subclass_with_superclass_domain': 'SELECT ?l WHERE { ?l a fab:ProductiveLot ; fab:priority ?p ; fab:hasEvent ?e . ?e fab:processedOn ?t }',
    'subclass_downevent': 'SELECT ?d WHERE { ?d a fab:Breakdown ; fab:onTool ?t ; fab:startTime ?s . ?t fab:memberOf ?g }',
    'optional_bodies_coalesce': 'SELECT ?s ?g WHERE { ?s a fab:ProcessStep . OPTIONAL { ?s fab:requiresToolGroup ?g } OPTIONAL { ?s fab:waitsAt ?g } }',
    'union_bodies_differ': 'SELECT ?s ?g WHERE { ?s a fab:ProcessStep . { ?s fab:requiresToolGroup ?g } UNION { ?s fab:waitsAt ?g } }',
    'union_same_var_two_classes': 'SELECT ?x WHERE { { ?x a fab:Lot } UNION { ?x a fab:Wafer } UNION { ?x a fab:Tool } ?x fab:identifier ?i }',
    'optional_after_union': 'SELECT ?x ?w WHERE { { ?x a fab:Lot } UNION { ?x a fab:Wafer } OPTIONAL { ?x fab:hasWafer ?w } }',
    'path_sequence_inverse': 'SELECT ?g WHERE { ?l a fab:Lot ; fab:hasEvent/fab:processedOn/fab:memberOf ?g . ?t ^fab:processedOn ?e . ?l ^fab:ofLot/fab:atStep ?s }',
    'subquery_reuses_name': 'SELECT ?lot ?x WHERE { { SELECT ?lot WHERE { ?x fab:waferOf ?lot } GROUP BY ?lot HAVING (COUNT(?x) >= 3) } ?x fab:ofLot ?lot }',
    'not_disjoint_is_not_an_error': 'SELECT ?x WHERE { ?x a fab:Tool ; fab:atStep ?s }',          # 정의역과 다르지만 서로소 선언이 없다
    'domainless_property': 'SELECT ?x WHERE { ?x fab:startTime ?s ; fab:endTime ?e ; fab:identifier ?i ; fab:ofProduct ?p }',
    'superclass_from_dr': 'PREFIX SO: <http://www.w3id.org/ecsel-dr-SO#>\nSELECT ?x WHERE { ?x a SO:Lot ; fab:priority ?p ; SO:has_lot_event ?e }',
    'predicate_variable': 'SELECT DISTINCT ?p WHERE { ?s ?p ?o }',
    'schema_query': 'SELECT ?p ?c WHERE { ?p rdfs:domain fab:Lot ; rdfs:range ?c . ?c rdfs:subClassOf ?d . ?x a ?c }',
    'values': 'SELECT ?t WHERE { VALUES ?lid { "L1" "L2" } VALUES ?st { fab:UDT fab:SDT } ?l fab:identifier ?lid . ?i fab:e10State ?st ; fab:stateOf ?t }',
    'individual_filter': 'SELECT ?i WHERE { ?i fab:stateOf ?t ; fab:e10State ?st . FILTER (?st IN (fab:UDT, fab:SDT) && ?st != fab:SBY) }',
    'datetime_typed': 'SELECT ?x WHERE { ?x fab:trackInTime ?t FILTER(?t >= "2018-03-02T00:00:00"^^xsd:dateTime && ?t < xsd:dateTime("2018-03-03T00:00:00")) }',
    'datetime_prefix_match': 'SELECT ?x WHERE { ?x fab:trackInTime ?t FILTER(STRSTARTS(STR(?t), "2018-03-02")) }',
    'string_compare_on_string': 'SELECT ?x WHERE { ?x fab:identifier ?i ; fab:lotType ?k FILTER(?i > "A" && STR(?k) = "PROD") }',
    'functional_same_value': 'SELECT ?s WHERE { ?s fab:stepSeq 1 . ?s fab:stepSeq 01 }',
    'functional_two_variables': 'SELECT ?s WHERE { ?s fab:onRoute ?r1 ; fab:onRoute ?r2 . ?i fab:e10State fab:SBY . ?j fab:e10State fab:PRD }',
    'functional_in_union': 'SELECT ?i WHERE { { ?i fab:e10State fab:UDT } UNION { ?i fab:e10State fab:SDT } }',
    'exists_consistent': 'SELECT ?l WHERE { ?l a fab:Lot FILTER EXISTS { ?l fab:hasWafer ?w . ?w fab:exhibits fab:Center } FILTER NOT EXISTS { ?l fab:completeTime ?d } }',
    'minus_consistent': 'SELECT ?l WHERE { ?l a fab:Lot MINUS { ?w fab:waferOf ?l ; fab:exhibits ?p } }',
    'ask': 'ASK { ?x a fab:Lot ; fab:lotType "PROD" }',
    'lowercase_and_comment': 'select ?x where {\n  # 장비\n  ?x a fab:Tool ; fab:locatedIn ?b . ?b fab:bayType "litho" } order by ?x limit 5',
    'blank_node': 'SELECT ?l WHERE { ?l fab:hasEvent [ fab:processedOn [ fab:identifier "Litho_01" ] ; fab:trackInTime ?t ] }',
    'inverse_pair': 'SELECT ?e WHERE { ?l fab:hasEvent ?e . ?e fab:ofLot ?l . ?r fab:hasStep ?s . ?s fab:onRoute ?r }',
    'same_class_both_ends': 'SELECT ?b WHERE { ?a fab:nextStep ?b . ?b fab:nextStep ?c . ?a fab:reworkTo ?c }',
    'aggregate_subquery': 'SELECT ?tg ?n WHERE { ?g a fab:ToolGroup ; fab:identifier ?tg . { SELECT ?g (COUNT(?m) AS ?n) WHERE { ?m fab:memberOf ?g } GROUP BY ?g } }',
    'select_expression_exists': 'SELECT ?x (EXISTS { ?x fab:hasWafer ?w } AS ?has) WHERE { ?x a fab:Lot }',
    'data_iri_subject': 'SELECT ?e WHERE { <https://w3id.org/ontosemc/data/lot/L1> fab:hasEvent ?e . ?e fab:processedOn <https://w3id.org/ontosemc/data/tool/T1> }',
    # 조건 D는 위반 그래프를 기본 그래프에 합쳐 조회한다. 위반 어휘는 T-Box에 선언된 어휘라 지적하지 않는다
    'violation_qtime': '''SELECT ?id ?h ?tool WHERE { ?v a fab:QTimeViolation ; fab:violationLot ?lot ; fab:violatedConstraint ?c ;
        fab:entryEndTime ?t0 ; fab:deadline ?d ; fab:stillWaiting ?sw ; fab:overrunHours ?h . ?lot fab:identifier ?id .
        ?c fab:entryStep ?es ; fab:exitStep ?xs ; fab:maxHours ?mx . OPTIONAL { ?v fab:exitExecution ?x . ?x fab:processedOn ?tool ; fab:trackInTime ?tin }
        FILTER (?d >= "2018-03-02T00:00:00"^^xsd:dateTime && ?h > 1.5) } ORDER BY DESC(?h)''',
    'violation_generic': '''SELECT ?kind (COUNT(?v) AS ?n) WHERE { ?v a fab:ConstraintViolation ; a ?kind ; fab:violatesShape ?shape ;
        fab:violationFocus ?focus . OPTIONAL { ?v fab:violationMessage ?msg } OPTIONAL { ?v fab:violationValue ?val . ?val fab:identifier ?name }
        ?kind rdfs:subClassOf fab:ConstraintViolation } GROUP BY ?kind''',
    'violation_kinds': '''SELECT ?v ?lot WHERE { { ?v a fab:LtlViolation } UNION { ?v a fab:QualificationViolation } UNION { ?v a fab:AvailabilityViolation }
        UNION { ?v a fab:IntegrityViolation } ?v fab:violationLot ?lot ; fab:violationFocus ?e . ?e fab:ofLot ?lot ; fab:processedOn ?t }''',
    'violation_not_exists': 'SELECT ?lot WHERE { ?lot a fab:Lot FILTER NOT EXISTS { ?v a fab:QTimeViolation ; fab:violationLot ?lot } }',
}
PATHS = {
    'plus': 'SELECT ?s2 WHERE { ?s fab:nextStep+ ?s2 }',
    'star_question': 'SELECT ?c WHERE { ?c rdfs:subClassOf* fab:DownEvent . ?a fab:nextStep? ?b }',
    'alternative': 'SELECT ?g WHERE { ?s (fab:requiresToolGroup|fab:waitsAt) ?g }',
    'negated': 'SELECT ?o WHERE { ?s !(rdf:type|fab:identifier) ?o }',
    'mixed': 'SELECT ?z WHERE { ?l fab:hasEvent/fab:atStep/fab:nextStep+ ?z }',
}


@pytest.mark.parametrize('name', sorted(CLEAN))
def test_correct_query_gets_no_finding(chk, name):
    res = well_formed(chk.check(CLEAN[name]))
    assert res['ok'] and res['findings'] == [], res['findings']
    assert res['checked'] >= 1 and res['unknown_terms'] == [] and len(res['not_checked']) == 4


@pytest.mark.parametrize('name', sorted(PATHS))
def test_repeating_and_alternative_paths_are_listed_not_judged(chk, name):
    res = well_formed(chk.check(PATHS[name]))
    assert res['ok'] and res['findings'] == []
    extra = res['not_checked'][4:]
    assert extra and all('이 질의의 경로' in x for x in extra)        # 경로마다 한 줄


def test_path_classes_are_not_inferred_for_repetition(chk):
    # nextStep*는 0회일 수 있다. ?l이 fab:Lot이어도 오류가 아니다
    assert chk.check('SELECT ?x WHERE { ?l a fab:Lot . ?l fab:nextStep* ?x }')['ok']
    assert chk.check('SELECT ?x WHERE { ?l a fab:Lot . ?l (fab:nextStep|fab:hasEvent) ?x }')['ok']


def test_engine_parser_decides_syntax(chk):
    # rdflib는 "2."을 읽지 못하지만 실행 엔진은 받는다. 막지 않고 점검하지 못했다고 적는다
    res = well_formed(chk.check('SELECT ?x WHERE { ?x fab:maxHours ?h FILTER(?h > 1.5 && ?h <= 2.) }'))
    assert res['ok'] and res['findings'] == [] and 'rdflib' in res['not_checked'][-1]
    res = well_formed(chk.check(f'SELECT ?x WHERE {{ GRAPH <{VG}> {{ ?x fab:overrunHours ?h FILTER(?h <= 2.) }} }}'))
    assert errors(res) == ['GRAPH_NOT_ALLOWED']             # 읽지 못한 질의에서도 도구 규칙은 본다


def test_negation_conflict_with_outer_pattern_is_only_a_warning(chk):
    res = chk.check('SELECT ?x WHERE { ?x a fab:ProductiveLot FILTER NOT EXISTS { ?x a fab:EngineeringLot } }')
    assert res['ok'] and warnings(res) == ['VACUOUS_NEGATION']
    res = chk.check('SELECT ?x WHERE { ?x a fab:Lot FILTER (!EXISTS { ?x fab:waferNo ?n }) }')
    assert res['ok'] and warnings(res) == ['VACUOUS_NEGATION']
    res = chk.check('SELECT ?x WHERE { ?x a fab:Lot FILTER (EXISTS { ?x fab:waferNo ?n } || ?x = fab:PRD) }')
    assert res['ok'] and warnings(res) == ['VACUOUS_NEGATION']         # 논리합 안의 EXISTS는 질의 전체를 비우지 않는다
    # 같은 충돌이 NOT EXISTS에서는 경고, 뒤따르는 OPTIONAL에서는 오류다. 경고가 오류를 가리지 않는다
    res = chk.check('SELECT ?x WHERE { { ?x a fab:Lot FILTER NOT EXISTS { ?x fab:waferNo ?n } } OPTIONAL { ?x fab:waferNo ?m } }')
    assert errors(res) == ['DISJOINT_TYPES'] and warnings(res) == ['VACUOUS_NEGATION']


# ---- 4. 결과 형식 -----------------------------------------------------------------
def test_result_structure(chk):
    samples = ([q for qs in ERROR_CASES.values() for q in qs] + [q for qs in WARNING_CASES.values() for q in qs]
               + list(CLEAN.values()) + list(PATHS.values()))
    for q in samples:
        well_formed(chk.check(q))
    for odd in (None, 3, b'SELECT', 'SELECT', '}{', '\x00', 'SELECT ?x WHERE { ' + '?x fab:hasEvent ?e . ' * 400 + '}'):
        well_formed(chk.check(odd))                         # 어떤 입력에도 예외를 내지 않는다


def test_unknown_terms_and_counts(chk):
    res = chk.check('SELECT ?x WHERE { ?x a fab:Machine ; fab:hasLot ?l ; fab:identifier ?i ; <http://example.org/p> ?o . ?l a fab:Lot }')
    assert res['unknown_terms'] == ['http://example.org/p', FAB + 'Machine', FAB + 'hasLot']
    assert errors(res).count('UNKNOWN_TERM') == 2 and warnings(res) == ['UNKNOWN_NAMESPACE'] and res['checked'] == 5
    res = chk.check('SELECT ?x WHERE { ?x a fab:SBY ; rdfs:label ?l ; rdfs:madeUp ?m ; owl:sameAs ?y }')
    assert res['unknown_terms'] == []                       # 개체를 클래스로 쓴 것은 없는 이름이 아니다. rdf, rdfs, owl, xsd는 보지 않는다
    assert chk.check('SELECT ?x WHERE { ?x fab:hasEvent/fab:processedOn ?t }')['checked'] == 1      # 경로도 패턴 하나로 센다
    assert chk.check('SELECT ?x WHERE { ?x a fab:Lot OPTIONAL { ?x fab:priority ?p } FILTER NOT EXISTS { ?x fab:hasWafer ?w } }')['checked'] == 3


def test_prefixes_are_read_at_call_time(chk, monkeypatch):
    q = 'SELECT ?x WHERE { ?x zz:p ?y }'
    assert errors(chk.check(q)) == ['SYNTAX']
    monkeypatch.setitem(sparql_common.PREFIXES, 'zz', 'http://example.org/zz#')
    res = chk.check(q)
    assert res['ok'] and warnings(res) == ['UNKNOWN_NAMESPACE'] and res['findings'][0]['pattern'] == '?x zz:p ?y'
    own = 'PREFIX f: <https://w3id.org/ontosemc/fab#>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\nSELECT ?x WHERE { ?x a f:Lot ; f:processedOn ?t }'
    assert errors(chk.check(own)) == ['DISJOINT_TYPES']     # 모델이 선언한 접두사로 쓴 질의도 같게 점검한다


# ---- 5. T-Box 폐포 ----------------------------------------------------------------
def test_tbox_closures(chk):
    tb, f = chk.tbox, lambda n: URIRef(FAB + n)
    so, gdm, df = 'http://www.w3id.org/ecsel-dr-SO#', 'http://www.w3id.org/ecsel-dr-GDM#', 'http://www.w3id.org/ecsel-dr-DF#'
    assert {f('Lot'), URIRef(so + 'Lot'), URIRef(df + 'Productive_Lot'), URIRef(df + 'Lot')} <= tb.supers(f('ProductiveLot'))
    assert tb.disjoint(f('Lot'), f('StepExecution')) and tb.disjoint(f('ProductiveLot'), f('StepExecution'))      # 하위 클래스로 전파
    assert tb.disjoint(f('ProductionRoute'), f('EngineeringLot')) and tb.disjoint(f('Breakdown'), f('PreventiveMaintenance'))
    assert not tb.disjoint(f('Tool'), f('StepExecution')) and not tb.disjoint(f('Lot'), f('ProductiveLot'))
    assert set(tb.domain[f('onRoute')]) == {f('ProcessStep'), URIRef(gdm + 'Route_Sequence')}        # 상위 속성의 정의역 상속
    assert f('ProcessStep') in tb.range[f('nextStep')] and f('Tool') in tb.range[f('processedOn')]
    assert tb.inverses[f('ofLot')] == [f('hasEvent')] and tb.inverses[f('hasEvent')] == [f('ofLot')]
    assert tb.kind[f('processedOn')] == 'object' and tb.kind[f('identifier')] == 'datatype'
    assert f('processedOn') in tb.functional and f('hasEvent') not in tb.functional
    assert f('trackInTime') in tb.datetime_props and f('identifier') not in tb.datetime_props
    assert tb.individuals[f('SBY')] == {f('E10State')} and tb.individuals[f('EdgeRing')] == {f('DefectPattern')}
    s = chk.stats()
    assert (s['classes_fab'], s['properties_fab'], s['object_properties'], s['datatype_properties']) == (46, 122, 69, 58)
    assert (s['functional_properties'], s['disjoint_pairs_declared'], s['disjoint_pairs_propagated'], s['inverse_pairs']) == (61, 24, 48, 5)


def test_every_declared_term_alone_is_accepted(chk):
    """T-Box 자체가 오류를 만들지 않는다: 선언된 속성과 클래스를 하나씩 쓴 질의는 전부 통과한다."""
    tb = chk.tbox
    for p in sorted(tb.props):
        res = chk.check(f'SELECT * WHERE {{ ?s <{p}> ?o }}')
        assert res['ok'] and res['findings'] == [], (p, res['findings'])
    for c in sorted(tb.classes):
        res = chk.check(f'SELECT * WHERE {{ ?s a <{c}> }}')
        assert res['ok'] and res['findings'] == [], (c, res['findings'])


# ---- 6. 속도 ---------------------------------------------------------------------
def test_loading_time():
    t = time.perf_counter()
    c = obqc.Checker()
    assert time.perf_counter() - t < 2.0
    assert obqc.checker() is obqc.checker() and c is not obqc.checker()         # 공용 검증기는 하나다


def test_single_check_time(chk):
    typical = [CLEAN['subclass_with_superclass_domain'], CLEAN['aggregate_subquery'], CLEAN['exists_consistent'],
               ERROR_CASES['DISJOINT_TYPES'][0], WARNING_CASES['DATETIME_AS_STRING'][0], PATHS['mixed']]
    worst = 0.0
    for q in typical:
        runs = []
        for _ in range(5):
            t = time.perf_counter()
            chk.check(q)
            runs.append(time.perf_counter() - t)
        worst = max(worst, statistics.median(runs))
    assert worst < 0.1, f'{worst * 1000:.1f}ms'
