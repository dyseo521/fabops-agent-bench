"""원천 SQL DB -> RDF 지식 그래프 (pyoxigraph 디스크 저장소).

1. A-Box: ontology/fab_mapping.r2rml.ttl을 morph-kgc로 실행해 N-Triples로 materialize
2. T-Box: ontology/fab.ttl + ontology/dr_subset.ttl에 OWL RL(owlrl)을 적용한 폐포. T-Box에만 적용한다
3. A-Box 파생 관계: fab.ttl의 공리를 읽어 SPARQL UPDATE로 materialize
   역관계(owl:inverseOf) -> 대칭(owl:SymmetricProperty) -> 속성 사슬(owl:propertyChainAxiom)
   -> 전이 폐포(owl:TransitiveProperty, 단언된 트리플이 있는 속성만) -> fab 상위 클래스 타입
   DR 클래스 타입은 materialize 하지 않는다. T-Box의 rdfs:subClassOf로 도달한다
4. 전부 기본 그래프에 적재. 통계를 kg_stats.json으로 남긴다

예) python scripts/build_kg.py data/derived/db/lvhm_e_cr_s0.sqlite
    -> data/derived/kg/lvhm_e_cr_s0/ (store/, abox.nt, tbox_closure.ttl, kg_stats.json)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import owlrl
import pyoxigraph as ox
import rdflib
from rdflib.namespace import OWL, RDF, RDFS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONTO = os.path.join(ROOT, 'ontology')
FAB = 'https://w3id.org/ontosemc/fab#'
NT = 'application/n-triples'


def run_morph(db_path, out_nt, work):
    # 다중 프로세스는 같은 출력 파일에 동시에 써서 줄이 섞인다(Windows, morph-kgc 2.10). 단일 프로세스로 고정
    # na_values: SQL NULL이 pandas에서 None이 된다. 목록에 없으면 문자열 'None'으로 IRI·리터럴이 만들어진다
    cfg = os.path.join(work, 'morph_kgc.ini')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(f"""[CONFIGURATION]
output_file = {out_nt.replace(os.sep, '/')}
output_format = N-TRIPLES
na_values = ,None,NULL,nan,NaN,<NA>
number_of_processes = 1

