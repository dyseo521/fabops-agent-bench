"""자료 구조의 설명. SQL 조건과 KG 조건이 같은 내용의 설명을 받게 한다.

설명의 원천은 동결한 T-Box(ontology/fab.ttl) 하나다. 손으로 쓴 설명문은 없다.
- KG 조건: 클래스·속성의 정의, 정의역·치역, 어휘 개체를 그대로 본다
- SQL 조건: R2RML 매핑(ontology/fab_mapping.r2rml.ttl)을 따라 같은 정의를 테이블·컬럼에 붙인다.
  객체 속성의 치역은 참조 테이블로, 어휘 개체는 컬럼 값 목록으로 옮긴다
- 조건 B의 뷰: sql/condition_b_views.sql 머리 주석의 대응표(뷰 -> fab 용어)로 같은 정의를 붙인다
"""
import functools
import os
import re

import rdflib
from rdflib import OWL, RDF, RDFS
from rdflib.collection import Collection

from .. import config
from .sparql_common import DATA, FAB

RR = rdflib.Namespace('http://www.w3.org/ns/r2rml#')
SKOS_DEF = rdflib.URIRef('http://www.w3.org/2004/02/skos/core#definition')
ONT = os.path.join(config.ROOT, 'ontology')
F = rdflib.Namespace(FAB)


# ---------------------------------------------------------------- T-Box

@functools.lru_cache(maxsize=1)
def tbox():
    g = rdflib.Graph()
    g.parse(os.path.join(ONT, 'fab.ttl'), format='turtle')
    g.parse(os.path.join(ONT, 'dr_subset.ttl'), format='turtle')
    return g


def is_fab(x):
    return isinstance(x, rdflib.URIRef) and str(x).startswith(FAB)


def local(iri):
    s = str(iri)
    if s.startswith(FAB):
        return 'fab:' + s[len(FAB):]
    if s.startswith(str(rdflib.XSD)):
        return 'xsd:' + s[len(str(rdflib.XSD)):]
    return s


DESCRIPTIONS = True         # 제거 실험(no_descriptions)에서 False. 설명문의 원천이 이 함수 하나라 SQL 쪽과 KG 쪽이 함께 빠진다


def set_descriptions(flag):
    global DESCRIPTIONS
    DESCRIPTIONS = bool(flag)
    _sql_docs.cache_clear()


def definition(iri):
    if not DESCRIPTIONS:
        return ''
    g = tbox()
    for p in (SKOS_DEF, RDFS.comment):
        v = g.value(iri, p)
        if v:
            return str(v)
    return ''


@functools.lru_cache(maxsize=1)
def classes():
    g = tbox()
    return tuple(sorted({c for c in g.subjects(RDF.type, OWL.Class) if is_fab(c)}, key=str))


@functools.lru_cache(maxsize=1)
def properties():
    g = tbox()
    out = {}
    for kind, t in (('object', OWL.ObjectProperty), ('datatype', OWL.DatatypeProperty)):
        for p in g.subjects(RDF.type, t):
            if is_fab(p):
                out[p] = kind
    return out


def supers(c):
    """fab 상위 클래스(자신 제외, 전이)."""
    return {x for x in tbox().transitive_objects(c, RDFS.subClassOf) if is_fab(x) and x != c}


def subs(c):
    return {x for x in tbox().transitive_subjects(RDFS.subClassOf, c) if is_fab(x) and x != c}


def individuals(c):
    """T-Box에 선언된 어휘 개체(예: E10 상태, 불량 패턴). [(개체, label)]"""
    g = tbox()
    return sorted(((i, str(g.value(i, RDFS.label) or '')) for i in g.subjects(RDF.type, c) if is_fab(i)), key=lambda x: str(x[0]))


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def values_text(c):
    """어휘 개체를 값 목록으로. label이 이름과 같으면 label만 쓴다."""
    out = []
    for i, label in individuals(c):
        name = str(i)[len(FAB):]
        out.append(label if label and _norm(label) == _norm(name) else (f'{name} ({label})' if label else name))
    return ', '.join(out)


@functools.lru_cache(maxsize=1)
def hidden_terms():
    """자료에 개체가 없는 어휘. gate: 조치·시나리오(게이트 내부 어휘), violation: 제약 위반(검증 조건에만 보인다)."""
    g = tbox()

    def family(root):
        cs = {root} | subs(root)
        return cs | {p for p in properties() if any(d in cs for d in g.objects(p, RDFS.domain))}

    gate = family(F.Action) | family(F.Scenario) | family(F.Incident)
    return dict(gate=frozenset(gate), violation=frozenset(family(F.ConstraintViolation)))


