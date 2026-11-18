"""OpenAI 호환 엔드포인트. 공개 모델(Gemma 4)에 쓴다. 엔드포인트는 config.ENDPOINTS에 정의한다.

  baze        BAZE 게이트웨이(유료). 비용은 응답의 usage.estimated_cost(USD)
  google      Google AI Studio 무료 등급(OpenAI 호환 주소). 비용 0. 분당·일일 요청 한도가 있다
  openrouter  OpenRouter의 무료 모델(":free"). 비용 0. 분당·일일 요청 한도가 있다

키는 환경 변수에서 읽는다(.env). 키 값을 출력하거나 기록하지 않는다.
호출 설정: tool_choice auto, temperature 0. 한 응답에 도구 호출이 여러 개 오면 루프가 순서대로 실행한다
(parallel_tool_calls를 받는 엔드포인트에는 false를 보낸다).
요청 간격: 엔드포인트의 분당 한도(rpm)를 작업자 수로 나눈 간격으로 요청을 보낸다. 429를 받으면 기다렸다가 다시 보내고,
일일 한도 소진이면 치명 오류로 올려 러너가 멈춘다. 대화 기록은 덧붙이기만 한다.
실행 전에 scripts/agent_probe_provider.py로 도구 호출 동작과 사용량 필드를 확인한다.
"""
import json
import os
import re
import time

import openai

from .. import config
from .base import Provider, ProviderError, ToolCall, Turn

STOP = dict(tool_calls='tool_use', stop='end_turn', length='max_tokens', content_filter='refusal')
DAILY = re.compile(r'per.?day|daily|PerDay|free-models-per-day|quota.*(day|month)|insufficient_quota', re.I)
RETRY_AFTER = re.compile(r'retry(?:Delay|[- ]after| in)?["\s:=]+(\d+(?:\.\d+)?)\s*s?', re.I)


