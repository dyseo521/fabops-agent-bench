"""온톨로지 기반 질의 검증(OBQC). 모델이 쓴 SPARQL 질의를 실행 전에 동결한 T-Box와 대조한다.

  from agent.tools import obqc
  res = obqc.checker().check(query_text)
  res = {"ok": bool, "findings": [{"code", "severity", "pattern", "rule", "message", "hint"}, ...],
         "checked": int, "not_checked": [str, ...], "unknown_terms": [str, ...]}

오류(severity "error", 실행 차단)
  SYNTAX               질의를 읽을 수 없다
  NOT_SELECT           SELECT, ASK가 아니다
  GRAPH_NOT_ALLOWED    GRAPH, FROM, FROM NAMED, SERVICE를 썼다
  UNKNOWN_TERM         fab: 이름공간의 속성 또는 클래스(rdf:type의 목적어)가 T-Box에 없다
  DISJOINT_TYPES       한 변수(또는 항)에 서로소인 두 클래스가 함께 요구된다
  OBJECT_VS_DATATYPE   객체 속성에 리터럴, 데이터 속성에 IRI 또는 개체로 쓰인 변수
  FUNCTIONAL_CONFLICT  함수 속성에 같은 주어로 서로 다른 상수 값 둘
경고(severity "warning", 실행함)
  REVERSED_DIRECTION   주어와 목적어의 알려진 클래스가 속성의 방향과 반대다
  DATETIME_AS_STRING   xsd:dateTime 값을 문자열과 비교하거나 STR()로 감싸 비교한다
  UNKNOWN_NAMESPACE    T-Box에 없는 다른 이름공간의 이름을 속성 또는 클래스로 썼다
  VACUOUS_NEGATION     NOT EXISTS, MINUS 안의 패턴이 바깥 패턴과 함께 성립할 수 없다(조건이 결과를 바꾸지 않는다)

오탐을 내지 않는 쪽으로 설계했다. 오류는 공리로 증명되는 것만 낸다
  - rdfs:domain, rdfs:range는 추론 공리다. 클래스가 다르다는 것만으로는 오류가 아니다. 추론한 클래스 집합(상위 클래스로 닫음)에
    서로소로 선언된 쌍이 있을 때만 오류다
  - 클래스 환경은 함께 성립해야 하는 패턴끼리만 합친다. OPTIONAL 본문과 UNION 가지의 클래스는 밖으로 내보내지 않는다
    (UNION은 모든 가지에 공통인 클래스만 내보낸다). 부질의는 투영한 변수만 바깥과 같은 변수로 본다
  - NOT EXISTS, MINUS 안의 패턴이 바깥과 충돌하는 것은 오류가 아니다(중복이지만 결과가 맞는 조건일 수 있다). 경고 VACUOUS_NEGATION으로 낸다.
    안쪽 패턴만으로 모순이면 오류다
  - 구문 오류는 실행 엔진(pyoxigraph)의 파서도 거부할 때만 낸다. rdflib 파서만 읽지 못하는 질의는 점검하지 않고 통과시킨다
  - 검증기 안에서 예외가 나면 점검을 건너뛰고 통과시킨다(not_checked에 적는다)

unknown_terms에는 속성 또는 클래스 자리에 쓰였지만 T-Box에 없는 IRI(전체 IRI)를 넣는다. rdf, rdfs, owl, xsd 이름공간은 넣지 않는다.
제약 위반 어휘(fab:ConstraintViolation과 하위 클래스, fab:violationLot, fab:deadline 등)는 T-Box에 선언된 어휘라 다른 어휘와 같게 다룬다
(조건 D의 질의 도구는 위반 그래프를 기본 그래프에 합쳐 조회한다). GRAPH 절로 그 그래프를 지정하는 것만 막는다.
T-Box는 ontology/fab.ttl과 ontology/dr_subset.ttl을 읽어 폐포를 직접 계산한다. data/ 아래 파일은 읽지 않는다.
"""
import difflib
import functools
import itertools
import logging
import os
import re
import threading
from collections import defaultdict, namedtuple

import rdflib
from rdflib import OWL, RDF, RDFS, XSD
from rdflib.collection import Collection
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql import algebra, parser
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import BNode, Literal, URIRef, Variable

from .. import config
from . import sparql_common

TBOX_FILES = (os.path.join(config.ROOT, 'ontology', 'fab.ttl'), os.path.join(config.ROOT, 'ontology', 'dr_subset.ttl'))
BUILTIN_NS = (str(RDF), str(RDFS), str(OWL), str(XSD))
NOT_CHECKED = (
    '질의가 질문의 의도와 맞는지',
    'FILTER의 산술과 집계 논리',
    '조건에 맞는 데이터가 실제로 있는지',
    '속성 경로의 +, *, ? 반복과 대안 경로(|), 부정 경로(!)의 클래스(속성이 선언되어 있는지만 확인한다)',
)
ERROR, WARNING = 'error', 'warning'
TOOL_RULE_SELECT = '질의 도구 규칙: SELECT와 ASK만 실행한다'
TOOL_RULE_GRAPH = '질의 도구 규칙: 기본 그래프만 조회한다'
NO_DECL = 'T-Box(fab.ttl, dr_subset.ttl)에 선언이 없다'
NON_SELECT_WORDS = {'CONSTRUCT', 'DESCRIBE', 'INSERT', 'DELETE', 'LOAD', 'CLEAR', 'CREATE', 'DROP', 'COPY', 'MOVE', 'ADD', 'WITH'}
COMPARE_OPS = {'=', '!=', '<', '>', '<=', '>='}
_LOCK = threading.Lock()        # rdflib 파서는 재진입을 보장하지 않는다
_NOISE = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""|\'\'\'(?:[^\'\\]|\\.|\'(?!\'\'))*\'\'\'|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\''
                    r'|(<[^<>"{}|^`\\\s]*>)|#[^\n]*')
_HEAD = re.compile(r'\s*(?:(?:PREFIX\s+[^\s:]*:\s*<>|BASE\s*<>)\s*)*([A-Za-z]+)', re.I)
_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(:\d{2}(?:\.\d+)?)?)?$')

_Fact = namedtuple('_Fact', 'tid text axioms cls role prop')     # 한 항이 어떤 클래스에 속한다는 근거 하나
_Triple = namedtuple('_Triple', 'tid s p o sk ok text')          # 경로를 푼 뒤의 단순 삼중 패턴. sk, ok는 환경의 열쇠


def _is_datatype(c):
    return str(c).startswith(str(XSD)) or c in (RDFS.Literal, RDF.langString, RDF.PlainLiteral, RDF.XMLLiteral, RDF.HTML)


def _closure(direct, nodes):
    """{x: 바로 위 원소 집합} -> {x: x를 포함한 모든 위 원소 집합}"""
    anc = {n: {n} | set(direct.get(n, ())) for n in nodes}
    changed = True
    while changed:
        changed = False
        for s in anc.values():
            add = set().union(*(anc.get(m, {m}) for m in s)) - s
            if add:
                s |= add
                changed = True
    return anc


