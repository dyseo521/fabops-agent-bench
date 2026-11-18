"""루프의 종료 규칙, 예산, 오류 형식, 프롬프트, 과제 로더, 기록, 비용 계산."""
import json

from agent import budget, config, prompt, tasks
from agent.loop import run_trial
from agent.providers.fake import ScriptedProvider
from agent.tools import envelope
from agent.tools.registry import Context, Registry, ToolSpec
from agent.trace import Tracer

OBJ = lambda props: dict(type='object', properties=props, required=list(props), additionalProperties=False)


def toy_registry():
    def echo(ctx, text):
        if text == 'boom':
            raise RuntimeError('bug')
        if text == 'bad':
            raise envelope.ToolError('BAD', '나쁜 입력', '다른 값을 쓴다')
        return envelope.table(ctx.store, ['v'], [[text]])

    def submit(ctx, abstain, reason, answer, evidence):
        ctx.state['final'] = dict(abstain=abstain, reason=reason, answer=json.loads(answer), evidence=json.loads(evidence))
        return dict(ok=True)

    return Registry([
        ToolSpec('echo', '시험용', OBJ(dict(text=dict(type='string'))), echo),
        ToolSpec('submit_answer', '제출', OBJ(dict(abstain=dict(type='boolean'), reason=dict(type=['string', 'null']),
                                                   answer=dict(type='string'), evidence=dict(type='string'))), submit)])


SUBMIT = ('tool', 'submit_answer', dict(abstain=False, reason=None, answer='{"value": 1}', evidence='{}'))


def run(script, **kw):
    tr = Tracer()
    p = ScriptedProvider(script)
    out = run_trial(p, toy_registry(), Context(task=None, condition='A'), 'sys', 'q', tracer=tr, **kw)
    return out, p, tr


def test_answered():
    out, p, tr = run([('tool', 'echo', dict(text='a')), SUBMIT])
    assert out['stop'] == 'answered' and out['tool_calls'] == 1 and out['response']['answer'] == {'value': 1}
    assert json.loads(p.results[0]['content'])['result_id'] == 'q1'
    assert [e['name'] for e in tr.of('tool_call')] == ['echo', 'submit_answer'] and tr.of('trial_end')


def test_budget_then_submit():
    out, p, _ = run([('tool', 'echo', dict(text='a'))] * 3 + [SUBMIT], budget=2)
    assert out['stop'] == 'answered' and out['tool_calls'] == 2
    assert json.loads(p.results[2]['content'])['code'] == 'BUDGET_EXHAUSTED' and p.results[2]['is_error']


def test_budget_exhausted():
    out, _, _ = run([('tool', 'echo', dict(text='a'))] * 5, budget=2)
    assert out['stop'] == 'budget_exhausted' and out['response'] is None and out['tool_calls'] == 2


def test_no_answer_after_nudges():
    out, p, tr = run([('text', '모르겠다')] * 5)
    assert out['stop'] == 'no_answer' and out['nudges'] == config.NUDGE_LIMIT
    assert [h for h in p.history if h[0] == 'user'][1][1] == prompt.NUDGE


def test_error_shapes_are_returned_and_counted():
    out, p, _ = run([('tool', 'nope', {}), ('raw', 'echo', '{bad json'), ('tool', 'echo', dict(text=1)),
                     ('tool', 'echo', dict(text='bad')), SUBMIT])
    codes = [json.loads(r['content']).get('code') for r in p.results[:4]]
    assert codes == ['UNKNOWN_TOOL', 'INVALID_ARGUMENTS', 'INVALID_ARGUMENTS', 'BAD']
    assert all(r['is_error'] for r in p.results[:4]) and out['tool_calls'] == 4 and out['stop'] == 'answered'
    assert set(json.loads(p.results[3]['content'])) == {'ok', 'code', 'message', 'hint'}


def test_refusal_max_tokens_internal():
    assert run([('stop', 'refusal')])[0]['stop'] == 'refusal'
    assert run([('stop', 'max_tokens')])[0]['stop'] == 'max_tokens'
    out, _, _ = run([('tool', 'echo', dict(text='boom')), SUBMIT])
    assert out['stop'] == 'infra_error' and out['response'] is None


