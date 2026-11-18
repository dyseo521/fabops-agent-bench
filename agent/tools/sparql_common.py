"""SPARQL 도구와 질의 검증이 함께 쓰는 접두사. 모델이 PREFIX를 적지 않아도 아래 접두사는 쓸 수 있다."""
import re

FAB = 'https://w3id.org/ontosemc/fab#'
DATA = 'https://w3id.org/ontosemc/data/'
VIOLATION_GRAPH = 'https://w3id.org/ontosemc/graph/violations'
PREFIXES = {
    'fab': FAB,
    'data': DATA,
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
    'xsd': 'http://www.w3.org/2001/XMLSchema#',
    'owl': 'http://www.w3.org/2002/07/owl#',
}


def with_prefixes(query):
    """질의에 선언되지 않은 표준 접두사를 앞에 붙인다."""
    declared = {m.group(1) for m in re.finditer(r'(?im)^\s*PREFIX\s+([A-Za-z][\w-]*)\s*:', query)}
    head = ''.join(f'PREFIX {p}: <{iri}>\n' for p, iri in PREFIXES.items() if p not in declared)
    return head + query
