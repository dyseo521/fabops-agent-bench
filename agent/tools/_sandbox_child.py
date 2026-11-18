"""run_python의 자식 프로세스. pysandbox.run이 1회용으로 실행한다. 단독으로 쓰지 않는다.

요청은 표준 입력의 JSON 1건, 응답은 표준 출력의 표식 줄이다(표식에는 요청마다 다른 난수가 들어간다).
순서: 허용 모듈 적재 -> results, df 구성 -> 감사 훅 설치 -> 준비 표식 -> 사용자 코드 실행 -> 결과 변환 -> 응답 표식.
차단한 동작은 응답의 violation에 싣고 프로세스를 바로 끝낸다. 차단 뒤의 코드는 실행되지 않는다.

이 파일은 agent 패키지를 가져오지 않는다. 허용 import 목록과 금지 builtins는 요청에 실려 온다(정의는 pysandbox.py 한 곳).
"""
import sys

FILENAME = '<run_python>'

# 설치되어 있어도 쓰지 않는 선택 의존성. sys.modules에 None을 넣어 "미설치"와 같게 만든다.
# pyarrow, lxml, tables 등은 Python의 open을 거치지 않고 파일을 읽으므로 감사 훅이 보지 못한다. 가속 모듈은 수치 재현성 때문에 뺀다
DISABLED = ('pyarrow', 'numexpr', 'bottleneck', 'numba', 'tables', 'h5py', 'fsspec', 'lxml', 'bs4', 'html5lib', 'openpyxl', 'xlrd',
            'xlsxwriter', 'odf', 'pyxlsb', 'python_calamine', 'sqlalchemy', 'matplotlib', 'jinja2', 'tabulate', 'xarray', 'zstandard',
            'fastparquet', 'pyreadstat', 'blosc', 'brotli', 'snappy', 'gcsfs', 's3fs', 'pandas_gbq', 'psycopg2', 'pymysql',
            'adbc_driver_manager', 'adbc_driver_postgresql', 'adbc_driver_sqlite', 'dataframe_api_compat', 'qtpy', 'PyQt5',
            'hypothesis', 'pytest', 'IPython', 'PIL', 'pooch', 'requests', 'httpx', 'duckdb', 'polars')

# 허용 라이브러리가 실행 중에 늦게 가져올 수 있는 표준 모듈. 계산·서식 전용이고, 파일·프로세스 동작은 어차피 감사 이벤트로 차단된다
BENIGN_TOP = frozenset('''
    _strptime calendar locale _locale time copy operator numbers textwrap string enum types typing dataclasses struct array cmath
    random _random bisect _bisect heapq _heapq decimal _decimal _pydecimal datetime _datetime json _json statistics _statistics
    fractions math itertools functools _functools collections _collections _collections_abc re sre_compile sre_parse sre_constants _sre
    encodings codecs _codecs _codecs_kr _codecs_jp _codecs_cn _codecs_tw _codecs_hk _codecs_iso2022 _multibytecodec unicodedata stringprep
    weakref contextlib abc reprlib keyword token tokenize linecache traceback warnings pprint difflib html graphlib queue _queue
    hashlib _hashlib _blake2 _sha512 _sha3 _md5 _sha1 _sha256 binascii base64 zlib bz2 _bz2 lzma _lzma gzip zipfile _compression
    zoneinfo _zoneinfo tzdata atexit logging pkgutil pydoc timeit sysconfig
'''.split())
# 파일·프로세스·네트워크 모듈. 미리 적재되지 않은 실행에서 누가 요청하든 import를 위반으로 보고한다(os.popen의 subprocess 등)
SENSITIVE_TOP = frozenset('''
    subprocess socket _socket ctypes _ctypes sqlite3 _sqlite3 mmap shutil tempfile glob pathlib urllib http ftplib smtplib poplib
    imaplib telnetlib nntplib xmlrpc ssl _ssl asyncio _asyncio _overlapped multiprocessing _multiprocessing concurrent webbrowser
    winreg _winapi msvcrt pickle _pickle shelve dbm zipimport runpy code codeop pdb bdb socketserver select selectors ensurepip venv
    pip setuptools distutils _posixsubprocess pty fcntl resource
'''.split())