def test_preview_caps():
    store = envelope.ResultStore()
    r = envelope.table(store, ['a'], [[i] for i in range(500)])
    assert r['row_count'] == 500 and len(r['rows']) == config.CAPS['preview_rows'] and r['truncated'] and 'run_python' in r['note']
    assert len(store.get('q1')['rows']) == 500
    wide = envelope.table(store, ['a'], [['x' * 5000] for _ in range(40)])
    assert len(envelope.render(wide['rows'])) <= config.CAPS['preview_chars'] and wide['truncated']
    try:
        envelope.table(store, ['a'], [[0]] * (config.CAPS['store_rows'] + 1))
        assert False
    except envelope.ToolError as e:
        assert e.code == 'RESULT_TOO_LARGE'


def test_system_prompt_is_fixed_and_complete():
    a, ha = prompt.system_prompt()
    b, hb = prompt.system_prompt()
    assert a == b and ha == hb
    glossary = open(config.GLOSSARY, encoding='utf-8').read().strip()
    assert glossary in a and f'최대 {config.TOOL_BUDGET}회' in a and f'c는 {config.RS_C}' in a
    assert 'abstain' in a and 'submit_answer' in a
    # 질문 종류별 양식은 시스템 프롬프트에 넣지 않는다(과제 메시지에 넣는다)
    assert 'answer.lots' not in a and 'answer.actions: 조치 목록' not in a


def test_task_loader_hides_answer_fields():
    T = tasks.load('dev')
    assert len(T) == 46 and sum(t.layer == 'action' for t in T) == 20
    asof = [t for t in T if t.time]
    assert len(asof) == 27 and all('__t' in t.data_name for t in asof) and all(t.data_name == t.scenario for t in T if not t.time)
    assert all(t.gate_sc and t.gate_sc['down_id'].startswith('D') for t in T if t.layer == 'action')
    banned = ('abstain_', 'bd_during_pm', 'no_up_peers', 'scenario_id', 'required_evidence', 'NO_IMPACT', 'PM_CQT', 'SINGLE_TOOL')
    for t in T:
        for i in range(3):
            v = tasks.agent_view(t, i)
            msg = prompt.task_message(v)
            assert set(vars(v)) == {'question', 'answer_format'}
            assert not any(b in msg for b in banned), (t.id, msg)
            assert t.scenario not in msg and (t.gate_sc is None or t.gate_sc['id'] not in msg)


def test_trace_file_is_atomic(tmp_path):
    path = str(tmp_path / 'trials' / 't.jsonl')
    tr = Tracer(path)
    tr.event('a', x=1)
    assert not (tmp_path / 'trials' / 't.jsonl').exists()
    tr.close()
    lines = open(path, encoding='utf-8').read().splitlines()
    assert json.loads(lines[0])['event'] == 'a'


def test_cost_and_ledger(tmp_path):
    u = dict(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write_5m=0, cache_write_1h=1_000_000)
    assert abs(budget.cost_usd('claude-sonnet-5-5', u) - (2 + 10 + 0.10 + 4)) < 1e-9
    small = dict(input=1000, output=1000, cache_read=50_000)
    big = dict(input=1000, output=1000, cache_read=150_000)
    assert abs(budget.cost_usd('claude-haiku-5-5', small) - (1000 * 0.10 + 1000 * 0.50 + 50_000 * 0.01) / 1e6) < 1e-12
    assert abs(budget.cost_usd('claude-haiku-5-5', big) - (1000 * 0.50 + 1000 * 2.50 + 150_000 * 0.05) / 1e6) < 1e-12
    assert budget.cost_usd('google/gemma-4-31B-it', dict(input=10)) is None
    assert budget.cost_usd('google/gemma-4-31B-it', dict(cost_usd=0.01)) == 0.01
    led = budget.Ledger(str(tmp_path / 'ledger.jsonl'), cap_usd=1.0)
    led.add('t1', 'claude-haiku-5-5', small, 0.6)
    assert led.allows(0.3) and not led.allows(0.5) and abs(led.spent() - 0.6) < 1e-9