class TBox:
    """동결한 T-Box에서 질의 검증에 쓰는 폐포. rdflib로 읽어 직접 계산한다."""

    def __init__(self, paths=TBOX_FILES):
        g = rdflib.Graph()
        for p in paths:
            g.parse(p, format='turtle')
        iri = lambda x: isinstance(x, URIRef)
        pairs = lambda pred: [(s, o) for s, o in g.subject_objects(pred) if iri(s) and iri(o)]

        # 클래스와 상위 클래스
        classes = {s for t in (OWL.Class, RDFS.Class) for s in g.subjects(RDF.type, t) if iri(s)}
        sup = defaultdict(set)
        for s, o in pairs(RDFS.subClassOf):
            sup[s].add(o)
        for s, o in pairs(OWL.equivalentClass):
            sup[s].add(o)
            sup[o].add(s)
        # 속성과 상위 속성, 역속성
        prop_types = (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, OWL.FunctionalProperty,
                      OWL.InverseFunctionalProperty, OWL.TransitiveProperty, OWL.SymmetricProperty, RDF.Property)
        props = {s for t in prop_types for s in g.subjects(RDF.type, t) if iri(s)}
        psup, inv, dom, rng = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
        for s, o in pairs(RDFS.subPropertyOf):
            psup[s].add(o)
        for s, o in pairs(OWL.equivalentProperty):
            psup[s].add(o)
            psup[o].add(s)
        for s, o in pairs(OWL.inverseOf):
            inv[s].add(o)
            inv[o].add(s)
        for s, o in pairs(RDFS.domain):
            dom[s].add(o)
        for s, o in pairs(RDFS.range):
            rng[s].add(o)
        props |= set(psup) | {o for v in psup.values() for o in v} | set(inv) | set(dom) | set(rng)
        # 서로소 선언
        declared = set()
        for n in g.subjects(RDF.type, OWL.AllDisjointClasses):
            head = g.value(n, OWL.members)
            members = [m for m in Collection(g, head) if iri(m)] if head is not None else []
            declared |= {frozenset(c) for c in itertools.combinations(members, 2)}
        declared |= {frozenset((s, o)) for s, o in pairs(OWL.disjointWith) if s != o}
        classes |= set(sup) | {o for v in sup.values() for o in v} | {c for v in dom.values() for c in v}
        classes |= {c for v in rng.values() for c in v if not _is_datatype(c)} | {c for pair in declared for c in pair}

        self.classes = classes
        self.props = props
        self._supers = _closure(sup, classes)
        self._psupers = _closure(psup, props)
        subs = defaultdict(set)
        for c, ups in self._supers.items():
            for u in ups:
                subs[u].add(c)
        self.disjoint_declared = declared
        self.disjoint_all = {frozenset((x, y)) for pair in declared for a, b in [tuple(pair)]
                             for x in subs[a] for y in subs[b] if x != y}
        self.partners = defaultdict(set)        # 선언된 서로소 상대. 환경을 상위 클래스로 닫으므로 선언 쌍만 보면 된다
        for pair in declared:
            a, b = tuple(pair)
            self.partners[a].add(b)
            self.partners[b].add(a)

        obj, dat = set(g.subjects(RDF.type, OWL.ObjectProperty)), set(g.subjects(RDF.type, OWL.DatatypeProperty))
        fun = set(g.subjects(RDF.type, OWL.FunctionalProperty))
        self.symmetric = set(g.subjects(RDF.type, OWL.SymmetricProperty))
        self.inverses = {p: sorted(v) for p, v in inv.items()}
        self.kind, self.functional = {}, set()
        self.domain, self.range, self.range_dt = {}, {}, {}      # 속성 -> {클래스: 근거 공리들}. 선언 순서: 직접, 상위 속성, 역속성
        for p in props:
            ups = sorted(self._psupers[p], key=lambda q: (q != p, str(q)))
            is_obj, is_dat = any(q in obj for q in ups) or p in inv, any(q in dat for q in ups)
            if is_obj != is_dat:
                self.kind[p] = 'object' if is_obj else 'datatype'
            if any(q in fun for q in ups):
                self.functional.add(p)
            d, r, rdt = {}, {}, {}
            for q in ups:
                via = () if q == p else ((p, 'rdfs:subPropertyOf', q),)
                for c in sorted(dom.get(q, ())):
                    d.setdefault(c, via + ((q, 'rdfs:domain', c),))
                for c in sorted(rng.get(q, ())):
                    (rdt if _is_datatype(c) else r).setdefault(c, via + ((q, 'rdfs:range', c),))
            for q in sorted(inv.get(p, ())):
                for c in sorted(rng.get(q, ())):
                    if not _is_datatype(c):
                        d.setdefault(c, ((p, 'owl:inverseOf', q), (q, 'rdfs:range', c)))
                for c in sorted(dom.get(q, ())):
                    r.setdefault(c, ((p, 'owl:inverseOf', q), (q, 'rdfs:domain', c)))
            self.domain[p], self.range[p], self.range_dt[p] = d, r, rdt
        self.datetime_props = {p for p, r in self.range_dt.items() if XSD.dateTime in r}

        # T-Box에 선언된 개체(E10 상태, 불량 패턴)
        self.individuals = defaultdict(set)
        for s, o in g.subject_objects(RDF.type):
            if iri(s) and o in classes and s not in classes and s not in props:
                self.individuals[s].add(o)
        self.known_preds = props | {p for p in g.predicates() if iri(p)}
        self.all_terms = classes | self.known_preds | set(self.individuals)
        fab = sparql_common.FAB
        local = lambda terms: sorted(str(t)[len(fab):] for t in terms if str(t).startswith(fab))
        self.fab_props, self.fab_classes, self.fab_individuals = local(props), local(classes), local(self.individuals)
        self.triples = len(g)

    def supers(self, c):
        return self._supers.get(c) or {c}

    def disjoint(self, a, b):
        """a와 b가 서로소인가(선언 또는 하위 클래스로 전파)."""
        return frozenset((a, b)) in self.disjoint_all

    def links(self, a, b, limit=4):
        """클래스 a의 개체와 b의 개체를 잇는 fab: 객체 속성. [(속성, 주어 클래스, 목적어 클래스)]"""
        sa, sb, out = self.supers(a), self.supers(b), []
        for p in sorted(self.props, key=str):
            if self.kind.get(p) != 'object' or not str(p).startswith(sparql_common.FAB):
                continue
            d, r = set(self.domain[p]), set(self.range[p])
            if d & sa and r & sb:
                out.append((p, a, b))
            elif d & sb and r & sa:
                out.append((p, b, a))
        return out[:limit]

    def stats(self):
        fab = sparql_common.FAB
        n_fab = lambda xs: sum(1 for x in xs if str(x).startswith(fab))
        kinds = list(self.kind.items())
        return dict(triples=self.triples, classes=len(self.classes), classes_fab=n_fab(self.classes),
                    properties=len(self.props), properties_fab=n_fab(self.props),
                    object_properties=sum(1 for _, k in kinds if k == 'object'),
                    datatype_properties=sum(1 for _, k in kinds if k == 'datatype'),
                    functional_properties=len(self.functional), inverse_pairs=sum(len(v) for v in self.inverses.values()) // 2,
                    disjoint_pairs_declared=len(self.disjoint_declared), disjoint_pairs_propagated=len(self.disjoint_all),
                    individuals=len(self.individuals), datetime_properties=len(self.datetime_props))


