"""제공사 연결부. 네트워크 호출 없이 가짜 클라이언트로 요청 형식과 응답 해석을 점검한다."""
from types import SimpleNamespace as NS

import anthropic
import pytest

from agent.loop import run_trial
from agent.providers.anthropic_api import AnthropicProvider
from agent.providers.base import ProviderError
from agent.tools.registry import Context
from agent.trace import Tracer
from tests.agent.test_core import toy_registry


def block(type, **kw):
    return NS(type=type, **kw)


def message(content, stop='tool_use', **usage):
    u = dict(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None)
    u.update(usage)
    return NS(content=content, stop_reason=stop, stop_details=None, usage=NS(**u), model='claude-haiku-5-5', _request_id='req_1')


class FakeClient:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.requests.append(dict(kw, messages=list(kw['messages'])))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_anthropic_request_shape_and_loop():
    thinking = block('thinking', thinking='', signature='sig')
    client = FakeClient([
        message([thinking, block('text', text='조회한다'), block('tool_use', id='tu_1', name='echo', input=dict(text='a'))],
                cache_creation_input_tokens=9000, cache_creation=NS(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=9000)),
        message([block('tool_use', id='tu_2', name='submit_answer',
                       input=dict(abstain=False, reason=None, answer='{"value": 1}', evidence='{}'))], cache_read_input_tokens=9000),
    ])
    p = AnthropicProvider('claude-haiku-5-5', effort='medium', client=client)
    tr = Tracer()
    out = run_trial(p, toy_registry(), Context(task=None, condition='A'), 'SYSTEM', '질문', tracer=tr)
    assert out['stop'] == 'answered' and out['response']['answer'] == {'value': 1} and out['tool_calls'] == 1
    assert out['usage'] == dict(input=200, output=40, cache_read=9000, cache_write_5m=0, cache_write_1h=9000)
    # Haiku 5.5: 입력 0.10, 출력 0.50, 1시간 쓰기 0.20, 읽기 0.01 ($/MTok)
    assert abs(out['cost_usd'] - (200 * 0.10 + 40 * 0.50 + 9000 * 0.20 + 9000 * 0.01) / 1e6) < 1e-9
    a, b = client.requests
    assert set(a) == {'model', 'max_tokens', 'system', 'tools', 'tool_choice', 'output_config', 'cache_control', 'messages'}
    assert 'temperature' not in a and 'thinking' not in a and 'fallbacks' not in a
    assert a['system'] == [dict(type='text', text='SYSTEM', cache_control=dict(type='ephemeral', ttl='1h'))]
    assert a['tool_choice'] == dict(type='auto', disable_parallel_tool_use=True) and a['output_config'] == dict(effort='medium')
    assert [t['name'] for t in a['tools']] == ['echo', 'submit_answer'] and all(t['strict'] for t in a['tools'])
    assert a['messages'] == [dict(role='user', content=[dict(type='text', text='질문')])]
    # 둘째 요청: 첫 응답의 content(사고 블록 포함)를 그대로 덧붙이고 도구 결과를 user 메시지 하나로 보낸다
    assert a['system'] == b['system'] and a['tools'] == b['tools'] and b['messages'][:1] == a['messages']
    assert b['messages'][1]['role'] == 'assistant' and b['messages'][1]['content'][0] is thinking
    res = b['messages'][2]
    assert res['role'] == 'user' and res['content'][0]['type'] == 'tool_result' and res['content'][0]['tool_use_id'] == 'tu_1'
    assert res['content'][0]['is_error'] is False and len(b['messages']) == 3
    ev = tr.of('llm_response')
    assert ev[0]['request_id'] == 'req_1' and ev[0]['model'] == 'claude-haiku-5-5' and ev[0]['text'] == '조회한다'


