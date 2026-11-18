"""오라클 설계 값의 민감도. 설계 값을 하나씩 바꿨을 때 기대 조치가 기준과 같은 시나리오의 비율을 템플릿별로 낸다.

기준 오라클 출력(scenarios/<이름>_oracle.json)은 건드리지 않는다. 결과는 data/derived/profile/<이름>_oracle_sensitivity.json.
연구노트 U-2(LTL 적용 대상), U-3(설계 값)의 근거.

예) python scripts/oracle_sensitivity.py data/derived/db/lvhm_e_cr_s1.sqlite
    python scripts/oracle_sensitivity.py data/derived/db/lvhm_e_cr_s1.sqlite --time-floor
        질문은 시각을 분 단위로 적는다. 기준 시각을 그 분의 시작(초 버림)으로 바꿔도 기대 조치가 같은 시나리오를
        time_floor_stable_ids로 기존 결과 파일에 더한다(D-68). PM 연기 시간 값은 비교하지 않는다(S2가 +1시간을 허용)
"""
import argparse
import json
import os
from collections import Counter, defaultdict

import oracle
from asof_state import FabState

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID = dict(
    long_down_h=[2.0, 8.0, 12.0, 24.0],
    wipmax_safety=[0.4, 0.6, 0.7, 0.9, 1.0, 1.6],
    reprio_slack_h=[1.0, 4.0],
    pm_window_h=[12.0, 48.0],
    ltl_scope=['reach', 'all'],
)
# 강건 시나리오(D-49)의 기준: 수치 설계 값을 0.5배, 2배로 바꿔도 기대 조치가 같다. PM 연기 시간은 범위로 채점하므로 값은 비교하지 않는다
STABLE = ('long_down_h=2.0', 'long_down_h=8.0', 'wipmax_safety=0.4', 'wipmax_safety=1.6', 'reprio_slack_h=1.0', 'reprio_slack_h=4.0',
          'pm_window_h=12.0', 'pm_window_h=48.0')


def canon(r, drop=()):
    """기대 조치 대안 집합의 정규형. 조치 순서와 근거 문구는 비교하지 않는다. drop의 필드는 뺀다."""
    return json.dumps([sorted(json.dumps({k: v for k, v in a.items() if k not in drop}, sort_keys=True, ensure_ascii=False)
                              for a in alt) for alt in r['alternatives']], ensure_ascii=False)