class _Names:
    """IRI와 항을 모델이 쓸 수 있는 표기로 적는다. 접두사는 호출 시점의 sparql_common.PREFIXES와 질의가 선언한 것을 쓴다."""

    def __init__(self, declared=None):
        standard = dict(sparql_common.PREFIXES)
        declared = dict(declared) if declared else standard         # 질의에서 실제로 유효한 선언(with_prefixes가 붙인 것 포함)
        ns = {}
        for prefix, base in declared.items():
            if prefix and base:
                ns[str(base)] = prefix
        for prefix, base in standard.items():       # 한 이름공간에 접두사가 둘이면 표준 접두사로 적는다
            if str(declared.get(prefix)) == str(base):
                ns[str(base)] = prefix
        self.pairs = sorted(ns.items(), key=lambda x: -len(x[0]))

    def iri(self, u):
        s = str(u)
        for base, prefix in self.pairs:
            if s.startswith(base) and re.fullmatch(r'[A-Za-z0-9_][\w.-]*', s[len(base):]):
                return f'{prefix}:{s[len(base):]}'
        return f'<{s}>'

    def literal(self, x):
        lex = str(x)
        lex = (lex[:57] + '...') if len(lex) > 60 else lex
        lex = lex.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        if x.language:
            return f'"{lex}"@{x.language}'
        if x.datatype is None or x.datatype == XSD.string:
            return f'"{lex}"'
        if x.datatype in (XSD.integer, XSD.decimal, XSD.double, XSD.boolean) and re.fullmatch(r'[-+]?[\d.]+(?:[eE][-+]?\d+)?|true|false', lex):
            return lex
        return f'"{lex}"^^{self.iri(x.datatype)}'

    def term(self, x):
        if isinstance(x, Variable):
            return f'?{x}'
        if isinstance(x, URIRef):
            return self.iri(x)
        if isinstance(x, Literal):
            return self.literal(x)
        if isinstance(x, BNode):
            return '[]'
        return '(경로의 중간 노드)'

    def path(self, p, top=True):
        if isinstance(p, URIRef):
            return 'a' if p == RDF.type and top else self.iri(p)
        if isinstance(p, Variable):
            return f'?{p}'
        if isinstance(p, InvPath):
            return '^' + self.path(p.arg, False)
        if isinstance(p, SequencePath):
            s = '/'.join(self.path(a, False) for a in p.args)
            return s if top else f'({s})'
        if isinstance(p, AlternativePath):
            s = '|'.join(self.path(a, False) for a in p.args)
            return s if top else f'({s})'
        if isinstance(p, MulPath):
            return self.path(p.path, False) + str(p.mod)
        if isinstance(p, NegatedPath):
            return '!(' + '|'.join(self.path(a, False) for a in p.args) + ')'
        return '(경로)'

    def triple(self, s, p, o):
        return f'{self.term(s)} {self.path(p)} {self.term(o)}'

    def axioms(self, axioms):
        return [f'{self.iri(s)} {label} {self.iri(o)}' for s, label, o in axioms]


class _Fresh:
    """순차 경로를 풀 때 넣는 중간 노드."""
    __slots__ = ('n',)

    def __init__(self, n):
        self.n = n


class _Node:
    """대수식을 줄인 중간 표현.
    all: 자식이 모두 함께 성립(Join, Filter, Extend 등). bgp: 삼중 패턴. opt: [왼쪽, 선택 본문]. minus: [왼쪽, 뺄 패턴].
    union: 가지. sub: 부질의(Project 경계). exists: 이 노드의 식에 든 (EXISTS 패턴, 극성)."""
    __slots__ = ('kind', 'kids', 'triples', 'exists', 'out')

    def __init__(self, kind, kids=()):
        self.kind, self.kids, self.triples, self.exists, self.out = kind, list(kids), [], [], None


class _Env:
    """함께 성립해야 하는 패턴에서 나온 사실. types: 열쇠 -> {클래스: [근거]}, res: 개체로 쓰인 근거, lit: 리터럴로 쓰인 근거,
    func: (주어 열쇠, 함수 속성) -> [(상수 값, 근거)]. 만든 뒤에는 바꾸지 않는다(merge, common이 새로 만든다)."""
    __slots__ = ('types', 'res', 'lit', 'func')

    def __init__(self):
        self.types, self.res, self.lit, self.func = {}, {}, {}, {}

    def merge(self, other):
        if not (other.types or other.res or other.lit or other.func):
            return self
        out = _Env()
        for src in (self, other):
            for k, tm in src.types.items():
                dst = out.types.setdefault(k, {})
                for c, facts in tm.items():
                    cur = dst.setdefault(c, [])
                    cur.extend(f for f in facts if f not in cur)
            for k, f in src.res.items():
                out.res.setdefault(k, f)
            for k, f in src.lit.items():
                out.lit.setdefault(k, f)
            for k, vals in src.func.items():
                cur = out.func.setdefault(k, [])
                cur.extend(v for v in vals if v not in cur)
        return out

    def common(self, other):
        """두 가지에서 모두 성립하는 사실(UNION 밖으로 내보낼 것)."""
        out = _Env()
        for k, tm in self.types.items():
            both = {c: facts + [f for f in other.types[k][c] if f not in facts] for c, facts in tm.items() if c in other.types.get(k, ())}
            if both:
                out.types[k] = both
        out.res = {k: f for k, f in self.res.items() if k in other.res}
        out.lit = {k: f for k, f in self.lit.items() if k in other.lit}
        for k, vals in self.func.items():
            both = [v for v in vals if any(_same_value(v[0], w[0]) for w in other.func.get(k, ()))]
            if both:
                out.func[k] = both
        return out


_EMPTY = _Env()


def _same_value(a, b):
    """두 상수가 같은 값인가. 판정할 수 없으면 같다고 본다(오류를 내지 않는 쪽)."""
    if a == b:
        return True
    if isinstance(a, Literal) and isinstance(b, Literal):
        try:
            return bool(a.eq(b))
        except Exception:
            return True
    return False


def _plain(x):
    return isinstance(x, Literal) and (x.datatype is None or x.datatype == XSD.string)