def test_anthropic_stop_reasons_and_errors():
    refusal = message([], stop='refusal')
    refusal.stop_details = NS(category='cyber', explanation='x')
    p = AnthropicProvider('claude-haiku-5-5', client=FakeClient([refusal]))
    p.start('S', [])
    p.add_user('q')
    t = p.complete()
    assert t.stop == 'refusal' and t.detail['refusal']['category'] == 'cyber'
    resp = NS(status_code=400, headers={}, request=NS())
    too_long = anthropic.BadRequestError('prompt is too long: 250000 tokens', response=resp, body=None)
    other = anthropic.BadRequestError('tools.0.custom.input_schema: bad', response=resp, body=None)
    p = AnthropicProvider('claude-haiku-5-5', client=FakeClient([too_long, other]))
    p.start('S', [])
    p.add_user('q')
    assert p.complete().stop == 'context_limit'
    with pytest.raises(ProviderError):
        p.complete()
    out = run_trial(AnthropicProvider('claude-haiku-5-5', client=FakeClient([other])), toy_registry(),
                    Context(task=None, condition='A'), 'S', 'q')
    assert out['stop'] == 'infra_error' and out['response'] is None


# ---------------------------------------------------------------- OpenAI 호환 엔드포인트

import openai  # noqa: E402

from agent import budget, config  # noqa: E402
from agent.providers.openai_compat import OpenAICompatProvider  # noqa: E402


def oa_message(tool=None, text='', usage=None, **extra):
    calls = [NS(id=tool[0], function=NS(name=tool[1], arguments=tool[2]))] if tool else None
    u = NS(prompt_tokens=100, completion_tokens=10, prompt_tokens_details=None, **(usage or {}))
    return NS(choices=[NS(message=NS(content=text, tool_calls=calls), finish_reason='tool_calls' if tool else 'stop')],
              usage=u, model='gemma', id='chatcmpl-1', **extra)


class FakeOpenAI:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def oa_error(status, text):
    resp = NS(status_code=status, headers={}, request=NS(), text=text)
    cls = openai.RateLimitError if status == 429 else openai.APIStatusError
    return cls(f'Error code: {status}', response=resp, body=None)


def test_openai_compat_endpoints_cost_and_request_shape():
    submit = ('c1', 'submit_answer', '{"abstain": false, "reason": null, "answer": "{\\"value\\": 1}", "evidence": "{}"}')
    # 무료 등급: 비용 0, parallel_tool_calls를 보내지 않는다
    client = FakeOpenAI([oa_message(tool=submit)])
    p = OpenAICompatProvider('gemma-4-31b-it', endpoint='google', client=client, sleep=lambda s: None)
    out = run_trial(p, toy_registry(), Context(task=None, condition='A'), 'SYSTEM', '질문')
    assert out['stop'] == 'answered' and out['cost_usd'] == 0.0 and p.name == 'google_ai_studio' and p.free
    req = client.requests[0]
    assert 'parallel_tool_calls' not in req and req['temperature'] == 0 and req['tool_choice'] == 'auto'
    assert req['messages'][0] == dict(role='system', content='SYSTEM') and req['tools'][0]['type'] == 'function'
    # 유료 게이트웨이: 응답의 추정 비용을 쓰고 parallel_tool_calls false를 보낸다
    client = FakeOpenAI([oa_message(tool=submit, usage=dict(estimated_cost=0.002))])
    p = OpenAICompatProvider('google/gemma-4-31B-it', endpoint='baze', client=client, sleep=lambda s: None)
    out = run_trial(p, toy_registry(), Context(task=None, condition='A'), 'S', 'q')
    assert out['cost_usd'] == 0.002 and client.requests[0]['parallel_tool_calls'] is False and p.name == 'openai_compat'
    assert budget.cost_usd('gemma-4-31b-it', dict(input=10)) is None      # 가격표에 없는 모델은 응답의 비용만 쓴다
    assert {config.MODELS[k]['endpoint'] for k in ('gemma-4', 'gemma-4-google', 'gemma-4-openrouter')} == set(config.ENDPOINTS)


