"""대본 모델. 정해진 순서로 도구를 호출한다. 하니스의 무비용 검증에 쓴다."""
from .base import Provider, ToolCall, Turn


class ScriptedProvider(Provider):
    """script의 각 단계
      ('tool', 이름, 인자 dict)      도구 호출
      ('raw', 이름, 원문 문자열)     읽을 수 없는 인자로 도구 호출
      ('text', 문장)                 도구 없이 끝내는 응답
      ('stop', 종료 사유)            refusal, max_tokens 등
      callable(provider) -> 단계     앞 결과를 보고 정하는 단계
    대본이 끝나면 ('text', '')를 낸다."""
    name = 'scripted'

    def __init__(self, script, model='scripted', usage=None):
        self.script, self.model, self._usage = list(script), model, usage or {}
        self.settings = dict(scripted=True)
        self.system, self.tools, self.history, self.results, self._i = None, None, [], [], 0

    def start(self, system, tools):
        self.system, self.tools = system, tools

    def add_user(self, text):
        self.history.append(('user', text))

    def add_tool_result(self, call_id, content, is_error=False):
        self.history.append(('tool_result', call_id, content, is_error))
        self.results.append(dict(id=call_id, content=content, is_error=is_error))

    def complete(self):
        step = self.script[self._i] if self._i < len(self.script) else ('text', '')
        self._i += 1
        if callable(step):
            step = step(self)
        turn = Turn(usage=dict(self._usage), model=self.model)
        if step[0] == 'tool':
            turn.tool_calls, turn.stop = [ToolCall(id=f'call_{self._i}', name=step[1], input=step[2])], 'tool_use'
        elif step[0] == 'raw':
            turn.tool_calls, turn.stop = [ToolCall(id=f'call_{self._i}', name=step[1], raw=step[2])], 'tool_use'
        elif step[0] == 'stop':
            turn.stop = step[1]
        else:
            turn.text = step[1]
        self.history.append(('assistant', step))
        return turn
