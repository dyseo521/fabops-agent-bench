"""제공사 점검. 모델을 한두 번 호출해 도구 호출 동작과 사용량 필드를 확인한다(유료 호출. 건당 1센트 미만).

확인 항목
1. 도구 정의(전 조건의 실제 도구)를 받아들이는가. strict 스키마 오류가 없는가
2. 도구 호출을 한 응답에 하나씩 하는가. 인자가 스키마에 맞는 JSON인가
3. 사용량 필드(입력, 출력, 캐시 읽기·쓰기)와 비용 필드, 응답의 모델 ID
4. 같은 요청을 다시 보냈을 때 캐시 읽기 토큰이 0보다 큰가(Anthropic)
결과는 runs/_probe/<모델>.json에 남긴다. 키 값은 출력하거나 기록하지 않는다.

예) .venv-agent\\Scripts\\python scripts\\agent_probe_provider.py --model haiku-5.5
    .venv-agent\\Scripts\\python scripts\\agent_probe_provider.py --model gemma-4
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import jsonschema  # noqa: E402

from agent import budget, config, prompt  # noqa: E402
from agent.session import specs  # noqa: E402
from agent_run import make_provider  # noqa: E402

QUESTION = ('점검용 질문이다. 자료 구조 조회 도구를 한 번 호출해 목록을 본 다음, submit_answer로 '
            'answer에 {"value": 목록의 행 수}를 적어 제출한다.\n\n응답 양식: answer.value: 행 수(정수)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', required=True, choices=list(config.MODELS))
    ap.add_argument('--condition', default='A', choices=list(config.CONDITIONS))
    a = ap.parse_args()
    config.load_env()
    provider = make_provider(a.model)
    tools = specs(a.condition)
    schema = {s.name: s.input_schema for s in tools}
    system, _ = prompt.system_prompt()
    out = dict(model_key=a.model, provider=provider.name, settings=provider.settings, condition=a.condition, turns=[])
    cost = 0.0
    for attempt in range(2):        # 같은 접두부로 두 번: 둘째 요청의 캐시 읽기를 본다
        provider.start(system, [dict(name=s.name, description=s.description, input_schema=s.input_schema) for s in tools])
        provider.add_user(QUESTION)
        turn = provider.complete()
        valid = []
        for c in turn.tool_calls:
            try:
                jsonschema.validate(c.input, schema[c.name])
                valid.append(True)
            except Exception as e:
                valid.append(f'{type(e).__name__}: {str(e)[:200]}')
        c1 = budget.cost_usd(turn.model or provider.model, turn.usage)
        cost += c1 or 0.0
        out['turns'].append(dict(attempt=attempt, stop=turn.stop, model=turn.model, request_id=turn.request_id, usage=turn.usage, cost_usd=c1,
                                 latency_s=round(turn.latency_s, 2), text=turn.text[:300], detail=turn.detail,
                                 tool_calls=[dict(name=c.name, input=c.input, raw=c.raw) for c in turn.tool_calls], args_valid=valid))
    t0, t1 = out['turns']
    out['checks'] = dict(
        tools_accepted=True, one_call_per_turn=all(len(t['tool_calls']) <= 1 for t in out['turns']),
        called_a_tool=all(len(t['tool_calls']) == 1 for t in out['turns']), args_valid=all(v is True for t in out['turns'] for v in t['args_valid']),
        cache_read_on_second=t1['usage'].get('cache_read', 0) > 0, cost_known=all(t['cost_usd'] is not None for t in out['turns']),
        model_id=t0['model'])
    out['cost_usd'] = round(cost, 6)
    d = os.path.join(config.RUNS, '_probe')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, a.model.replace('/', '_') + '.json')
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(out['checks'], ensure_ascii=False), f"cost ${out['cost_usd']}", path)


if __name__ == '__main__':
    main()
