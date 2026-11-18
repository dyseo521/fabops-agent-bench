"""조건 B용 DB를 만든다: 원천 DB 사본 + sql/condition_b_views.sql. 원천 DB(조건 A)는 건드리지 않는다.

뷰 행 수가 KG의 대응 materialize와 같은지 확인한다. 같아야 B 대 C의 차이를 온톨로지 표현의 효과로 볼 수 있다.

예) python scripts/build_condition_b.py data/derived/db/lvhm_e_cr_s0.sqlite
    -> data/derived/db/lvhm_e_cr_s0_B.sqlite
"""
import argparse
import os
import shutil
import sqlite3
import sys

import pyoxigraph as ox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = [  # (뷰, KG 질의)
    ('v_step_execution', 'SELECT (COUNT(?x) AS ?n) WHERE { ?x a fab:StepExecution }'),
    ('v_lot_tool', 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughTool ?t }'),
    ('v_lot_toolgroup', 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughToolGroup ?t }'),
    ('v_lot_bay', 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughBay ?t }'),
    ('v_lot_operation', 'SELECT (COUNT(*) AS ?n) WHERE { ?l fab:passedThroughOperation ?t }'),
    ('v_ltl_dedication_closure', 'SELECT (COUNT(*) AS ?n) WHERE { ?a fab:ltlDedicatedTo ?b }'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    a = ap.parse_args()
    src = a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db)
    name = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(os.path.dirname(src), name + '_B.sqlite')
    shutil.copyfile(src, dst)
    db = sqlite3.connect(dst)
    db.executescript(open(os.path.join(ROOT, 'sql', 'condition_b_views.sql'), encoding='utf-8').read())
    db.commit()
    kg = os.path.join(ROOT, 'data', 'derived', 'kg', name, 'store')
    store = ox.Store.read_only(kg) if os.path.exists(kg) else None
    fail = False
    print(os.path.relpath(dst, ROOT))
    for view, q in PAIRS:
        n = db.execute(f'SELECT COUNT(*) FROM {view}').fetchone()[0]
        line = f'  {view:26s} {n:>9,d}'
        if store is not None:
            k = int(next(iter(store.query('PREFIX fab: <https://w3id.org/ontosemc/fab#> ' + q)))[0].value)
            line += f'   KG {k:>9,d}  {"OK" if k == n else "DIFF"}'
            fail |= k != n
        print(line)
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