# 차단하지 않는 감사 이벤트. import, open, os.listdir, object.__setattr__은 조건을 따로 본다. 그 밖의 이벤트는 전부 차단한다
PASS_EVENTS = frozenset((
    'exec', 'compile', 'marshal.loads', 'marshal.load', 'marshal.dumps',      # 늦은 import, namedtuple, dataclass
    'sys._getframe', 'builtins.id', 'object.__getattr__', 'object.__delattr__',  # namedtuple, pandas 경고 위치 계산
    'gc.get_referents',                                                        # pandas의 사본 대입 점검(_check_setitem_copy)
    'array.__new__', '_thread.start_new_thread', 'sys.excepthook', 'sys.unraisablehook', 'time.sleep', 'sys._getframemodulename',
    'sys.set_asyncgen_hooks_firstiter', 'sys.set_asyncgen_hooks_finalizer',
    'cpython._PySys_ClearAuditHooks', 'cpython.PyInterpreterState_Clear',
))
READ_MODES = ('r', 'rb')


class SandboxViolation(BaseException):
    """차단한 동작. 정상 경로에서는 violate가 프로세스를 끝내므로 이 예외는 그 대비다."""


class _ResultError(Exception):
    """result를 JSON으로 돌려줄 수 없다."""


class _Results(dict):
    def __missing__(self, key):
        raise KeyError(f'{key}: inputs에 넣은 result_id만 쓸 수 있다')