def memoize(fs):
    """한 시나리오 안에서 같은 시각의 상태 계산을 다시 하지 않게 한다. decide는 이 값들을 수정하지 않는다."""
    cache = {}

    def wrap(fn, n):
        def cached(t):
            k = (n, t)
            if k not in cache:
                cache[k] = fn(t)
            return cache[k]
        return cached

    for name in ('tools', 'wip', 'dedications', 'qtime_open'):
        setattr(fs, name, wrap(getattr(fs, name), name))
    return cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--time-floor', action='store_true')
    a = ap.parse_args()
    db_path = a.db if os.path.isabs(a.db) else os.path.join(ROOT, a.db)
    name = os.path.splitext(os.path.basename(db_path))[0]
    scs = json.load(open(os.path.join(ROOT, 'data', 'derived', 'scenarios', name + '.json'), encoding='utf-8'))['scenarios']
    fs = FabState(db_path)
    if a.time_floor:
        path = os.path.join(ROOT, 'data', 'derived', 'profile', name + '_oracle_sensitivity.json')
        out = json.load(open(path, encoding='utf-8'))
        fl = lambda t: t[:16] + ':00.000'
        ids, same_t, n_t = [], Counter(), Counter()
        for i, sc in enumerate(scs[:a.limit]):
            b = oracle.decide(fs, sc)
            f = oracle.decide(fs, dict(sc, time=fl(sc['time']), down_start=fl(sc['down_start'])))
            ok = b['decision'] == f['decision'] and canon(b, drop=('defer_hours',)) == canon(f, drop=('defer_hours',))
            n_t[sc['template']] += 1
            same_t[sc['template']] += ok
            if ok:
                ids.append(sc['id'])
            if (i + 1) % 200 == 0:
                print(f'  {i + 1}/{len(scs)}', flush=True)
        out.update(time_floor_stable_ids=ids, time_floor_same_by_template={t: [same_t[t], n_t[t]] for t in sorted(n_t)})
        with open(path, 'w', encoding='utf-8') as fo:
            json.dump(out, fo, ensure_ascii=False, indent=1)
        print(os.path.relpath(path, ROOT), 'time_floor', out['time_floor_same_by_template'], len(ids), '/', len(scs[:a.limit]))
        return
    cache = memoize(fs)
    base = dict(oracle.PARAMS)
    variants = [('base', {})] + [(f'{k}={v}', {k: v}) for k, vs in GRID.items() for v in vs]
    act = defaultdict(Counter)        # variant -> template -> ACT 수
    same = defaultdict(Counter)       # variant -> template -> 기준과 같은 수
    n_tpl, stable_num, stable_ids = Counter(), Counter(), []
    ltl_n = defaultdict(list)         # ltl_scope -> LTL 템플릿의 대상 lot 수
    for i, sc in enumerate(scs[:a.limit]):
        cache.clear()
        tpl = sc['template']
        n_tpl[tpl] += 1
        res = {}
        for vname, over in variants:
            oracle.PARAMS.clear()
            oracle.PARAMS.update(base, **over)
            r = oracle.decide(fs, sc)
            res[vname] = (r['decision'], canon(r), canon(r, drop=('defer_hours',)))
            act[vname][tpl] += r['decision'] == 'ACT'
            if tpl == 'LTL' and (vname == 'base' or vname.startswith('ltl_scope')):
                ltl_n[vname].append(sum(len(x.get('lots', [])) for x in r['alternatives'][0] if x.get('request') == 'DEDICATION_WAIVER'))
        for vname, _ in variants:
            same[vname][tpl] += res[vname][1] == res['base'][1]
        ok = all(res[v][2] == res['base'][2] for v in STABLE)
        stable_num[tpl] += ok
        if ok:
            stable_ids.append(sc['id'])
        if (i + 1) % 200 == 0:
            print(f'  {i + 1}/{len(scs)}', flush=True)
    oracle.PARAMS.clear()
    oracle.PARAMS.update(base)
    tpls = sorted(n_tpl)
    out = dict(name=name, base_params=base, grid=GRID, n_by_template=dict(n_tpl),
               act_by_variant={v: dict(act[v]) for v, _ in variants},
               same_as_base_by_variant={v: dict(same[v]) for v, _ in variants},
               stable_under_half_and_double=dict(stable_num), stable_ids=stable_ids,
               ltl_target_lots={v: dict(n=len(x), zero=sum(1 for k in x if k == 0), median=sorted(x)[len(x) // 2] if x else 0,
                                        max=max(x) if x else 0) for v, x in ltl_n.items()})
    os.makedirs(os.path.join(ROOT, 'data', 'derived', 'profile'), exist_ok=True)
    path = os.path.join(ROOT, 'data', 'derived', 'profile', name + '_oracle_sensitivity.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(os.path.relpath(path, ROOT))
    print('template'.ljust(22) + ''.join(t.rjust(12) for t in tpls))
    print('n'.ljust(22) + ''.join(str(n_tpl[t]).rjust(12) for t in tpls))
    for v, _ in variants:
        print(v.ljust(22) + ''.join(f'{act[v][t]}/{same[v][t]}'.rjust(12) for t in tpls))
    print('(칸: ACT 수 / 기준과 기대 조치가 같은 시나리오 수)')
    print('stable(numeric)'.ljust(22) + ''.join(str(stable_num[t]).rjust(12) for t in tpls))


if __name__ == '__main__':
    main()
