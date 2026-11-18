"""쌍 비교(같은 문항을 두 조건이 푼다)의 검정력 모의 계산. 연구노트 D-46의 재현 스크립트.

조건 X, Y의 문항별 성공 여부가 쌍으로 관측된다. 결과가 갈리는 문항 비율(불일치율) d = P(X만 성공) + P(Y만 성공),
차이 delta = P(Y만 성공) - P(X만 성공).
  1) 우월성: 정확 McNemar 검정(양측 5%)의 검정력과, 검정력 80%에 필요한 최소 차이(MDE)
  2) 비열등성: 참 차이가 0일 때 "차이의 단측 95% 하한 > -마진"이 성립할 확률
패러프레이즈는 독립 표본으로 세지 않는다(문항 수 = n).
결과는 data/derived/profile/power_sim.json.

예) python scripts/power_sim.py
"""
import json
import os

import numpy as np
from scipy.stats import binom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = (30, 55, 65, 120, 150)          # 조회 30, 진단 55, 조치 65, 진단 + 조치 120, 전체 150
DS = (0.2, 0.3, 0.4, 0.5)            # 불일치율
REPS = 20000
rng = np.random.default_rng(0)


def draw(n, d, delta):
    b_p, c_p = (d - delta) / 2, (d + delta) / 2       # X만 성공, Y만 성공
    k = rng.multinomial(n, [b_p, c_p, 1 - d], size=REPS)
    return k[:, 0], k[:, 1]


def power_mcnemar(n, d, delta):
    b, c = draw(n, d, delta)
    m = b + c
    p = np.minimum(1.0, 2 * binom.cdf(np.minimum(b, c), m, 0.5))
    p[m == 0] = 1.0
    return float((p < 0.05).mean())


def mde(n, d):
    for delta in np.arange(0.02, d + 1e-9, 0.01):
        if power_mcnemar(n, d, float(delta)) >= 0.8:
            return round(float(delta), 2)
    return None                                         # 불일치율 안에서는 80%에 못 미침


def power_noninferiority(n, d, margin):
    b, c = draw(n, d, 0.0)
    diff = (c - b) / n
    se = np.sqrt(np.maximum(1e-12, (b + c) / n - diff ** 2) / n)
    return float((diff - 1.645 * se > -margin).mean())


def main():
    out = dict(reps=REPS, alpha=0.05, mde_80=[], power=[], noninferiority=[])
    for n in NS:
        for d in DS:
            out['mde_80'].append(dict(n=n, discordant=d, mde=mde(n, d)))
            for delta in (0.10, 0.15, 0.20, 0.30):
                if delta <= d:
                    out['power'].append(dict(n=n, discordant=d, delta=delta, power=round(power_mcnemar(n, d, delta), 3)))
            for margin in (0.05, 0.10, 0.15, 0.20):
                out['noninferiority'].append(dict(n=n, discordant=d, margin=margin, power=round(power_noninferiority(n, d, margin), 3)))
    os.makedirs(os.path.join(ROOT, 'data', 'derived', 'profile'), exist_ok=True)
    with open(os.path.join(ROOT, 'data', 'derived', 'profile', 'power_sim.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    print('MDE (power 80%, two-sided 5%, exact McNemar). rows n, cols discordant rate', DS)
    for n in NS:
        print(f'  n={n:3d}', [next(r['mde'] for r in out['mde_80'] if r['n'] == n and r['discordant'] == d) for d in DS])
    print('Non-inferiority power at true difference 0. rows n x discordant, cols margin 0.05 0.10 0.15 0.20')
    for n in NS:
        for d in (0.2, 0.4):
            print(f'  n={n:3d} d={d}', [next(r['power'] for r in out['noninferiority'] if r['n'] == n and r['discordant'] == d and r['margin'] == m)
                                         for m in (0.05, 0.10, 0.15, 0.20)])


if __name__ == '__main__':
    main()