class OpenAICompatProvider(Provider):
    def __init__(self, model, endpoint='baze', temperature=0, max_tokens=None, client=None, max_retries=2, timeout_s=600.0,
                 workers=1, sleep=time.sleep):
        ep = config.ENDPOINTS[endpoint]
        self.model, self.temperature, self.endpoint = model, temperature, endpoint
        self.name = ep['ledger']                 # 원장의 제공사 이름. 엔드포인트마다 예산을 따로 센다
        self.free = ep.get('free', False)
        self.max_tokens = min(max_tokens or config.MAX_TOKENS, ep.get('max_tokens', 10 ** 9))
        self.send_parallel_flag = ep.get('parallel_flag', True)
        # 이 프로세스의 요청 간격(초). 분당 한도를 작업자들이 나눠 쓴다
        self.min_interval = 60.0 * max(1, workers) / ep['rpm'] if ep.get('rpm') else 0.0
        self.rate_waits, self.server_retries, self._last, self._sleep = 0, 0, 0.0, sleep
        # 분당 입력 토큰 한도(무료 등급). 요청 1건이 한도를 넘으면 보낼 수 없다. 작업자들이 한도를 나눠 쓴다
        self.tpm = ep['tpm'] / max(1, workers) if ep.get('tpm') else None
        self._sent, self._last_prompt = [], 0
        if client is None:
            key = os.environ.get(ep['key_env'])
            url = os.environ.get(ep['url_env']) if ep.get('url_env') else ep['base_url']
            if not key or not url:
                raise ProviderError(f"{ep['key_env']}를 .env에 넣어야 한다", fatal=True)
            client = openai.OpenAI(api_key=key, base_url=url, max_retries=max_retries, timeout=timeout_s)
        self.client = client
        self.settings = dict(endpoint=endpoint, temperature=temperature, max_tokens=self.max_tokens, tool_choice='auto',
                             parallel_tool_calls=False if self.send_parallel_flag else 'not sent', rpm=ep.get('rpm'),
                             workers=workers, sdk=openai.__version__)
        self.tools, self.messages = [], []

    def start(self, system, tools):
        self.tools = [dict(type='function', function=dict(name=t['name'], description=t['description'], parameters=t['input_schema']))
                      for t in tools]
        self.messages = [dict(role='system', content=system)]

    def add_user(self, text):
        self.messages.append(dict(role='user', content=text))

    def add_tool_result(self, call_id, content, is_error=False):
        self.messages.append(dict(role='tool', tool_call_id=call_id, content=content))

    def _request(self):
        kw = dict(model=self.model, messages=self.messages, tools=self.tools, tool_choice='auto', temperature=self.temperature,
                  max_tokens=self.max_tokens)
        if self.send_parallel_flag:
            kw['parallel_tool_calls'] = False
        if self.tpm:
            est = self._last_prompt or sum(len(str(m.get('content') or '')) for m in self.messages) // 3
            if est > self.tpm:
                raise ProviderError(f'요청 1건의 입력(약 {est:,}토큰)이 이 엔드포인트의 분당 입력 토큰 한도({self.tpm:,.0f})를 넘는다')
            while True:
                now = time.time()
                self._sent = [(t, n) for t, n in self._sent if now - t < 60]
                if sum(n for _, n in self._sent) + est <= self.tpm:
                    break
                self.rate_waits += 1
                self._sleep(max(1.0, 61 - (now - self._sent[0][0])))
        for attempt in range(6):
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                self._sleep(wait)
            self._last = time.time()
            try:
                return self.client.chat.completions.create(**kw)
            except openai.APIError as e:
                status = getattr(e, 'status_code', None)
                body = ''
                try:        # 오류 사유가 본문에만 있는 엔드포인트가 있다(예: 402 insufficient_quota)
                    body = e.response.text[:500]
                except Exception:
                    pass
                text = f'{type(e).__name__}: {str(e)[:300]} {body}'.strip()
                if status is not None and status >= 500 and attempt < 3:      # 서버의 일시 오류(예: 500 INTERNAL)
                    self.server_retries += 1
                    self._sleep(10.0 * (attempt + 1))
                    continue
                if status == 429 and not DAILY.search(text) and attempt < 5:
                    m = RETRY_AFTER.search(text)
                    try:
                        header = float(e.response.headers.get('retry-after', 0))
                    except Exception:
                        header = 0.0
                    delay = min(90.0, max(header, float(m.group(1)) if m else 0.0, 15.0 * (attempt + 1)))
                    self.rate_waits += 1
                    self._sleep(delay)
                    continue
                # 인증 실패, 권한 없음, 크레딧·일일 한도 소진은 다시 시도해도 같다
                raise ProviderError(text, fatal=status in (401, 402, 403) or (status == 429 and bool(DAILY.search(text))))
        raise ProviderError('분당 한도 초과가 이어졌다(429)')

    def complete(self):
        t0 = time.time()
        waits0 = self.rate_waits
        resp = self._request()
        if not resp.choices:
            err = getattr(resp, 'error', None)      # 본문에 오류를 담아 200으로 돌려주는 엔드포인트가 있다
            text = json.dumps(err, ensure_ascii=False)[:400] if err else '응답에 choices가 없다'
            raise ProviderError(text, fatal=bool(err) and bool(DAILY.search(text)))
        ch = resp.choices[0]
        msg = ch.message
        calls, record = [], []
        for i, tc in enumerate(msg.tool_calls or []):
            raw = tc.function.arguments or ''
            try:
                args = json.loads(raw) if raw.strip() else {}
                args = args if isinstance(args, dict) else None
            except json.JSONDecodeError:
                args = None
            cid = tc.id or f'call_{len(self.messages)}_{i}'      # id를 주지 않는 엔드포인트가 있다
            calls.append(ToolCall(id=cid, name=tc.function.name, input=args, raw=None if args is not None else raw))
            record.append(dict(id=cid, type='function', function=dict(name=tc.function.name, arguments=raw)))
        assistant = dict(role='assistant', content=msg.content or '')
        if record:
            assistant['tool_calls'] = record
        self.messages.append(assistant)
        u = resp.usage
        if u is not None:
            self._last_prompt = (u.prompt_tokens or 0) + (u.completion_tokens or 0)
            self._sent.append((time.time(), u.prompt_tokens or 0))
        cached = (getattr(getattr(u, 'prompt_tokens_details', None), 'cached_tokens', None) or 0) if u is not None else 0
        usage = dict(input=(u.prompt_tokens or 0) - cached, output=u.completion_tokens or 0, cache_read=cached,
                     cache_write_5m=0, cache_write_1h=0) if u is not None else {}
        # 비용: 유료 게이트웨이는 usage.estimated_cost(BAZE) 또는 usage.cost(OpenRouter)를 준다. 무료 등급은 0
        billing = getattr(resp, 'billing', None)
        est = [getattr(u, k, None) for k in ('estimated_cost', 'cost')] if u is not None else []
        est = next((x for x in est if isinstance(x, (int, float))), None)
        if isinstance(billing, dict) and billing.get('cost_usd') is not None:
            usage['cost_usd'] = float(billing['cost_usd'])
        elif est is not None:
            usage['cost_usd'] = float(est)
        elif self.free:
            usage['cost_usd'] = 0.0
        stop = 'tool_use' if calls else STOP.get(ch.finish_reason, 'other')
        return Turn(tool_calls=calls, text=msg.content or '', stop=stop, usage=usage, latency_s=time.time() - t0,
                    model=resp.model or self.model, request_id=resp.id,
                    detail=dict(finish_reason=ch.finish_reason, endpoint=self.endpoint, provider=getattr(resp, 'provider', None),
                                rate_waits=self.rate_waits - waits0, server_retries=self.server_retries, n_tool_calls=len(calls)))
