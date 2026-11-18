"""비용 계산과 예산 상한. 비용은 기록한 토큰 수와 가격표(pricing.yaml)로 계산한다. 토큰 원값을 남기므로 가격이 바뀌면 다시 계산할 수 있다."""
import datetime
import json
import os

import yaml

from . import config

_PRICES = None


def prices():
    global _PRICES
    if _PRICES is None:
        _PRICES = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), 'pricing.yaml'), encoding='utf-8'))
    return _PRICES


def cost_usd(model, usage):
    """usage: {input, output, cache_read, cache_write_5m, cache_write_1h, cost_usd(제공사가 준 값)} -> USD 또는 None"""
    if usage.get('cost_usd') is not None:
        return float(usage['cost_usd'])
    p = prices()['models'].get(model)
    if not p:
        return None
    prompt = usage.get('input', 0) + usage.get('cache_read', 0) + usage.get('cache_write_5m', 0) + usage.get('cache_write_1h', 0)
    if 'over_100k' in p and prompt > 100_000:
        p = p['over_100k']
    return sum(usage.get(k, 0) * p[k] for k in ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h')) / 1e6


class Ledger:
    """비용 원장. 응답마다 한 줄을 덧붙인다. 월 누적이 상한에 닿기 전에 실행을 멈추게 한다."""

    def __init__(self, path, cap_usd=config.BUDGET_CAP_USD, provider='anthropic'):
        self.path, self.cap, self.provider = path, cap_usd, provider
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def add(self, trial_id, model, usage, cost):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(dict(ts=datetime.datetime.now().isoformat(timespec='seconds'), provider=self.provider,
                                    trial=trial_id, model=model, usage=usage, cost_usd=cost), ensure_ascii=False) + '\n')

    def spent(self, month=None, extra_paths=()):
        """이번 달 누적 비용. extra_paths: 다른 실행의 원장(같은 달, 같은 제공사 예산을 함께 센다)."""
        month = month or datetime.datetime.now().strftime('%Y-%m')
        total = 0.0
        for p in (self.path, *extra_paths):
            if not os.path.exists(p):
                continue
            for line in open(p, encoding='utf-8'):
                r = json.loads(line)
                if r['ts'].startswith(month) and r.get('provider') == self.provider and r.get('cost_usd'):
                    total += r['cost_usd']
        return total

    def allows(self, reserve_usd, extra_paths=()):
        """시행 1건의 최대 비용(reserve)을 더해도 상한 안인가."""
        return self.spent(extra_paths=extra_paths) + reserve_usd <= self.cap