def visible(term, verify):
    h = hidden_terms()
    return term not in h['gate'] and (verify or term not in h['violation'])


def property_info(p):
    g = tbox()
    d = dict(name=local(p), kind=properties().get(p, '') + ' property',
             domain=[local(x) for x in g.objects(p, RDFS.domain)], range=[local(x) for x in g.objects(p, RDFS.range)],
             description=definition(p))
    flags = [n for n, t in (('functional', OWL.FunctionalProperty), ('transitive', OWL.TransitiveProperty),
                            ('symmetric', OWL.SymmetricProperty)) if (p, RDF.type, t) in g]
    if flags:
        d['characteristics'] = flags
    inv = sorted({local(x) for x in list(g.objects(p, OWL.inverseOf)) + list(g.subjects(OWL.inverseOf, p))})
    if inv:
        d['inverse_of'] = inv
    chain = g.value(p, OWL.propertyChainAxiom)
    if chain is not None:
        d['property_chain'] = [local(x) for x in Collection(g, chain)]
    for r in g.objects(p, RDFS.range):
        v = values_text(r) if is_fab(r) else ''
        if v:
            d['range_values'] = v
    return d


def class_properties(c, verify):
    """클래스의 속성 행. 나가는 속성(정의역이 이 클래스나 상위 클래스, 또는 매핑이 이 클래스의 개체에 붙이는 속성)과
    들어오는 속성(치역이 이 클래스나 상위 클래스). -> [(방향, 속성)]"""
    g = tbox()
    up = {c} | supers(c)
    down = {c} | subs(c)
    mapped = set()
    for kind in _kinds().values():
        if kind['classes'] & down:
            mapped |= kind['preds']
    out = []
    for p in sorted(properties(), key=str):
        if not visible(p, verify):
            continue
        dom, rng = set(g.objects(p, RDFS.domain)), set(g.objects(p, RDFS.range))
        if dom & up or (not dom and p in mapped):
            out.append(('out', p))
        if rng & up:
            out.append(('in', p))
    return out


# ---------------------------------------------------------------- R2RML

def _strip_parens(s):
    prev = None
    while prev != s:
        prev, s = s, re.sub(r'\([^()]*\)', ' ', s)
    return s