def test_openai_compat_rate_limits():
    waits = []
    ok = oa_message(text='끝')
    # 분당 한도(429)는 기다렸다가 다시 보낸다
    client = FakeOpenAI([oa_error(429, '{"error": {"message": "Rate limit exceeded: free-models-per-min"}}'), ok])
    p = OpenAICompatProvider('m', endpoint='openrouter', client=client, sleep=waits.append, workers=4)
    p.start('S', [])
    p.add_user('q')
    t = p.complete()
    assert t.stop == 'end_turn' and t.detail['rate_waits'] == 1 and len(client.requests) == 2 and max(waits) >= 15
    assert abs(p.min_interval - 60 * 4 / config.ENDPOINTS['openrouter']['rpm']) < 1e-9      # 작업자 4개가 분당 한도를 나눠 쓴다
    # 일일 한도와 크레딧 소진은 치명 오류다(러너가 멈춘다)
    for status, text in ((429, '{"error": {"message": "Rate limit exceeded: free-models-per-day"}}'),
                         (429, 'Quota exceeded for metric: generate_requests_per_model_per_day'),
                         (402, '{"detail": {"code": 402, "message": "insufficient_quota"}}'), (401, 'invalid api key')):
        p = OpenAICompatProvider('m', endpoint='google', client=FakeOpenAI([oa_error(status, text)]), sleep=lambda s: None)
        p.start('S', [])
        p.add_user('q')
        with pytest.raises(ProviderError) as e:
            p.complete()
        assert e.value.fatal, (status, text)
    # 서버 오류는 세 번 다시 보낸 뒤에 올린다. 치명 오류가 아니다(시행만 infra_error)
    p = OpenAICompatProvider('m', endpoint='google', client=FakeOpenAI([oa_error(500, 'internal')] * 4), sleep=lambda s: None)
    p.start('S', [])
    p.add_user('q')
    with pytest.raises(ProviderError) as e:
        p.complete()
    assert not e.value.fatal and p.server_retries == 3
    ok2 = FakeOpenAI([oa_error(500, 'internal'), oa_message(text='끝')])
    p = OpenAICompatProvider('m', endpoint='google', client=ok2, sleep=lambda s: None)
    p.start('S', [])
    p.add_user('q')
    assert p.complete().stop == 'end_turn' and len(ok2.requests) == 2
    out = run_trial(OpenAICompatProvider('m', endpoint='google', client=FakeOpenAI([oa_error(402, 'insufficient_quota')]), sleep=lambda s: None),
                    toy_registry(), Context(task=None, condition='A'), 'S', 'q')
    assert out['stop'] == 'infra_error' and out['provider_fatal'] and 'insufficient_quota' in out['provider_error']


def test_openai_compat_input_token_limit():
    """분당 입력 토큰 한도(무료 등급): 한도 안에서는 기다렸다가 보내고, 요청 1건이 한도를 넘으면 보내지 않는다."""
    tpm = config.ENDPOINTS['google']['tpm']
    waits = []
    big = dict(prompt_tokens=tpm - 1000, completion_tokens=10, prompt_tokens_details=None)
    client = FakeOpenAI([NS(choices=[NS(message=NS(content='a', tool_calls=None), finish_reason='stop')], usage=NS(**big), model='m', id='1'),
                         oa_message(text='b')])
    p = OpenAICompatProvider('m', endpoint='google', client=client, sleep=waits.append)
    p.start('S', [])
    p.add_user('q')
    p.complete()
    p._sent[0] = (p._sent[0][0] - 61, p._sent[0][1])        # 1분이 지난 것으로 둔다
    p.add_user('q2')
    assert p.complete().text == 'b' and len(client.requests) == 2
    p._last_prompt = tpm + 1
    p.add_user('q3')
    with pytest.raises(ProviderError) as e:
        p.complete()
    assert not e.value.fatal and '분당 입력 토큰 한도' in str(e.value) and len(client.requests) == 2
    assert OpenAICompatProvider('m', endpoint='google', client=client, workers=4).tpm == tpm / 4
    assert OpenAICompatProvider('m', endpoint='baze', client=client).tpm is None