class _Run:
    """질의 1건의 점검 상태."""

    def __init__(self, tbox, names):
        self.tb, self.nm = tbox, names
        self.findings, self._seen = [], set()
        self.unknown = set()
        self.patterns = 0                   # 원래 삼중 패턴 수(경로를 풀기 전)
        self.complex_paths, self.skipped = [], []
        self.graph_uses = []
        self._tid = self._fresh = self._scope = 0
        self.dt_vars = {}                   # xsd:dateTime 값을 받는 변수 열쇠 -> 속성
        self.aliases, self.comparisons, self.agg_text = [], [], {}
        self._reported, self._soft_reported, self._reversed = set(), set(), set()

    # ---- 결과 ---------------------------------------------------------------
    def add(self, code, severity, pattern, rule, message, hint):
        key = (code, pattern, rule)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(dict(code=code, severity=severity, pattern=pattern, rule=rule, message=message, hint=hint))

    # ---- 대수식 -> 중간 표현 --------------------------------------------------
    def key(self, x, scopes):
        if isinstance(x, Variable):
            name = str(x)
            for sid, projected in reversed(scopes):         # 부질의가 투영하지 않은 변수는 그 부질의 안의 변수다
                if name not in projected:
                    return ('v', name, sid)
            return ('v', name, 0)
        if isinstance(x, URIRef):
            return ('i', str(x))
        if isinstance(x, BNode):
            return ('b', str(x))
        if isinstance(x, _Fresh):
            return ('p', x.n)
        return None

    def build(self, n, scopes):
        if not isinstance(n, CompValue):
            return _Node('all')
        name = n.name
        if name == 'BGP':
            return self.bgp(n['triples'], scopes)
        if name == 'TriplesBlock':          # 번역되지 않은 구문 트리가 남은 경우
            flat = [x for t in n['triples'] for x in t]
            return self.bgp([tuple(flat[i:i + 3]) for i in range(0, len(flat) - 2, 3)], scopes)
        if name == 'LeftJoin':
            left, right = self.build(n['p1'], scopes), self.build(n['p2'], scopes)
            found = self.exprs(n['expr'], scopes, top=True)         # OPTIONAL 본문의 FILTER
            if found:
                right = _Node('all', [right])
                right.exists = found
            return _Node('opt', [left, right])
        if name in ('Minus', 'Union'):
            return _Node(name.lower(), [self.build(n['p1'], scopes), self.build(n['p2'], scopes)])
        if name == 'Graph':
            self.graph_uses.append(('GRAPH', n['term']))
            return _Node('all', [self.build(n['p'], scopes)])
        if name == 'ServiceGraphPattern':
            self.graph_uses.append(('SERVICE', n['term'] if 'term' in n else None))
            return _Node('all')
        if name == 'Project':
            self._scope += 1
            inner = scopes + ((self._scope, frozenset(str(v) for v in n['PV'])),)
            return _Node('sub', [self.build(n['p'], inner)])
        if name == 'Extend':
            self.alias(self.key(n['var'], scopes), n['expr'], scopes)
        if name == 'AggregateJoin':
            for a in n['A']:
                if isinstance(a, CompValue) and a.name in ('Aggregate_Min', 'Aggregate_Max', 'Aggregate_Sample') and 'res' in a:
                    self.alias(self.key(a['res'], scopes), a['vars'], scopes)
                    if isinstance(a['vars'], Variable):         # 집계 결과를 담는 내부 변수는 원래 식으로 적는다
                        self.agg_text[self.key(a['res'], scopes)] = f"{a.name[len('Aggregate_'):].upper()}(?{a['vars']})"
        node = _Node('all', [self.build(n[k], scopes) for k in ('p', 'p1', 'p2') if k in n and isinstance(n[k], CompValue)])
        rest = [v for k, v in n.items() if k not in ('p', 'p1', 'p2', '_vars', 'PV')]
        node.exists = self.exprs(rest[0] if name == 'Filter' and len(rest) == 1 else rest, scopes, top=name == 'Filter')
        return node

    def bgp(self, triples, scopes):
        node = _Node('bgp')
        for s, p, o in triples:
            self.patterns += 1
            text = self.nm.triple(s, p, o)
            for s2, p2, o2 in self.expand(s, p, o, text):
                self._tid += 1
                t = _Triple(self._tid, s2, p2, o2, self.key(s2, scopes), self.key(o2, scopes), text)
                node.triples.append(t)
                self.lexical(t)
        return node

    def expand(self, s, p, o, text):
        """순차 경로와 역방향 경로를 단순 삼중 패턴으로 푼다. 그 밖의 경로는 속성 선언만 확인한다."""
        if isinstance(p, (URIRef, Variable)):
            yield s, p, o
        elif isinstance(p, InvPath):
            yield from self.expand(o, p.arg, s, text)
        elif isinstance(p, SequencePath):
            cur = s
            for i, part in enumerate(p.args):
                if i == len(p.args) - 1:
                    nxt = o
                else:
                    self._fresh += 1
                    nxt = _Fresh(self._fresh)
                yield from self.expand(cur, part, nxt, text)
                cur = nxt
        else:
            note = f'이 질의의 경로 {self.nm.path(p)}: 속성이 선언되어 있는지만 확인했다'
            if note not in self.complex_paths:
                self.complex_paths.append(note)
            for q in self.path_iris(p):
                self.check_predicate(q, text)

    def path_iris(self, p):
        if isinstance(p, URIRef):
            yield p
        elif isinstance(p, InvPath):
            yield from self.path_iris(p.arg)
        elif isinstance(p, MulPath):
            yield from self.path_iris(p.path)
        elif isinstance(p, (SequencePath, AlternativePath, NegatedPath)):
            for a in p.args:
                yield from self.path_iris(a)

    def exprs(self, e, scopes, top=False):
        """식에 든 EXISTS 패턴 [(노드, 극성)]. 극성: pos(FILTER의 최상위 논리곱 항), neg(NOT EXISTS), mixed(그 밖). 비교식도 모은다."""
        out = []
        for c in (self.conjuncts(e) if top else [e]):
            neg, x = False, c
            while top and isinstance(x, CompValue) and x.name == 'UnaryNot' and 'expr' in x:
                neg, x = not neg, x['expr']
            if top and isinstance(x, CompValue) and x.name in ('Builtin_EXISTS', 'Builtin_NOTEXISTS'):
                negated = neg != (x.name == 'Builtin_NOTEXISTS')
                out.append((self.exists_graph(x, scopes), 'neg' if negated else 'pos'))
            else:
                self.scan(c, scopes, out)
        return out

    def conjuncts(self, e):
        if isinstance(e, CompValue) and e.name == 'ConditionalAndExpression':
            yield from self.conjuncts(e['expr'])
            for x in e['other']:
                yield from self.conjuncts(x)
        else:
            yield e

    def scan(self, e, scopes, out):
        if isinstance(e, CompValue):
            if e.name in ('Builtin_EXISTS', 'Builtin_NOTEXISTS'):
                out.append((self.exists_graph(e, scopes), 'mixed'))
                return
            if e.name == 'RelationalExpression' and 'op' in e:
                self.comparisons.append((str(e['op']), e['expr'], e['other'], scopes))
            for k, v in e.items():
                if k != '_vars':
                    self.scan(v, scopes, out)
        elif isinstance(e, (list, tuple)):
            for v in e:
                self.scan(v, scopes, out)

    def exists_graph(self, e, scopes):
        g = e.__dict__.get('graph')         # rdflib는 FILTER와 BIND의 EXISTS만 번역해 속성에 둔다
        if not isinstance(g, CompValue):
            try:                            # SELECT 식, HAVING, ORDER BY의 EXISTS는 직접 번역한다
                g = algebra.translateGroupGraphPattern(algebra.traverse(e['graph'], visitPost=algebra.translatePath))
            except Exception:
                self.skipped.append('EXISTS 안의 패턴 일부(번역하지 못함)')
                return _Node('all')
        return self.build(g, scopes)

    def alias(self, target, expr, scopes):
        if isinstance(expr, Variable):
            self.aliases.append((target, 'same', self.key(expr, scopes)))
        elif isinstance(expr, CompValue) and expr.name == 'Builtin_STR' and isinstance(expr.get('arg'), Variable):
            self.aliases.append((target, 'str', self.key(expr['arg'], scopes)))

    # ---- 삼중 패턴 하나로 판정하는 것 ------------------------------------------
    def lexical(self, t):
        nm, tb, p = self.nm, self.tb, t.p
        if not isinstance(p, URIRef):
            return
        if p == RDF.type:
            if isinstance(t.o, URIRef):
                self.check_class(t.o, t.text)
            return
        self.check_predicate(p, t.text)
        kind, name = tb.kind.get(p), nm.iri(p)
        if p in tb.datetime_props and t.ok and t.ok[0] != 'i':
            self.dt_vars.setdefault(t.ok, p)
        if kind == 'object' and isinstance(t.o, Literal):
            rng = next(iter(tb.range[p]), None)
            rule = [f'{name} rdf:type owl:ObjectProperty'] + (nm.axioms(tb.range[p][rng]) if rng else [])
            hint = (f'목적어는 {nm.iri(rng)} 개체여야 한다. ' if rng else '목적어는 개체여야 한다. ') + \
                f'이름으로 찾으려면 목적어를 변수로 바꾸고 `?o fab:identifier {nm.literal(t.o)}`를 추가한다.'
            self.add('OBJECT_VS_DATATYPE', ERROR, t.text, '; '.join(rule), f'{name} 속성은 객체 속성인데 목적어에 리터럴을 썼다.', hint)
        elif kind == 'datatype' and isinstance(t.o, URIRef):
            dt = next(iter(tb.range_dt[p]), None)
            rule = [f'{name} rdf:type owl:DatatypeProperty'] + (nm.axioms(tb.range_dt[p][dt]) if dt else [])
            self.add('OBJECT_VS_DATATYPE', ERROR, t.text, '; '.join(rule), f'{name} 속성은 데이터 속성인데 목적어에 IRI를 썼다.',
                     '목적어에는 리터럴 값이나 변수를 쓴다' + (f'(치역 {nm.iri(dt)}).' if dt else '.'))

    def suggest(self, local, pool):
        """철자가 가까운 이름과 한쪽이 다른 쪽을 포함하는 이름. 최대 3개."""
        low, want = {}, local.lower()
        for x in pool:
            low.setdefault(x.lower(), x)
        hits = difflib.get_close_matches(want, list(low), n=3, cutoff=0.6)
        if len(want) >= 3:
            hits += [x for x in low if x not in hits and (want in x or (len(x) >= 4 and x in want))]
        return [f'fab:{low[h]}' for h in hits[:3]]

    def check_predicate(self, p, text):
        tb, s, name = self.tb, str(p), self.nm.iri(p)
        if p in tb.known_preds or s.startswith(BUILTIN_NS):
            return
        fab = sparql_common.FAB
        if not s.startswith(fab):
            if p not in tb.all_terms:
                self.unknown.add(s)
                self.add('UNKNOWN_NAMESPACE', WARNING, text, NO_DECL, f'{name} 이름은 T-Box에 없는 이름공간에 속한다.',
                         'KG의 속성은 fab: 이름공간에 선언되어 있다. 결과가 비면 fab: 속성으로 바꾼다.')
            return
        local = s[len(fab):]
        if p in tb.classes:
            self.add('UNKNOWN_TERM', ERROR, text, f'{name} rdf:type owl:Class', f'{name} 이름은 속성이 아니라 클래스로 선언되어 있다.',
                     f'클래스는 `?x a {name}` 형태로 쓴다. 속성 이름은 T-Box에서 확인한다.')
        elif p in tb.individuals:
            cls = self.nm.iri(sorted(tb.individuals[p])[0])
            self.add('UNKNOWN_TERM', ERROR, text, f'{name} rdf:type {cls}', f'{name} 이름은 속성이 아니라 {cls} 클래스의 개체다.',
                     '개체는 속성의 목적어 자리에 쓴다.')
        else:
            self.unknown.add(s)
            near = self.suggest(local, tb.fab_props)
            hint = ('비슷한 속성: ' + ', '.join(near) + '.') if near else '비슷한 이름의 속성이 없다. T-Box에 선언된 속성만 쓸 수 있다.'
            self.add('UNKNOWN_TERM', ERROR, text, NO_DECL, f'{name} 속성은 T-Box에 없다.', hint)

    def check_class(self, c, text):
        tb, s, name = self.tb, str(c), self.nm.iri(c)
        if c in tb.classes or s.startswith(BUILTIN_NS):
            return
        fab = sparql_common.FAB
        if not s.startswith(fab):
            if c not in tb.all_terms:
                self.unknown.add(s)
                self.add('UNKNOWN_NAMESPACE', WARNING, text, NO_DECL, f'{name} 이름은 T-Box에 없는 이름공간에 속한다.',
                         'KG의 클래스는 fab: 이름공간에 선언되어 있다. 결과가 비면 fab: 클래스로 바꾼다.')
            return
        local = s[len(fab):]
        if c in tb.individuals:
            cls = sorted(tb.individuals[c])[0]
            via = [self.nm.iri(p) for p in sorted(tb.props, key=str) if cls in tb.range.get(p, ())][:2]
            hint = f'개체는 속성의 목적어로 쓴다(예: `?x {via[0]} {name}`).' if via else '개체는 속성의 목적어 자리에 쓴다.'
            self.add('UNKNOWN_TERM', ERROR, text, f'{name} rdf:type {self.nm.iri(cls)}',
                     f'{name} 이름은 클래스가 아니라 {self.nm.iri(cls)} 클래스의 개체다.', hint)
        elif c in tb.props:
            self.add('UNKNOWN_TERM', ERROR, text, f'{name} rdf:type rdf:Property', f'{name} 이름은 클래스가 아니라 속성으로 선언되어 있다.',
                     f'속성은 `?s {name} ?o` 형태로 쓴다.')
        else:
            self.unknown.add(s)
            near = self.suggest(local, tb.fab_classes)
            hint = ('비슷한 클래스: ' + ', '.join(near) + '.') if near else '비슷한 이름의 클래스가 없다. T-Box에 선언된 클래스만 쓸 수 있다.'
            self.add('UNKNOWN_TERM', ERROR, text, NO_DECL, f'{name} 클래스는 T-Box에 없다.', hint)

    # ---- 함께 성립해야 하는 패턴의 사실 ------------------------------------------
    def outflow(self, n):
        """노드의 모든 해에서 성립하는 사실. OPTIONAL 본문, MINUS, EXISTS의 사실은 넣지 않는다."""
        if n.out is None:
            if n.kind == 'bgp':
                n.out = self.facts(n.triples)
            elif n.kind in ('opt', 'minus'):
                n.out = self.outflow(n.kids[0])
            elif n.kind == 'union':
                n.out = self.outflow(n.kids[0]).common(self.outflow(n.kids[1]))
            else:
                out = _EMPTY
                for k in n.kids:
                    out = out.merge(self.outflow(k)) if out is not _EMPTY else self.outflow(k)
                n.out = out
        return n.out

    def facts(self, triples):
        env, tb = _Env(), self.tb

        def typed(key, cls, fact):
            tm = env.types.setdefault(key, {})
            for c in tb.supers(cls):
                tm.setdefault(c, []).append(fact)

        def touch(key, term, t):            # T-Box에 선언된 개체는 선언된 클래스를 갖는다(tid 0: 어느 삼중 패턴에도 기대지 않는 근거)
            if key and key[0] == 'i' and key not in env.types:
                for c in sorted(tb.individuals.get(term, ())):
                    typed(key, c, _Fact(0, t.text, ((term, 'rdf:type', c),), c, 'individual', None))

        variable = lambda key: key is not None and key[0] != 'i'
        for t in triples:
            touch(t.sk, t.s, t)
            touch(t.ok, t.o, t)
            if variable(t.sk):
                env.res.setdefault(t.sk, _Fact(t.tid, t.text, (), None, 'subject', t.p if isinstance(t.p, URIRef) else None))
            if not isinstance(t.p, URIRef) or t.sk is None:
                continue
            if t.p == RDF.type:
                if isinstance(t.o, URIRef) and t.o in tb.classes:
                    typed(t.sk, t.o, _Fact(t.tid, t.text, (), t.o, 'type', None))
                continue
            for c, ax in tb.domain.get(t.p, {}).items():
                typed(t.sk, c, _Fact(t.tid, t.text, ax, c, 'domain', t.p))
            kind = tb.kind.get(t.p)
            if t.ok is not None:
                for c, ax in tb.range.get(t.p, {}).items():
                    typed(t.ok, c, _Fact(t.tid, t.text, ax, c, 'range', t.p))
            if kind == 'object' and variable(t.ok):
                env.res.setdefault(t.ok, _Fact(t.tid, t.text, ((t.p, 'rdf:type', OWL.ObjectProperty),), None, 'object', t.p))
            elif kind == 'datatype' and variable(t.ok):
                env.lit.setdefault(t.ok, _Fact(t.tid, t.text, ((t.p, 'rdf:type', OWL.DatatypeProperty),), None, 'value', t.p))
            if t.p in tb.functional and isinstance(t.o, (URIRef, Literal)):
                env.func.setdefault((t.sk, t.p), []).append((t.o, _Fact(t.tid, t.text, (), None, 'value', t.p)))
        return env

    def check(self, n, ctx, soft=False):
        """n과 그 문맥 ctx가 함께 성립할 수 있는지 본다. soft: 새로 나온 충돌을 경고(VACUOUS_NEGATION)로 낸다."""
        env = ctx.merge(self.outflow(n)) if ctx is not _EMPTY else self.outflow(n)
        self.conflicts(env, soft)

        def visit(m):
            if m.kind == 'bgp':
                for t in m.triples:
                    self.reversed_direction(t, env)
                return
            if m.kind == 'opt':
                visit(m.kids[0])
                self.check(m.kids[1], env, soft)            # 선택 본문은 왼쪽 해와 함께 성립해야 쓰인다
            elif m.kind == 'minus':
                visit(m.kids[0])
                self.check(m.kids[1], _EMPTY, soft)
                self.check(m.kids[1], env, True)
            elif m.kind == 'union':
                for b in m.kids:
                    self.check(b, env, soft)
            elif m.kind == 'sub':
                self.check(m.kids[0], _EMPTY, soft)         # 부질의는 따로 평가한다. 투영한 변수의 사실은 outflow로 바깥에 합쳐져 있다
            else:
                for k in m.kids:
                    visit(k)
            for g, polarity in m.exists:
                if polarity == 'pos':
                    self.check(g, env, soft)
                else:
                    self.check(g, _EMPTY, soft)
                    self.check(g, env, True)

        visit(n)

    def show(self, key):
        return {'v': lambda: f'변수 ?{key[1]}', 'i': lambda: f'항 {self.nm.iri(key[1])}', 'b': lambda: '빈 노드 []'}.get(key[0], lambda: '경로의 중간 노드')()

    def conflict(self, soft, code, pattern, rule, message, hint):
        if soft:
            self.add('VACUOUS_NEGATION', WARNING, pattern, rule,
                     'NOT EXISTS, MINUS 또는 EXISTS 안의 패턴이 바깥 패턴과 함께 성립할 수 없다. 이 조건은 항상 같은 값이라 결과를 바꾸지 않는다. ' + message, hint)
        else:
            self.add(code, ERROR, pattern, rule, message, hint)

    def conflicts(self, env, soft):
        nm, tb = self.nm, self.tb
        role = lambda f: {'type': '명시', 'domain': f'{nm.iri(f.prop)}의 주어' if f.prop else '', 'range': f'{nm.iri(f.prop)}의 목적어' if f.prop else '',
                          'individual': 'T-Box에 선언된 개체'}.get(f.role, '')
        both = lambda a, b: a.text if a.text == b.text else f'{a.text} . {b.text}'

        def new(*mark):                     # 같은 충돌은 한 번만 낸다. 경고로 낸 충돌이 다른 곳에서 오류로 확인되면 오류도 낸다
            if mark in self._reported or (soft and mark in self._soft_reported):
                return False
            (self._soft_reported if soft else self._reported).add(mark)
            return True

        for key, tm in env.types.items():
            for c1 in sorted(tm, key=str):
                for c2 in sorted(tb.partners.get(c1, set()) & tm.keys(), key=str):
                    if str(c1) >= str(c2) or not new('types', key, c1, c2):
                        continue
                    f1, f2 = tm[c1][0], tm[c2][0]
                    rule = nm.axioms(f1.axioms) + nm.axioms(f2.axioms)
                    rule += [f'{nm.iri(f.cls)} rdfs:subClassOf {nm.iri(c)}' for f, c in ((f1, c1), (f2, c2)) if f.cls != c]
                    rule.append(f'{nm.iri(c1)} owl:disjointWith {nm.iri(c2)}')
                    links = tb.links(f1.cls, f2.cls)
                    hint = '한 개체가 두 클래스에 함께 속할 수 없다. 변수를 둘로 나누고 두 개체를 잇는 속성을 쓴다'
                    hint += (': ' + ', '.join(f'{nm.iri(p)}({nm.iri(a)}에서 {nm.iri(b)})' for p, a, b in links) + '.') if links else '.'
                    self.conflict(soft, 'DISJOINT_TYPES', both(f1, f2), '; '.join(dict.fromkeys(rule)),
                                  f'{self.show(key)}에 서로소인 두 클래스가 함께 요구된다: {nm.iri(f1.cls)}({role(f1)}), {nm.iri(f2.cls)}({role(f2)}).', hint)
        for key, lit in env.lit.items():
            res = env.res.get(key)          # 주어로 쓰였거나 객체 속성의 목적어로 쓰인 근거
            if res is None or not new('kind', key):
                continue
            how = {'subject': '주어', 'object': f'객체 속성 {nm.iri(res.prop)}의 목적어' if res.prop else '객체 속성의 목적어'}.get(res.role, '클래스의 개체')
            rule = nm.axioms(lit.axioms) + nm.axioms(res.axioms)
            self.conflict(soft, 'OBJECT_VS_DATATYPE', both(lit, res), '; '.join(dict.fromkeys(rule)),
                          f'{self.show(key)}의 쓰임이 맞지 않는다. 데이터 속성 {nm.iri(lit.prop)}의 값(리터럴)이면서 {how}로도 쓰인다.',
                          '리터럴 값은 주어나 객체 속성의 목적어가 될 수 없다. 값과 개체에 서로 다른 변수를 쓴다.')
        for (key, p), vals in env.func.items():
            for (o1, f1), (o2, f2) in itertools.combinations(vals, 2):
                if _same_value(o1, o2) or not new('func', key, p):
                    continue
                self.conflict(soft, 'FUNCTIONAL_CONFLICT', both(f1, f2), f'{nm.iri(p)} rdf:type owl:FunctionalProperty',
                              f'함수 속성 {nm.iri(p)}의 값은 주어마다 하나다. 같은 주어에 서로 다른 두 값을 함께 요구한다: {nm.term(o1)}, {nm.term(o2)}.',
                              '여러 값 가운데 하나를 찾으려면 목적어를 변수로 두고 VALUES나 FILTER(?v IN (...))를 쓰거나 UNION으로 나눈다.')

    def reversed_direction(self, t, env):
        tb, nm, p = self.tb, self.nm, t.p
        if t.tid in self._reversed or not isinstance(p, URIRef) or tb.kind.get(p) != 'object' or p in tb.symmetric:
            return
        dom, rng = set(tb.domain.get(p, ())), set(tb.range.get(p, ()))
        if not dom or not rng or dom == rng or t.sk is None or t.ok is None:
            return
        # 이 삼중 패턴이 아닌 다른 근거로 알려진 클래스만 본다(자기 정의역·치역 추론은 방향의 근거가 아니다)
        other = lambda key: {c for c, facts in env.types.get(key, {}).items() if any(f.tid != t.tid for f in facts)}
        ts, to = other(t.sk), other(t.ok)
        if not (rng <= ts and dom <= to) or (dom <= ts and rng <= to):
            return
        self._reversed.add(t.tid)
        d, r = next(iter(tb.domain[p])), next(iter(tb.range[p]))
        rule = nm.axioms(tb.domain[p][d]) + nm.axioms(tb.range[p][r])
        simple = t.text == nm.triple(t.s, t.p, t.o)
        hint = f'주어와 목적어를 바꾼다: `{nm.triple(t.o, t.p, t.s)}`.' if simple else f'경로에서 이 속성의 방향을 바꾼다: ^{nm.iri(p)}.'
        inv = tb.inverses.get(p)
        if inv:
            rule.append(f'{nm.iri(p)} owl:inverseOf {nm.iri(inv[0])}')
            hint += f' 역속성을 써도 된다: {nm.iri(inv[0])}.'
        self.add('REVERSED_DIRECTION', WARNING, t.text, '; '.join(dict.fromkeys(rule)),
                 f'{nm.iri(p)} 속성의 방향이 반대로 보인다(정의역 {nm.iri(d)}, 치역 {nm.iri(r)}). '
                 '질의에서는 주어 자리의 항이 치역 클래스로, 목적어 자리의 항이 정의역 클래스로 쓰였다.', hint)

    # ---- 시각 비교 -------------------------------------------------------------
    def datetime_comparisons(self):
        nm = self.nm
        dt, as_str = dict(self.dt_vars), {}
        for _ in range(len(self.aliases) + 1):          # BIND, 집계(MIN, MAX, SAMPLE)로 옮겨진 변수
            before = len(dt) + len(as_str)
            for target, how, source in self.aliases:
                if how == 'same' and source in dt:
                    dt.setdefault(target, dt[source])
                elif how == 'same' and source in as_str:
                    as_str.setdefault(target, as_str[source])
                elif how == 'str' and source in dt:
                    as_str.setdefault(target, dt[source])
            if len(dt) + len(as_str) == before:
                break

        def operand(x, scopes):
            if isinstance(x, Variable):
                k = self.key(x, scopes)
                text = self.agg_text.get(k, f'?{x}')
                return ('dt', dt[k], text) if k in dt else ('str', as_str[k], text) if k in as_str else ('other', None, text)
            if isinstance(x, CompValue) and x.name == 'Builtin_STR' and isinstance(x.get('arg'), Variable):
                k = self.key(x['arg'], scopes)
                return ('str', dt[k], f'STR(?{x["arg"]})') if k in dt else ('other', None, f'STR(?{x["arg"]})')
            if isinstance(x, Literal):
                return ('plain' if _plain(x) else 'typed', x, nm.literal(x))
            if isinstance(x, URIRef):
                return ('other', None, nm.iri(x))
            return ('other', None, '(식)')

        def fixed(x):
            m = _DATE.match(str(x).strip()) if isinstance(x, Literal) else None
            if not m:
                return '"2018-03-02T00:00:00"^^xsd:dateTime'
            return f'"{m.group(1)}T{m.group(2) or "00:00"}{m.group(3) or ":00"}"^^xsd:dateTime'

        for op, a, b, scopes in self.comparisons:
            left = operand(a, scopes)
            rights = [operand(x, scopes) for x in b] if isinstance(b, (list, tuple)) else [operand(b, scopes)]
            if op not in COMPARE_OPS and op not in ('IN', 'NOT IN'):
                continue
            text = f'{left[2]} {op} ' + (rights[0][2] if op in COMPARE_OPS else '(' + ', '.join(r[2] for r in rights) + ')')
            for x, y in [(left, r) for r in rights] + [(r, left) for r in rights]:
                if x[0] == 'dt' and y[0] == 'plain':
                    self.add('DATETIME_AS_STRING', WARNING, text, f'{nm.iri(x[1])} rdfs:range xsd:dateTime',
                             f'xsd:dateTime 값({x[2]})을 문자열({y[2]})과 비교한다. 형이 달라 비교가 성립하지 않는다.',
                             f'{fixed(y[1])} 형식의 리터럴과 비교한다.')
                    break
                if x[0] == 'str':
                    self.add('DATETIME_AS_STRING', WARNING, text, f'{nm.iri(x[1])} rdfs:range xsd:dateTime',
                             f'xsd:dateTime 값을 문자열로 바꿔 비교한다({x[2]}). 문자열 비교는 소수부 자릿수가 다른 시각의 순서를 잘못 판정한다.',
                             f'STR()을 빼고 변수를 {fixed(y[1])} 형식의 리터럴과 비교한다.')
                    break