def _select_items(sql):
    """첫 SELECT의 선택 목록을 최상위 쉼표로 나눈다. -> [(식, 별칭)]"""
    m = re.search(r'(?i)\bSELECT\b(\s+DISTINCT\b)?', sql)
    i = start = m.end()
    depth, quote, items = 0, None, []
    while i < len(sql):
        ch = sql[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and ch == ',':
            items.append(sql[start:i])
            start = i + 1
        elif depth == 0 and re.match(r'(?i)FROM\b', sql[i:]) and not (sql[i - 1].isalnum() or sql[i - 1] == '_'):
            break
        i += 1
    items.append(sql[start:i])
    out = []
    for it in (x.strip() for x in items):
        if not it:
            continue
        m = re.match(r'(?is)(.*?)\s+AS\s+(\w+)$', it)
        out.append((m.group(1), m.group(2)) if m else (it, re.findall(r'\w+', it)[-1]))
    return out


def _idents(expr):
    """식에 나오는 (한정자, 이름). 문자열 상수는 뺀다."""
    return re.findall(r'(?:\b(\w+)\.)?\b([A-Za-z_]\w*)\b', re.sub(r"'[^']*'", ' ', expr))


def _kind_of(template):
    """IRI 틀 -> (개체 종류, 변수 목록). data/tool/{eqp_id} -> ('tool', ['eqp_id']). fab#{x} -> ('fab#', ['x'])"""
    t = str(template)
    if t.startswith(DATA):
        return t[len(DATA):].split('/')[0], re.findall(r'\{(\w+)\}', t)
    if t.startswith(FAB):
        return 'fab#', re.findall(r'\{(\w+)\}', t)
    return None, []


@functools.lru_cache(maxsize=1)
def _maps():
    """TriplesMap을 읽는다. base: 바깥 FROM의 첫 테이블, tables: {한정자: 테이블}, items: {별칭: 식}."""
    g = rdflib.Graph()
    g.parse(os.path.join(ONT, 'fab_mapping.r2rml.ttl'), format='turtle')
    cls_names = {str(c)[len(FAB):]: c for c in classes()}
    out = []
    for tm in sorted(g.subjects(RDF.type, RR.TriplesMap), key=str):
        lt = g.value(tm, RR.logicalTable)
        name, sql = g.value(lt, RR.tableName), g.value(lt, RR.sqlQuery)
        m = dict(name=str(tm).split('#')[-1], base=None, tables={}, items={}, distinct=False, classes=set(), poms=[])
        if name:
            m['base'] = str(name)
            m['tables'][None] = m['base']
        else:
            sql = str(sql)
            flat = _strip_parens(sql)
            if not re.match(r'(?i)\s*WITH\b', flat):
                refs = re.findall(r'(?i)\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?!JOIN\b|LEFT\b|USING\b|ON\b|WHERE\b|UNION\b)(\w+))?', flat)
                if not refs:        # 바깥 FROM이 없다(스칼라 부질의만). 부질의의 첫 테이블을 테이블 설명에만 쓴다
                    m['base'] = re.findall(r'(?i)\bFROM\s+(\w+)', sql)[0]
                    m['scalar'] = True
                if refs:
                    m['base'] = refs[0][0]
                    m['tables'][None] = m['base']
                    for t, a in refs:
                        m['tables'].setdefault(a or t, t)
                        m['tables'].setdefault(t, t)
                m['items'] = {a: e for e, a in _select_items(sql)}
                m['distinct'] = bool(re.match(r'(?i)\s*SELECT\s+DISTINCT\b', sql))
        sm = g.value(tm, RR.subjectMap)
        m['subject'] = _kind_of(g.value(sm, RR.template))
        m['classes'] |= {c for c in g.objects(sm, RR['class']) if is_fab(c)}
        for pom in g.objects(tm, RR.predicateObjectMap):
            pred, om = g.value(pom, RR.predicate), g.value(pom, RR.objectMap)
            col, tpl = g.value(om, RR.column), g.value(om, RR.template)
            if pred == RDF.type and tpl is not None:     # 클래스를 값으로 정하는 매핑. 질의문의 클래스 이름을 모은다
                m['classes'] |= {cls_names[n] for n in re.findall(r"'(\w+)'", str(sql)) if n in cls_names}
            elif is_fab(pred):
                kind, names = ('column', [str(col)]) if col is not None else _kind_of(tpl)
                m['poms'].append((pred, kind, names))
        m['poms'].sort(key=lambda x: (str(x[0]), x[2]))
        out.append(m)
    return out


def _source(m, name, columns):
    """매핑의 열 이름(별칭)이 가리키는 원천 (테이블, 컬럼). 찾지 못하면 None."""
    expr = m['items'].get(name, name)
    for q, ident in _idents(expr):
        t = m['tables'].get(q or None)
        if q and t and ident in columns.get(t, ()):
            return t, ident
        if not q:
            for t in dict.fromkeys(m['tables'].values()):
                if ident in columns.get(t, ()):
                    return t, ident
    return None


@functools.lru_cache(maxsize=1)
def _kinds():
    """개체 종류(IRI 틀의 첫 구간) -> dict(classes, preds, table, keys). table·keys는 그 개체의 원천 테이블과 식별 컬럼."""
    out = {}
    for m in _maps():
        kind, names = m['subject']
        k = out.setdefault(kind, dict(classes=set(), preds=set(), best=None))
        k['classes'] |= m['classes']
        k['preds'] |= {p for p, _, _ in m['poms']}
        if m['classes'] and m['base'] and (k['best'] is None or len(m['poms']) > len(k['best']['poms'])):
            k['best'] = m
    return out


def _common_class(cs):
    """클래스 집합의 가장 구체적인 공통 클래스."""
    cs = list(cs)
    if len(cs) == 1:
        return cs[0]
    common = set.intersection(*[{c} | supers(c) for c in cs])
    return max(common, key=lambda c: len(supers(c))) if common else None


@functools.lru_cache(maxsize=8)
def _sql_docs(schema_key):
    """schema_key: ((테이블, (컬럼, ...)), ...) -> dict(table={테이블: 설명}, column={(테이블, 컬럼): dict(description, references)},
    referenced_by={테이블: [테이블(컬럼)]})"""
    columns = {t: set(cols) for t, cols in schema_key}
    maps, kinds = _maps(), _kinds()
    g = tbox()
    # 테이블 설명: 그 테이블을 바탕으로 하는 매핑 가운데 속성이 가장 많은 것의 클래스
    table_doc = {}
    for t in columns:
        own = [m for m in maps if m['base'] == t]
        cand = sorted((m for m in own if m['classes']), key=lambda m: (-len(m['poms']), m['name']))
        if cand:
            c = _common_class(cand[0]['classes'])
            table_doc[t] = definition(c) if c is not None else ''
        elif own:       # 클래스 없는 매핑만 있다(관계 테이블). 속성의 정의를 쓴다
            table_doc[t] = ' '.join(dict.fromkeys(d for m in own for d in (definition(p) for p, _, _ in m['poms']) if d))
        else:           # 다른 매핑에 조인으로만 쓰인다
            joined = [m for m in maps if t in m['tables'].values() and m['classes']]
            c = _common_class(joined[0]['classes']) if len(joined) == 1 else None
            table_doc[t] = definition(c) if c is not None else ''
    # 컬럼 설명과 참조
    preds, refs = {}, {}
    for own_pass in (True, False):          # 바탕 테이블의 컬럼을 먼저 정한다
        for m in maps:
            if m.get('scalar'):
                continue
            for pred, kind, names in m['poms']:
                for n in names:
                    src = _source(m, n, columns)
                    if src is None or (src[0] == m['base']) != own_pass:
                        continue
                    if kind != 'column' and not own_pass:
                        continue                        # 조인한 테이블의 컬럼은 값으로 쓰인 경우만
                    slot = preds.setdefault(src, [])
                    if own_pass or not slot:
                        slot.append((0 if kind == 'column' else 1, str(pred), pred, kind))
                    if kind not in ('column', 'fab#') and own_pass:
                        best = kinds.get(kind, {}).get('best')
                        if best is not None:
                            keys = [(_source(best, k, columns) or (None, k))[1] for k in best['subject'][1]]
                            if not (best['base'] == src[0] and src[1] in keys):
                                refs.setdefault(src, set()).add(f"{best['base']}({', '.join(keys)})")
    column_doc = {}
    for src, lst in preds.items():
        parts = []
        for _, _, pred, kind in sorted(set(lst)):
            d = definition(pred)
            if kind == 'fab#':       # 값이 어휘 개체다. 치역 클래스의 정의와 값 목록을 붙인다
                for r in g.objects(pred, RDFS.range):
                    if is_fab(r) and individuals(r):
                        d = ' '.join(x for x in (d, definition(r), 'Values: ' + values_text(r) + '.') if x)
            if d and d not in parts:
                parts.append(d)
        column_doc[src] = dict(description=' '.join(parts), references=', '.join(sorted(refs.get(src, ()))))
    referenced_by = {}
    for (t, c), targets in sorted(refs.items()):
        for target in targets:
            referenced_by.setdefault(target.split('(')[0], {}).setdefault(t, []).append(c)
    return dict(table=table_doc, column=column_doc,
                referenced_by={k: [f"{t}({', '.join(cs)})" for t, cs in sorted(v.items())] for k, v in referenced_by.items()})


def _view_sources():
    """뷰 정의에서 (뷰 -> 바탕 테이블 또는 뷰), 머리 주석의 대응표에서 (뷰 -> fab 용어)를 읽는다."""
    text = open(os.path.join(config.ROOT, 'sql', 'condition_b_views.sql'), encoding='utf-8').read()
    term = {v: F[t] for v, t in re.findall(r'(?m)^--\s+(v_\w+)\s+fab:(\w+)', text)}
    base = {v: b for v, b in re.findall(r'(?is)CREATE\s+VIEW\s+(\w+)\s+AS\b.*?\bFROM\s+(\w+)', text)}
    return term, base


def sql_docs(schema):
    """schema: {이름: dict(kind, columns=[(컬럼, 형)])} -> 테이블·뷰·컬럼 설명. SqlAccess.schema()의 결과를 넣는다."""
    tables = {n: s for n, s in schema.items() if s['kind'] == 'table'}
    d = _sql_docs(tuple(sorted((n, tuple(c for c, _ in s['columns'])) for n, s in tables.items())))
    out = dict(table=dict(d['table']), column=dict(d['column']), referenced_by=d['referenced_by'])
    term, base = _view_sources()
    g = tbox()
    for v, s in schema.items():
        if s['kind'] != 'view':
            continue
        t = term.get(v)
        if t is None:
            out['table'][v] = ''
        elif t in properties():       # 관계를 미리 계산한 뷰: 속성의 이름, 정의역 -> 치역, 사슬, 정의
            info = property_info(t)
            text = f"{info['name']}: {', '.join(info['domain'])} -> {', '.join(info['range'])}."
            if info.get('property_chain'):
                text += ' Chain: ' + ' / '.join(info['property_chain']) + '.'
            if 'transitive' in info.get('characteristics', ()):
                text += ' Transitive closure.'
            out['table'][v] = ' '.join(x for x in (text, info['description']) if x)
        else:
            out['table'][v] = definition(t)
        b = base.get(v)
        while b in base:            # 뷰 위의 뷰는 바탕 테이블까지 따라간다
            b = base[b]
        for c, _ in s['columns']:
            if (b, c) in out['column']:
                out['column'][(v, c)] = out['column'][(b, c)]
    return out
