"""도구 결과와 오류의 공통 형식, 출력 상한, 결과 보관소. 전 조건이 같은 형식과 같은 상한을 쓴다.

결과  {"ok": true, "result_id": "q3", "columns": [...], "row_count": 1234, "rows": [[...], ...], "truncated": true, "note": "..."}
오류  {"ok": false, "code": "...", "message": "...", "hint": "..."}
"""
import json

from .. import config

CELL_CHARS = 300        # 미리보기에서 셀 하나의 문자 상한(보관소에는 전체를 둔다)
PYTHON_HINT = True      # 제거 실험(no_python)에서 False. 잘린 결과의 안내문에서 계산 도구 언급을 뺀다


class ToolError(Exception):
    """모델에 돌려줄 오류. hint에는 수정 방법을 적는다."""

    def __init__(self, code, message, hint=''):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


def error(code, message, hint=''):
    return dict(ok=False, code=code, message=message, hint=hint)


def render(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


class ResultStore:
    """시행 1건 동안의 질의·계산 결과. 핸들은 q1, q2, ... 순서로 붙는다."""

    def __init__(self):
        self._items = {}

    def put(self, columns, rows):
        h = f'q{len(self._items) + 1}'
        self._items[h] = dict(columns=list(columns), rows=rows)
        return h

    def get(self, handle):
        if handle not in self._items:
            raise ToolError('UNKNOWN_RESULT', f'결과 {handle}가 없다', '이 대화에서 받은 result_id만 쓸 수 있다')
        return self._items[handle]

    def handles(self):
        return list(self._items)


def _clip(v):
    return v[:CELL_CHARS] + '…' if isinstance(v, str) and len(v) > CELL_CHARS else v


def table(store, columns, rows, caps=None, **extra):
    """표 결과를 보관소에 넣고 모델에 보여 줄 미리보기를 만든다. rows: 행의 list(각 행은 list)."""
    caps = caps or config.CAPS
    if len(rows) > caps['store_rows']:
        raise ToolError('RESULT_TOO_LARGE', f"결과가 {len(rows):,}행이다(상한 {caps['store_rows']:,}행)",
                        '조건을 추가하거나 집계해서 행 수를 줄인다')
    handle = store.put(columns, rows)
    shown = [[_clip(v) for v in r] for r in rows[:caps['preview_rows']]]
    while len(shown) > 1 and len(render(shown)) > caps['preview_chars']:
        shown = shown[:max(1, len(shown) * 2 // 3)]
    out = dict(ok=True, result_id=handle, columns=list(columns), row_count=len(rows), rows=shown, truncated=len(shown) < len(rows))
    if out['truncated']:
        out['note'] = (f'{len(shown)} / {len(rows):,}행 표시. 집계하거나 조건을 추가하거나 result_id를 run_python에 넘긴다' if PYTHON_HINT
                       else f'{len(shown)} / {len(rows):,}행 표시. 집계하거나 조건을 추가한다')
    out.update(extra)
    return out