def _strip(text):
    """문자열, IRI, 주석을 지운 질의문(예약어 탐지용). IRI는 <>로 남긴다."""
    return _NOISE.sub(lambda m: '<>' if m.group(1) else ' ', text)


class Checker:
    def __init__(self, paths=TBOX_FILES):
        self.tbox = TBox(paths)
        self._engine = None
        self.check('SELECT ?x WHERE { ?x a fab:Lot ; fab:identifier ?i . FILTER(?i != "") } LIMIT 1')      # 파서 준비

    def stats(self):
        return self.tbox.stats()

    def engine_accepts(self, text):
        """실행 엔진(pyoxigraph)의 파서가 질의를 받는가. (True | False | None(엔진 없음), 엔진의 오류문).
        빈 저장소에 질의를 넘겨 구문만 본다. 결과는 읽지 않는다."""
        if self._engine is None:
            try:
                import pyoxigraph
                self._engine = pyoxigraph.Store()
            except Exception:
                self._engine = False
        if self._engine is False:
            return None, ''
        try:
            self._engine.query(text)
            return True, ''
        except SyntaxError as e:
            return False, str(e)
        except Exception:
            return None, ''

    def check(self, query):
        """질의문을 점검한다. 접두사는 질의 도구와 같게 with_prefixes로 붙인 뒤 읽는다. 예외를 내지 않는다."""
        run = _Run(self.tbox, _Names())
        note = None
        try:
            note = self._check(query, run)
        except Exception as e:              # 검증기의 결함이 조건 D의 실행을 막지 않게 한다
            run.findings = [f for f in run.findings if f['severity'] == ERROR and f['code'] in ('SYNTAX', 'NOT_SELECT', 'GRAPH_NOT_ALLOWED')]
            note = f'검증기 내부 오류로 T-Box 점검을 건너뛰었다({type(e).__name__})'
        findings = sorted(run.findings, key=lambda f: f['severity'] != ERROR)
        extra = run.complex_paths + list(dict.fromkeys(run.skipped)) + ([note] if note else [])
        return dict(ok=not any(f['severity'] == ERROR for f in findings), findings=findings, checked=run.patterns,
                    not_checked=list(NOT_CHECKED) + extra, unknown_terms=sorted(run.unknown))

    def _check(self, query, run):
        if not isinstance(query, str) or not query.strip():
            run.add('SYNTAX', ERROR, '', 'SPARQL 1.1 문법', '질의가 비어 있다.', 'SELECT 또는 ASK 질의를 쓴다.')
            return None
        full = sparql_common.with_prefixes(query)
        offset = full[:len(full) - len(query)].count('\n') if full.endswith(query) else 0
        bare = _strip(full)
        m = _HEAD.match(bare)
        word = m.group(1).upper() if m else ''
        if word in NON_SELECT_WORDS:
            run.add('NOT_SELECT', ERROR, word, TOOL_RULE_SELECT, f'{word} 질의는 실행할 수 없다.', 'SELECT 또는 ASK 질의로 쓴다.')
            return None

        problem, q = None, None             # problem: (메시지, 힌트, 위치 줄)
        log = logging.getLogger('rdflib.term')
        with _LOCK:
            level = log.level
            log.setLevel(logging.ERROR)     # 형식이 틀린 리터럴의 변환 경고를 내지 않는다
            try:
                parsed = parser.parseQuery(full)
                declared = {(x['prefix'] if 'prefix' in x else ''): x['iri'] for x in parsed[0] if x.name == 'PrefixDecl'}
                used = set()
                self._pnames(parsed[1], used)
                missing = sorted(used - set(declared))
                if missing:
                    problem = (f'선언하지 않은 접두사를 썼다: {missing[0]}:', self._prefix_hint(), f'{missing[0]}:')
                else:
                    q = algebra.translateQuery(parsed)
                    run.nm = _Names(declared)
            except Exception as e:
                problem = self._syntax_problem(e, full, offset)
            finally:
                log.setLevel(level)

        if problem:
            accepted, detail = self.engine_accepts(full)
            if accepted:                    # rdflib만 읽지 못한다. 실행은 되는 질의이므로 막지 않는다
                self._keywords_only(bare, word, run)
                return 'rdflib 파서가 이 질의를 읽지 못해 T-Box 점검을 하지 못했다'
            message, hint, line = problem
            if accepted is False and detail and 'Prefix not found' in detail and '접두사' not in message:
                message, hint = '선언하지 않은 접두사를 썼다.', self._prefix_hint()
            run.add('SYNTAX', ERROR, line, 'SPARQL 1.1 문법', message, hint)
            return None

        name = q.algebra.name
        if name not in ('SelectQuery', 'AskQuery'):
            word = name.replace('Query', '').upper()
            run.add('NOT_SELECT', ERROR, word, TOOL_RULE_SELECT, f'{word} 질의는 실행할 수 없다.', 'SELECT 또는 ASK 질의로 쓴다.')
            return None
        for d in q.algebra['datasetClause'] or []:
            named = 'named' in d
            run.graph_uses.append(('FROM NAMED' if named else 'FROM', d['named'] if named else d['default']))
        root = run.build(q.algebra['p'], ())
        for word, term in run.graph_uses:
            target = run.nm.term(term) if term is not None else ''
            hint = f'{word} 절을 지우고 기본 그래프의 패턴으로 쓴다.'
            if term is not None and str(term) == sparql_common.VIOLATION_GRAPH:      # 조건 D는 위반 그래프를 기본 그래프에 합쳐 조회한다
                hint += ' 제약 위반 사실(fab:ConstraintViolation과 하위 클래스)은 기본 그래프에서 함께 조회된다. 같은 패턴을 그래프 지정 없이 쓴다.'
            run.add('GRAPH_NOT_ALLOWED', ERROR, f'{word} {target}'.strip(), TOOL_RULE_GRAPH,
                    f'{word} 절은 쓸 수 없다. 질의 도구는 기본 그래프만 조회한다.', hint)
        run.check(root, _EMPTY)
        run.datetime_comparisons()
        return None

    def _pnames(self, n, used):
        if isinstance(n, CompValue):
            if n.name == 'pname':
                used.add(n['prefix'] if 'prefix' in n else '')
            for k, v in n.items():
                self._pnames(v, used)
        elif isinstance(n, (list, tuple)) or type(n).__name__ == 'ParseResults':
            for v in n:
                self._pnames(v, used)

    def _prefix_hint(self):
        return '선언 없이 쓸 수 있는 접두사: ' + ', '.join(f'{p}:' for p in sparql_common.PREFIXES) + '. 다른 접두사는 질의 앞에 PREFIX로 선언한다.'

    def _syntax_problem(self, e, full, offset):
        text = str(e)
        m = re.search(r'Unknown namespace prefix\s*:\s*(\S*)', text)
        if m:
            return f'선언하지 않은 접두사를 썼다: {m.group(1)}:', self._prefix_hint(), f'{m.group(1)}:'
        lineno, col = getattr(e, 'lineno', None), getattr(e, 'col', None)
        if not isinstance(lineno, int) or not isinstance(col, int):
            return f'SPARQL 구문 오류로 질의를 읽을 수 없다({text[:120]}).', '괄호와 중괄호의 짝, 마침표와 세미콜론, 변수 이름(?x), 접두사 표기를 확인한다.', ''
        lines = full.split('\n')
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ''
        near = line[max(0, col - 41):col + 39].strip()
        found = re.search(r"found (.*?)\s+\(at char", text)
        detail = f'{found.group(1)} 앞에서 막혔다' if found else text[:100]
        return (f'SPARQL 구문 오류: {max(1, lineno - offset)}행 {col}열 부근에서 질의를 읽을 수 없다({detail}).',
                '괄호와 중괄호의 짝, 마침표와 세미콜론, 변수 이름(?x), 접두사 표기를 확인한다.', near)

    def _keywords_only(self, bare, word, run):
        """rdflib로 읽지 못한 질의에서 도구 규칙만 본다(예약어 탐지)."""
        if word and word not in ('SELECT', 'ASK'):
            run.add('NOT_SELECT', ERROR, word, TOOL_RULE_SELECT, f'{word} 질의는 실행할 수 없다.', 'SELECT 또는 ASK 질의로 쓴다.')
        for kw, pat in (('GRAPH', r'(?<![:\w?$])GRAPH\s*(?:<>|[?$]\w|\w*:)'), ('SERVICE', r'(?<![:\w?$])SERVICE\s+(?:SILENT\s+)?(?:<>|[?$]\w|\w*:)'),
                        ('FROM', r'(?<![:\w?$])FROM\s+(?:NAMED\s+)?(?:<>|\w*:)')):
            if re.search(pat, bare, re.I):
                run.add('GRAPH_NOT_ALLOWED', ERROR, kw, TOOL_RULE_GRAPH, f'{kw} 절은 쓸 수 없다. 질의 도구는 기본 그래프만 조회한다.',
                        f'{kw} 절을 지우고 기본 그래프의 패턴으로 쓴다.')


@functools.lru_cache(maxsize=1)
def checker():
    """T-Box를 한 번만 읽는 공용 검증기."""
    return Checker()


def check(query):
    return checker().check(query)
