"""KG 질의를 실행하는 별도 프로세스.

pyoxigraph 질의는 실행 중에 멈출 수 없다. 시간 제한을 넘으면 프로세스를 종료하고 다음 질의 때 다시 띄운다.
저장소는 읽기 전용으로 연다. 러너 1개에 작업 프로세스 1개만 둔다(메모리 16 GB 기준).
"""
import multiprocessing as mp

from .sparql_common import FAB

XSD = 'http://www.w3.org/2001/XMLSchema#'
_INT = {XSD + t for t in ('integer', 'int', 'long', 'short', 'byte', 'nonNegativeInteger', 'positiveInteger',
                          'unsignedInt', 'unsignedLong', 'negativeInteger', 'nonPositiveInteger')}
_FLOAT = {XSD + t for t in ('double', 'float', 'decimal')}


def render_iri(iri):
    """fab 어휘는 접두사 이름으로, 개체 IRI는 질의에 그대로 붙여 쓸 수 있게 <...>로 적는다."""
    return 'fab:' + iri[len(FAB):] if iri.startswith(FAB) else f'<{iri}>'


def _convert(term):
    """pyoxigraph 항 -> JSON으로 옮길 수 있는 값."""
    if term is None:
        return None
    kind = type(term).__name__
    if kind == 'NamedNode':
        return render_iri(term.value)
    if kind == 'BlankNode':
        return '_:' + term.value
    if kind == 'Literal':
        dt = term.datatype.value if term.datatype is not None else None
        try:
            if dt in _INT:
                return int(term.value)
            if dt in _FLOAT:
                return float(term.value)
            if dt == XSD + 'boolean':
                return term.value in ('true', '1')
        except ValueError:
            pass
        return term.value
    return str(term)


def _serve(conn, store_path):
    import pyoxigraph as ox
    try:
        store = ox.Store.read_only(store_path)
    except Exception as e:      # 저장소를 열 수 없다. 하니스의 오류다
        conn.send(('fatal', f'{type(e).__name__}: {e}'))
        return
    conn.send(('ready', None))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        query, union, limit = msg
        try:
            res = store.query(query, use_default_graph_as_union=union)
            if isinstance(res, bool):
                out = ('ok', ['ask'], [[res]])
            elif hasattr(res, 'variables'):
                cols = [v.value for v in res.variables]
                rows = []
                for sol in res:
                    rows.append([_convert(sol[i]) for i in range(len(cols))])
                    if len(rows) >= limit:
                        break
                out = ('ok', cols, rows)
            else:
                out = ('error', 'form', 'SELECT 또는 ASK 질의만 실행할 수 있다')
        except SyntaxError as e:
            out = ('error', 'syntax', str(e))
        except Exception as e:
            out = ('error', 'eval', f'{type(e).__name__}: {e}')
        conn.send(out)


class WorkerTimeout(Exception):
    pass


class WorkerError(Exception):
    """질의 오류. kind: syntax | eval | form"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class KgWorker:
    def __init__(self, store_path, start_timeout_s=120):
        self.store_path, self.start_timeout_s = store_path, start_timeout_s
        self.proc = self.conn = None
        self.restarts = 0

    def _start(self):
        ctx = mp.get_context('spawn')
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_serve, args=(child, self.store_path), daemon=True)
        self.proc.start()
        child.close()
        if not self.conn.poll(self.start_timeout_s):
            self._kill()
            raise RuntimeError('KG 작업 프로세스가 시작되지 않았다')
        tag, detail = self.conn.recv()
        if tag != 'ready':
            self._kill()
            raise RuntimeError(f'KG 저장소를 열 수 없다: {detail}')

    def _kill(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc.join(10)
        if self.conn is not None:
            self.conn.close()
        self.proc = self.conn = None

    def query(self, sparql, union=False, timeout_s=60, limit=20001):
        """-> (columns, rows). 시간 초과는 WorkerTimeout, 질의 오류는 WorkerError."""
        if self.proc is None or not self.proc.is_alive():
            self._start()
        self.conn.send((sparql, union, limit))
        if not self.conn.poll(timeout_s):
            self._kill()
            self.restarts += 1
            raise WorkerTimeout()
        try:
            out = self.conn.recv()
        except (EOFError, OSError):          # 프로세스가 죽었다(메모리 부족 등)
            self._kill()
            self.restarts += 1
            raise WorkerError('eval', '질의 실행 중 작업 프로세스가 종료됐다')
        if out[0] == 'ok':
            return out[1], out[2]
        raise WorkerError(out[1], out[2])

    def close(self):
        if self.proc is not None and self.proc.is_alive():
            try:
                self.conn.send(None)
                self.proc.join(5)
            except (OSError, ValueError):
                pass
        self._kill()
