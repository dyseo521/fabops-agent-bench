"""Anthropic 공식 API(Messages). 키는 환경 변수 ANTHROPIC_API_KEY에서 SDK가 읽는다. 키 값을 출력하거나 기록하지 않는다.

호출 설정(전 조건 같음)
- tool_choice auto + disable_parallel_tool_use(한 응답에 도구 호출 하나). 최신 모델은 특정 도구 호출을 강제할 수 없다
- 도구마다 strict(스키마에 맞는 인자를 보장)
- output_config.effort를 모델별로 고정한다. temperature와 thinking은 보내지 않는다(최신 모델은 temperature를 받지 않고 사고는 항상 켜져 있다)
- 캐시: 시스템 프롬프트 끝에 1시간 지점(도구 정의 + 시스템 프롬프트), 대화 끝은 자동 캐시(5분)
- 거절 시 다른 모델로 넘기는 fallbacks는 쓰지 않는다. 다른 모델이 답하면 "동일 모델" 통제가 깨진다. 거절은 실패로 기록한다
- 대화 기록은 덧붙이기만 한다. 응답의 content(사고 블록 포함)를 그대로 다시 보낸다
"""
import time

import anthropic

from .. import config
from .base import Provider, ProviderError, ToolCall, Turn

STOP = dict(tool_use='tool_use', end_turn='end_turn', max_tokens='max_tokens', refusal='refusal',
            model_context_window_exceeded='context_limit')


class AnthropicProvider(Provider):
    name = 'anthropic'

    def __init__(self, model, effort='medium', thinking=None, max_tokens=None, client=None, max_retries=4, timeout_s=600.0):
        # effort: None이면 보내지 않는다(이전 세대 모델은 effort 인자가 없다). thinking: 'disabled'면 사고를 끈다(None이면 모델 기본값)
        self.model, self.effort, self.thinking = model, effort, thinking
        self.max_tokens = max_tokens or config.MAX_TOKENS
        self.client = client or anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)
        self.settings = dict(effort=effort, thinking=thinking, max_tokens=self.max_tokens, tool_choice='auto', disable_parallel_tool_use=True,
                             strict_tools=True, cache='system 1h + automatic 5m', sdk=anthropic.__version__)
        self.system, self.tools, self.messages, self._results = [], [], [], []

    def start(self, system, tools):
        self.system = [dict(type='text', text=system, cache_control=dict(type='ephemeral', ttl='1h'))]
        self.tools = [dict(name=t['name'], description=t['description'], input_schema=t['input_schema'], strict=True) for t in tools]
        self.messages, self._results = [], []

    def _flush(self):
        if self._results:
            self.messages.append(dict(role='user', content=self._results))
            self._results = []

    def add_user(self, text):
        self._flush()
        self.messages.append(dict(role='user', content=[dict(type='text', text=text)]))

    def add_tool_result(self, call_id, content, is_error=False):
        self._results.append(dict(type='tool_result', tool_use_id=call_id, content=content, is_error=bool(is_error)))

    def complete(self):
        self._flush()
        t0 = time.time()
        extra = {}
        if self.effort:
            extra['output_config'] = dict(effort=self.effort)
        if self.thinking:
            extra['thinking'] = dict(type=self.thinking)
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=self.system, tools=self.tools,
                tool_choice=dict(type='auto', disable_parallel_tool_use=True),
                cache_control=dict(type='ephemeral'), messages=self.messages, **extra)
        except anthropic.BadRequestError as e:
            if 'prompt is too long' in str(e).lower():
                return Turn(stop='context_limit', latency_s=time.time() - t0, model=self.model, detail=dict(error=str(e)[:300]))
            raise ProviderError(f'BadRequestError: {str(e)[:500]}', fatal='credit balance' in str(e).lower())
        except anthropic.APIError as e:
            fatal = getattr(e, 'status_code', None) in (401, 402, 403) or 'credit balance' in str(e).lower()
            raise ProviderError(f'{type(e).__name__}: {str(e)[:500]}', fatal=fatal)
        self.messages.append(dict(role='assistant', content=resp.content))
        u = resp.usage
        by_ttl = getattr(u, 'cache_creation', None)
        w5 = getattr(by_ttl, 'ephemeral_5m_input_tokens', None) if by_ttl is not None else None
        w1 = getattr(by_ttl, 'ephemeral_1h_input_tokens', None) if by_ttl is not None else None
        if w5 is None and w1 is None:       # TTL별 내역이 없으면 전부 5분 쓰기로 센다
            w5, w1 = getattr(u, 'cache_creation_input_tokens', 0) or 0, 0
        usage = dict(input=u.input_tokens or 0, output=u.output_tokens or 0, cache_read=getattr(u, 'cache_read_input_tokens', 0) or 0,
                     cache_write_5m=w5 or 0, cache_write_1h=w1 or 0)
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input) if isinstance(b.input, dict) else None,
                          raw=None if isinstance(b.input, dict) else str(b.input))
                 for b in resp.content if b.type == 'tool_use']
        detail = dict(blocks=[b.type for b in resp.content])
        sd = getattr(resp, 'stop_details', None)
        if resp.stop_reason == 'refusal' and sd is not None:
            detail['refusal'] = dict(category=getattr(sd, 'category', None), explanation=getattr(sd, 'explanation', None))
        return Turn(tool_calls=calls, text='\n'.join(b.text for b in resp.content if b.type == 'text'),
                    stop=STOP.get(resp.stop_reason, 'other'), usage=usage, latency_s=time.time() - t0, model=resp.model,
                    request_id=getattr(resp, '_request_id', None), detail=detail)