[FabDB]
mappings = {os.path.join(ONTO, 'fab_mapping.r2rml.ttl').replace(os.sep, '/')}
db_url = sqlite:///{db_path.replace(os.sep, '/')}
""")
    r = subprocess.run([sys.executable, '-m', 'morph_kgc', cfg], capture_output=True, text=True, encoding='utf-8')
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        raise SystemExit('morph-kgc 실패')


def tbox():
    g = rdflib.Graph()
    g.parse(os.path.join(ONTO, 'fab.ttl'))
    g.parse(os.path.join(ONTO, 'dr_subset.ttl'))
    asserted = len(g)
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(g)
    # OWL RL 폐포에는 리터럴 주어 트리플(일반화 RDF)과 자기 자신과의 자명한 관계가 섞인다. 표준 RDF로 걸러낸다
    trivial = {OWL.sameAs, OWL.equivalentClass, OWL.equivalentProperty, RDFS.subClassOf, RDFS.subPropertyOf}
    out = rdflib.Graph()
    for pfx, ns in g.namespaces():
        out.bind(pfx, ns)
    for s, p, o in g:
        if isinstance(s, rdflib.Literal) or (s == o and p in trivial):
            continue
        out.add((s, p, o))
    return out, asserted


def rules(g):
    """fab 공리를 SPARQL UPDATE 목록으로 바꾼다. (이름, 질의) 순서가 곧 적용 순서다."""
    fab = lambda x: isinstance(x, rdflib.URIRef) and str(x).startswith(FAB)
    out = []
    for p, q in g.subject_objects(OWL.inverseOf):
        if fab(p) and fab(q):
            out.append((f'inverse {p.n3(g.namespace_manager)}',
                        f'INSERT {{ ?o <{q}> ?s }} WHERE {{ ?s <{p}> ?o }}'))
            out.append((f'inverse {q.n3(g.namespace_manager)}',
                        f'INSERT {{ ?o <{p}> ?s }} WHERE {{ ?s <{q}> ?o }}'))
    for p in g.subjects(RDF.type, OWL.SymmetricProperty):
        if fab(p):
            out.append((f'symmetric {p.n3(g.namespace_manager)}', f'INSERT {{ ?o <{p}> ?s }} WHERE {{ ?s <{p}> ?o }}'))
    for p, lst in g.subject_objects(OWL.propertyChainAxiom):
        if not fab(p):
            continue
        chain = list(rdflib.collection.Collection(g, lst))
        body = ' . '.join(f'?x{i} <{c}> ?x{i + 1}' for i, c in enumerate(chain))
        out.append((f'chain {p.n3(g.namespace_manager)}', f'INSERT {{ ?x0 <{p}> ?x{len(chain)} }} WHERE {{ {body} }}'))
    for p in g.subjects(RDF.type, OWL.TransitiveProperty):
        if fab(p):
            out.append((f'transitive {p.n3(g.namespace_manager)}',
                        f'INSERT {{ ?a <{p}> ?c }} WHERE {{ ?a <{p}> ?b . ?b <{p}>+ ?c }}'))
    for c, d in g.subject_objects(RDFS.subClassOf):
        if fab(c) and fab(d) and c != d:
            out.append((f'subclass {c.n3(g.namespace_manager)} -> {d.n3(g.namespace_manager)}',
                        f'INSERT {{ ?x a <{d}> }} WHERE {{ ?x a <{c}> }}'))
    return out


def count(store, q):
    return int(next(iter(store.query(q)))[0].value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--out', help='기본값 data/derived/kg/<DB 이름>')
    a = ap.parse_args()
    db_path = os.path.abspath(a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db))
    name = os.path.splitext(os.path.basename(db_path))[0]
    out = a.out or os.path.join(ROOT, 'data', 'derived', 'kg', name)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    stats, t0 = {}, time.time()

    abox_nt = os.path.join(out, 'abox.nt')
    run_morph(db_path, abox_nt, out)
    stats['morph_kgc_s'] = round(time.time() - t0, 1)

    tg, asserted = tbox()
    tg.serialize(os.path.join(out, 'tbox_closure.ttl'), format='turtle')
    stats['tbox_asserted'], stats['tbox_closure'] = asserted, len(tg)

    store = ox.Store(os.path.join(out, 'store'))
    store.bulk_load(abox_nt, NT)
    stats['abox_mapped'] = len(store)
    store.bulk_load(os.path.join(out, 'tbox_closure.ttl'), 'text/turtle')
    stats['after_tbox'] = len(store)

    applied = []
    for label, q in rules(tg):
        before = len(store)
        store.update(q)
        applied.append(dict(rule=label, added=len(store) - before))
    stats['materialized'] = applied
    stats['total'] = len(store)
    stats['elapsed_s'] = round(time.time() - t0, 1)

    classes = ['Lot', 'ProductiveLot', 'EngineeringLot', 'StepExecution', 'Tool', 'ToolGroup', 'DelayStation', 'Bay',
               'Route', 'ProcessStep', 'Operation', 'QTimeConstraint', 'DownEvent', 'EquipmentStateInterval',
               'Wafer', 'LtlDedication', 'SamplingSkip', 'WipSnapshotEntry', 'QTimeSnapshotEntry', 'PMPlan']
    stats['instances'] = {c: count(store, f'SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?x a <{FAB}{c}> }}')
                          for c in classes}
    store.flush()
    with open(os.path.join(out, 'kg_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    print(f"{os.path.relpath(out, ROOT)}  트리플 {stats['total']:,} (매핑 {stats['abox_mapped']:,}, "
          f"T-Box 폐포 {stats['tbox_closure']:,}), {stats['elapsed_s']}초")
    for r in applied:
        if r['added']:
            print(f"  + {r['added']:>9,d}  {r['rule']}")
    for c, n in stats['instances'].items():
        print(f'  {c:24s} {n:>9,d}')


if __name__ == '__main__':
    main()