class _Capture:
    """print 출력을 받는 문자 스트림. 상한을 넘는 부분은 세기만 한다(메모리 보호)."""
    encoding = 'utf-8'
    closed = False

    def __init__(self, limit):
        self.parts, self.size, self.limit, self.dropped = [], 0, limit, 0

    def write(self, s):
        s = s if isinstance(s, str) else str(s)
        room = self.limit - self.size
        if room > 0:
            self.parts.append(s[:room])
            self.size += min(len(s), room)
        self.dropped += max(0, len(s) - max(room, 0))
        return len(s)

    def writelines(self, lines):
        for s in lines:
            self.write(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError('표준 출력에는 파일 번호가 없다')

    def getvalue(self):
        return ''.join(self.parts)


def _make_emit(out, mark):
    import json

    def emit(kind, payload):
        out.write(('\n' + mark + ' ' + kind + ' ' + json.dumps(payload, ensure_ascii=False)).encode('utf-8', 'replace') + b'\n')
        out.flush()
    return emit


def _make_violate(emit, cap, hard_exit):
    """차단 보고. 호출 즉시 응답을 쓰고 프로세스를 끝낸다.

    builtins 대체물용과 감사 훅용을 따로 만든다(두 번 호출). 사용자 코드가 닿는 함수와 훅이 클로저 셀을 공유하지 않게 한다.
    """
    def violate(kind, detail):
        try:
            emit('RESULT', dict(ok=False, stdout=cap.getvalue(), dropped=cap.dropped, result=None, result_table=None,
                                error=dict(code='PYTHON_FORBIDDEN', kind=kind), violation=detail))
        finally:
            hard_exit(0)
    return violate


def _make_hook(violate, search, tops, modules, soft):
    """감사 훅. 정책 값은 이 함수의 지역 변수로만 둔다. 훅 함수는 인터프리터만 참조한다.

    허용 밖 import는 요청한 쪽과 대상에 따라 다르게 처리한다.
    - 허용 라이브러리의 파일이 요청했고 대상이 SENSITIVE_TOP이 아닌 것(try: import cython 같은 선택 의존성 확인):
      ModuleNotFoundError만 내고 soft에 적는다. 라이브러리는 미설치로 알고 계속 실행한다. 모듈은 적재되지 않는다
    - 그 밖(사용자 코드의 요청, 파일·프로세스·네트워크 모듈): 위반으로 보고하고 프로세스를 끝낸다
    """
    import os
    normcase, abspath, fsdecode, sep = os.path.normcase, os.path.abspath, os.fsdecode, os.sep
    passed, read_modes, sensitive, getframe = PASS_EVENTS, READ_MODES, SENSITIVE_TOP, sys._getframe

    def top_of(path):
        """모듈 탐색 경로 아래의 파일·디렉터리가 속한 최상위 모듈 이름(소문자). 탐색 경로 자체는 '', 그 밖은 None"""
        try:
            p = normcase(abspath(fsdecode(path)))
        except Exception:
            return None
        for root in search:                         # 긴 경로부터 본다
            if p == root:
                return ''
            if p.startswith(root + sep):
                parts = p[len(root) + 1:].split(sep)
                first = parts[1] if parts[0] == '__pycache__' and len(parts) > 1 else parts[0]
                return first.partition('.')[0]
        return None

    def hook(event, args):
        if event in passed:
            return
        if event == 'import':
            name, filename = args[0], args[1]
            top = name.partition('.')[0] if isinstance(name, str) else ''
            if top in modules and modules[top] is None:
                return                              # 비활성 선택 의존성. import 기구가 ImportError를 낸다
            if top.lower() in tops and (filename is None or top_of(filename) in tops):
                return
            f = getframe(1)                         # import를 요청한 프레임. import 기구의 프레임은 건너뛴다
            while f is not None and f.f_code.co_filename.startswith('<frozen importlib'):
                f = f.f_back
            if f is not None and top not in sensitive and top_of(f.f_code.co_filename) in tops:
                if len(soft) < 50:
                    soft.append(str(name))
                raise ModuleNotFoundError(f"No module named '{name}' (run_python에서는 가져올 수 없다)", name=str(name))
            violate('import ' + str(name), f'import {name}' + (f' ({filename})' if filename else ''))
        elif event == 'open':
            path, mode = args[0], args[1]
            if mode in read_modes and not isinstance(path, int) and top_of(path) in tops:
                return                              # 허용 모듈의 늦은 import가 읽는 자기 패키지 파일
            violate('파일 열기(open)', f'open {path!r} mode={mode!r}')
        elif event in ('os.listdir', 'os.scandir'):
            path = args[0] if args else None
            if event == 'os.listdir' and path is not None and not isinstance(path, int):
                top = top_of(path)
                if top == '' or top in tops:
                    return                          # import 기구의 디렉터리 조회
            violate(event, f'{event} {path!r}')
        elif event == 'object.__setattr__':
            if len(args) < 2 or args[1] != '__code__':
                return                              # namedtuple의 __defaults__ 설정 등
            violate(event, f'{event} __code__')
        else:
            violate(event, f'{event} {repr(args)[:300]}')
        raise SandboxViolation(event)
    return hook


def _make_stub(name, violate):
    def stub(*args, **kwargs):
        violate(name + '()', f'builtin {name}()')
        raise SandboxViolation(name)
    stub.__name__ = name
    return stub


def _make_import(real_import, allowed, loaded, violate):
    """사용자 코드의 import 문이 부르는 __import__. 허용 목록에 있고 미리 적재한 모듈만 돌려준다.

    C 확장이 PyImport_Import로 가져올 때도 현재 프레임(사용자 코드)의 __import__가 불린다(numpy의 a.mean(), datetime.strptime).
    이 호출은 fromlist가 list다(3.10은 빈 list). import 문은 None이나 tuple을 넘기므로 구분된다. 내부 호출은 그대로 넘기고 감사 훅이 본다.
    """
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if type(fromlist) is list:
            return real_import(name, globals, locals, fromlist, level)
        ok = level == 0 and (name in allowed or (name == 'scipy' and all(f == 'stats' for f in (fromlist or ()))))
        if ok and (name if name != 'scipy' else 'scipy.stats') in loaded:
            return real_import(name, globals, locals, fromlist, level)
        violate('import ' + str(name), f'import {name} (user code)')
        raise SandboxViolation(name)
    return safe_import


def _make_df(pd, tables):
    def df(result_id):
        """조회 결과를 pandas DataFrame으로 돌려준다. 호출할 때마다 새 사본이다."""
        if result_id not in tables:
            raise KeyError(f'{result_id}: inputs에 넣은 result_id만 쓸 수 있다')
        t = tables[result_id]
        return pd.DataFrame(t['rows'], columns=t['columns'])
    return df


def _make_showwarning(cap):
    def showwarning(message, category, filename, lineno, file=None, line=None):
        where = f' ({lineno}행)' if filename == FILENAME else ''
        cap.write(f'{category.__name__}: {message}{where}\n')
    return showwarning


def _make_converter(np, pd, max_rows):
    """result -> (JSON 값, 표). DataFrame, Series, 키가 같은 dict의 list는 표로 돌려준다."""
    import collections
    import datetime
    import decimal
    import fractions
    inf = float('inf')
    views = (type({}.keys()), type({}.values()), type({}.items()))

    def key(k):
        return k if isinstance(k, str) else str(k)

    def conv(v, where, depth=0):
        if v is None or isinstance(v, (str, bool, int)):
            return v
        if isinstance(v, float):
            if v != v:
                return None
            return v if -inf < v < inf else ('inf' if v > 0 else '-inf')
        if depth > 40:
            raise _ResultError(f'{where}의 중첩이 너무 깊다')
        if pd is not None and (v is pd.NaT or v is pd.NA):
            return None
        if isinstance(v, datetime.datetime):
            return v.isoformat(sep=' ')
        if isinstance(v, (datetime.date, datetime.time)):
            return v.isoformat()
        if isinstance(v, datetime.timedelta):
            return str(v)
        if isinstance(v, decimal.Decimal):
            return conv(float(v), where, depth + 1)
        if isinstance(v, fractions.Fraction):
            return float(v)
        if np is not None:
            if isinstance(v, (np.datetime64, np.timedelta64)):
                return None if np.isnat(v) else str(v).replace('T', ' ')
            if isinstance(v, np.generic):
                return conv(v.item(), where, depth + 1)
            if isinstance(v, np.ndarray):
                return conv(v.tolist(), where, depth + 1)
        if pd is not None:
            if isinstance(v, pd.DataFrame):
                return conv(v.to_dict('records'), where, depth + 1)
            if isinstance(v, pd.Series):
                return conv(dict(zip(v.index.tolist(), v.tolist())), where, depth + 1)
            if isinstance(v, (pd.Index, pd.api.extensions.ExtensionArray)):
                return conv(v.tolist(), where, depth + 1)
            if isinstance(v, (pd.Interval, pd.Period)):
                return str(v)
        if isinstance(v, dict):
            return {key(k): conv(x, f'{where}[{k!r}]', depth + 1) for k, x in v.items()}
        if isinstance(v, tuple) and hasattr(v, '_asdict'):          # namedtuple, scipy의 검정 결과
            return conv(v._asdict(), where, depth + 1)
        if isinstance(v, (list, tuple, range, collections.deque) + views):
            w = where + '[]'
            return [conv(x, w, depth + 1) for x in v]
        if isinstance(v, (set, frozenset)):
            w = where + '[]'
            items = [conv(x, w, depth + 1) for x in v]
            try:
                return sorted(items)
            except TypeError:
                return items
        raise _ResultError(f'{where}의 형식 {type(v).__name__}은 돌려줄 수 없다')

    def column_name(c):
        if isinstance(c, tuple):
            return '_'.join(str(p) for p in c if p not in ('', None))
        return key(c)

    def frame_table(frame):
        if len(frame) > max_rows:
            raise _ResultError(f'result가 {len(frame):,}행이다(상한 {max_rows:,}행)')
        idx = frame.index
        plain = idx.nlevels == 1 and idx.name is None and pd.api.types.is_integer_dtype(idx.dtype)
        if not plain:                               # 이름이 있거나 정수가 아닌 인덱스(groupby 키 등)는 열로 내린다
            try:
                frame = frame.reset_index()
            except Exception:
                frame = frame.reset_index(drop=True)
        cols = [column_name(c) for c in frame.columns]
        data = [[conv(x, cols[i]) for x in frame.iloc[:, i].tolist()] for i in range(frame.shape[1])]
        return dict(columns=cols, rows=[list(r) for r in zip(*data)] if data else [])

    def convert(value):
        if pd is not None and isinstance(value, pd.Series):
            value = value.to_frame(name='value' if value.name is None else value.name)
        if pd is not None and isinstance(value, pd.DataFrame):
            return None, frame_table(value)
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            first = value[0].keys()
            if first and all(r.keys() == first for r in value):
                if len(value) > max_rows:
                    raise _ResultError(f'result가 {len(value):,}행이다(상한 {max_rows:,}행)')
                keys = list(first)
                cols = [key(k) for k in keys]
                return None, dict(columns=cols, rows=[[conv(r[k], c) for k, c in zip(keys, cols)] for r in value])
        return conv(value, 'result'), None
    return convert


def _user_line(tb):
    """트레이스백에서 사용자 코드의 가장 안쪽 행 번호"""
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == FILENAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def main():
    import builtins
    import gc
    import json
    import linecache
    import os
    import traceback  # noqa: F401  맨 아래의 결함 보고가 훅 설치 뒤에도 쓸 수 있게 미리 가져온다
    import warnings

    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.abspath(p)) != here]   # -I 대신 스크립트 디렉터리를 뺀다
    linecache.cache[__file__] = (0, None, [], __file__)     # 트레이스백과 경고가 이 파일을 읽으려 하지 않게 한다(훅이 차단한다)

    stdout = sys.stdout.buffer
    req = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    emit = _make_emit(stdout, req['mark'])
    soft = []       # 허용 라이브러리가 시도했다가 막힌 선택 의존성 import. 위반은 아니고 기록만 한다

    def finish(**kw):
        emit('RESULT', dict(dict(ok=False, stdout=cap.getvalue(), dropped=cap.dropped, result=None, result_table=None,
                                 error=None, violation=None, blocked_imports=soft), **kw))
        os._exit(0)

    # 1. 모듈 적재. 무거운 세 모듈은 코드가 쓰는 것만 가져온다(부모의 정적 검사가 정한다)
    preload = set(req['preload'])
    for name in DISABLED + (() if 'scipy.stats' in preload else ('scipy',)):
        sys.modules.setdefault(name, None)      # scipy는 적재하든지 막든지 둘 중 하나다. 훅 아래에서 늦게 적재하지 않는다
    import bisect, collections, datetime, decimal, fractions, functools, heapq, itertools, math, re, statistics  # noqa: E401, F401
    import _strptime, calendar, copy, locale, numbers, operator  # noqa: E401, F401  datetime.strptime 등이 실행 중에 가져온다
    np = pd = None
    if preload:
        import numpy as np
    if 'pandas' in preload:
        import pandas as pd
        pd.set_option('display.width', 200)
        pd.set_option('display.max_columns', 40)
    if 'scipy.stats' in preload:
        import scipy.stats  # noqa: F401
        try:
            from multiprocessing import Pool  # noqa: F401  scipy의 MapWrapper가 실행 중에 가져온다
        except ImportError:
            pass
    loaded = {m for m in req['allowed_imports'] if sys.modules.get(m) is not None}

    # 2. 사용자 코드에 줄 이름
    tables = req['inputs']
    results = _Results((h, [dict(zip(t['columns'], r)) for r in t['rows']]) for h, t in tables.items())
    cap = _Capture(req['stdout_keep'])
    try:
        code = compile(req['code'], FILENAME, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        finish(error=dict(code='PYTHON_ERROR', message=f'{type(e).__name__}: {getattr(e, "msg", e)} ({getattr(e, "lineno", "?")}행)'))
    lines = req['code'].splitlines()
    user_violate = _make_violate(_make_emit(stdout, req['mark']), cap, os._exit)
    safe = {k: v for k, v in vars(builtins).items() if k not in ('copyright', 'credits', 'license', '__loader__', '__spec__')}
    for name in req['banned_builtins']:
        safe[name] = _make_stub(name, user_violate)
    safe['__import__'] = _make_import(builtins.__import__, frozenset(req['allowed_imports']), frozenset(loaded), user_violate)
    ns = {'__name__': '__main__', '__builtins__': safe, 'results': results}
    if np is not None:
        ns['np'] = np
    if pd is not None:
        ns['pd'], ns['df'] = pd, _make_df(pd, tables)
    convert = _make_converter(np, pd, req['max_rows'])

    # 3. 감사 훅. 이 시점에 적재된 최상위 모듈과 BENIGN_TOP의 하위 모듈만 더 가져올 수 있다
    search = tuple(sorted({os.path.normcase(os.path.abspath(p)) for p in sys.path if p and os.path.isdir(p)}, key=len, reverse=True))
    tops = frozenset({k.partition('.')[0].lower() for k, m in sys.modules.items() if m is not None} | BENIGN_TOP)
    sys.stdout = sys.stderr = cap
    warnings.showwarning = _make_showwarning(cap)
    sys.addaudithook(_make_hook(_make_violate(_make_emit(stdout, req['mark']), cap, os._exit), search, tops, sys.modules, soft))
    emit('READY', {})

    # 4. 실행
    try:
        exec(code, ns)
    except MemoryError:
        ns.clear()
        gc.collect()
        finish(error=dict(code='PYTHON_MEMORY', message='MemoryError'))
    except SandboxViolation as e:                   # violate가 끝내지 못한 경우의 대비
        finish(error=dict(code='PYTHON_FORBIDDEN', kind=str(e)), violation=f'blocked {e}')
    except BaseException as e:
        if not (isinstance(e, SystemExit) and e.code in (None, 0)):
            n = _user_line(e.__traceback__)
            src = lines[n - 1].strip()[:120] if n and n <= len(lines) else ''
            where = f' ({n}행: {src})' if n else ''
            text = str(e) if not isinstance(e, SystemExit) else f'종료 코드 {e.code}'
            finish(error=dict(code='PYTHON_ERROR', message=f'{type(e).__name__}: {text[:500]}{where}'))

    # 5. 결과 변환
    try:
        result, table = convert(ns.get('result'))
        body = json.dumps(dict(result=result, result_table=table), ensure_ascii=False, allow_nan=False)
        if len(body.encode('utf-8', 'replace')) > req['max_bytes']:
            raise _ResultError(f"result가 {len(body) / 1e6:.1f} MB다(상한 {req['max_bytes'] / 1e6:.0f} MB)")
    except _ResultError as e:
        finish(error=dict(code='PYTHON_RESULT', message=str(e)))
    except MemoryError:
        ns.clear()
        gc.collect()
        finish(error=dict(code='PYTHON_MEMORY', message='MemoryError'))
    except (RecursionError, ValueError, TypeError) as e:
        finish(error=dict(code='PYTHON_RESULT', message=f'result를 JSON으로 바꿀 수 없다: {type(e).__name__}: {str(e)[:200]}'))
    finish(ok=True, result=result, result_table=table)


if __name__ == '__main__':
    try:
        main()
    except BaseException:                           # 여기까지 오면 하니스의 결함이다. 원인을 실제 stderr에 남긴다
        import os
        import traceback
        try:
            sys.__stderr__.write(traceback.format_exc())
            sys.__stderr__.flush()
        finally:
            os._exit(3)
