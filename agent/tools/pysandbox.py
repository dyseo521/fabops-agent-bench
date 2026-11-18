"""run_python 도구. 앞선 조회·계산 결과(q1, q2, ...)를 모델의 문맥을 거치지 않고 후처리한다. 전 조건이 같은 도구를 쓴다.

보안 목표: 실행 코드는 파일을 읽지 못하고(저장소에 정답 파일이 있다) 프로세스를 만들지 못하고 네트워크를 쓰지 못한다.
상정한 위협은 open()이나 import os를 시도하는 LLM이다. 작정한 공격자를 막는 경계가 아니다(한계는 아래).

계층
1. 정적 검사(AST). 허용 목록 밖 import와 금지 이름·속성을 실행 전에 반려한다. 프로세스를 만들지 않는다
   - 지시와 다르게 허용한 것(차단하면 호출만 낭비된다): __name__(이름·속성), __init__, __version__(속성),
     경로 없이 호출하는 to_csv·to_json·to_string·to_markdown·to_html·to_latex(문자열만 돌려준다)
   - 지시보다 넓게 막은 것: 밑줄 하나로 시작하는 속성(self·cls와 namedtuple의 공개 API 제외), 모듈 이름 속성(os, sys, io 등),
     프레임 속성(f_globals, gi_frame 등), import *, case C(attr=...)의 속성
2. 1회용 자식 프로세스(_sandbox_child.py). 작업 디렉터리는 빈 임시 디렉터리, 환경 변수는 새로 구성(부모의 변수와 API 키 없음),
   요청은 stdin의 JSON, 응답은 stdout의 표식 줄(요청마다 다른 난수)
3. 자식 안의 감사 훅(sys.addaudithook)과 제한한 builtins. 허용 목록 방식이다(통과시킬 이벤트만 정하고 나머지는 차단).
   차단하면 프로세스를 바로 끝내고 violation으로 보고한다
   - 실행 중 import는 훅 설치 시점에 적재된 최상위 모듈의 하위 모듈과 계산용 표준 모듈만 허용한다. 파일 열기는 그 모듈의
     패키지 디렉터리 안 읽기만 허용한다
   - 허용 라이브러리가 선택 의존성을 확인하는 import(try: import cython)는 미설치로 응답하고 blocked_imports에 적는다(위반 아님)
   - scipy는 미리 적재하든지 막든지 둘 중 하나다. pandas가 scipy를 쓰는 연산(순위 상관, interpolate)은 정적 검사가 찾아 적재한다
4. 상한. 실행 시간은 부모가 지킨다. 메모리와 프로세스 수는 Windows Job Object가 지킨다(커밋 메모리 상한, 활성 프로세스 1개,
   핸들을 닫으면 종료). Job Object를 만들지 못하면 psutil로 메모리를 감시하고 결과의 limiter에 'psutil'을 적는다
   (이때는 프로세스 수 상한이 없고 감사 훅만 프로세스 생성을 막는다)

인터프리터 플래그: -s -B
- -I(격리 모드)는 쓰지 않는다. -I는 PYTHONHASHSEED를 무시해 str 해시가 실행마다 달라진다(집합 순회 순서가 재현되지 않는다)
- -I의 효과는 따로 갖춘다. 환경 변수는 새로 구성하고, 사용자 site는 -s로 끄고, 스크립트 디렉터리는 자식이 sys.path에서 뺀다
- -S는 쓰지 못한다. site-packages가 탐색 경로에서 빠져 numpy를 가져오지 못한다
- venv의 python.exe는 실제 인터프리터를 자식으로 실행하는 런처다(프로세스 2개). 활성 프로세스 1개 상한과 맞지 않으므로
  기본 인터프리터를 직접 실행하고 런처와 같은 방식(__PYVENV_LAUNCHER__)으로 venv를 알려 준다

시간과 메모리
- 실행 시간 상한은 자식이 준비 표식을 낸 뒤부터 잰다. 모듈 적재 시간은 사용자 코드의 시간에서 뺀다
- numpy, pandas, scipy.stats는 코드가 쓰는 것만 적재한다(정적 검사가 정한다). 호출당 소요 시간의 중앙값(2026-11-14, 5회):
  무거운 모듈 없음 0.12초, numpy 0.33초, pandas 0.83초, scipy.stats 1.16초, pandas + scipy.stats 1.72초.
  다른 작업이 실행 중일 때는 2~3배까지 늘었다
- OPENBLAS_NUM_THREADS=1이 필요하다. 기본 스레드 수에서는 적재만으로 커밋 메모리가 785 MB다(1개면 78 MB)

한계
- Windows에는 파일 시스템·네트워크의 OS 수준 격리가 없다. 파일과 소켓 차단은 감사 훅에 의존한다
- os.stat, os.path.exists, os.getcwd, os.getlogin처럼 감사 이벤트가 없는 호출은 막지 못한다
  (파일 존재·크기·수정 시각, 사용자 이름이 드러난다. 파일 내용은 아니다). 닿으려면 정적 검사를 우회해야 한다
- os, ctypes, subprocess 모듈은 numpy를 적재한 실행의 자식 프로세스 안에 있다. 정적 검사를 우회해 닿더라도 파일·프로세스·소켓
  동작은 감사 이벤트로 차단된다. 다만 CPython 3.10은 이미 만들어진 ctypes 함수 포인터의 호출(ctypes.memmove, 주소로 만든
  CFUNCTYPE)에 이벤트를 내지 않는다. 이 경로로 네이티브 코드를 실행하면 감사 훅을 피할 수 있다(프로세스 수와 메모리 상한은 남는다)
- 감사 훅은 허용 라이브러리의 네이티브 코드가 Python의 open을 거치지 않고 여는 파일을 보지 못한다. 그런 경로가 있는
  pyarrow, lxml 등은 미설치와 같게 만든다(_sandbox_child.DISABLED). numpy의 fromfile·tofile, pandas의 read_*·to_*는
  Python의 open을 거치는 것을 시험으로 확인했다
- 훅의 허용 이벤트 목록은 Python 3.10 기준이다. 버전을 올리면 새 이벤트가 차단되어 정상 코드가 위반으로 보고될 수 있다
"""
import ast
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .. import config
from . import envelope
from .envelope import ToolError
from .registry import ToolSpec

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_sandbox_child.py')
ALLOWED_IMPORTS = ('math', 'statistics', 'datetime', 'collections', 'itertools', 'functools', 'json', 're', 'bisect', 'heapq',
                   'decimal', 'fractions', 'numpy', 'pandas', 'scipy.stats')
HEAVY = ('numpy', 'pandas', 'scipy.stats')
BANNED_NAMES = ('open', 'exec', 'eval', 'compile', '__import__', 'input', 'globals', 'locals', 'vars', 'getattr', 'setattr',
                'delattr', 'breakpoint', 'help', 'exit', 'quit')
SAFE_DUNDER_NAMES = frozenset(('__name__',))                            # if __name__ == '__main__':
SAFE_DUNDER_ATTRS = frozenset(('__name__', '__init__', '__version__'))  # type(x).__name__, super().__init__()
SAFE_PRIVATE_ATTRS = frozenset(('_asdict', '_replace', '_fields', '_make', '_field_defaults'))     # namedtuple의 공개 API
# 인자 없이 부르면 문자열을 돌려주고 경로를 주면 파일에 쓰는 메서드. 경로 없는 호출만 허용한다
PATH_OPTIONAL = frozenset(('to_csv', 'to_json', 'to_string', 'to_markdown', 'to_html', 'to_latex'))
PATH_KEYWORDS = frozenset(('path_or_buf', 'path_or_buffer', 'buf', 'path', 'storage_options'))
BANNED_ATTRS = frozenset(BANNED_NAMES) - {'compile'} | frozenset('''
    to_pickle to_parquet to_excel to_hdf to_sql to_feather to_clipboard to_stata to_orc to_gbq to_xml
    load loadtxt genfromtxt fromfile fromregex tofile save savetxt dump memmap open_memmap DataSource ExcelFile ExcelWriter HDFStore
    eval query
    system popen Popen startfile environ getenv listdir scandir urlopen read_text read_bytes write_text write_bytes unlink rmtree
    makedirs chdir getcwd
    os sys io builtins importlib subprocess ctypes ctypeslib socket pathlib shutil pickle marshal gc inspect codecs tempfile mmap
    sqlite3 urllib http operator modules f2py distutils testing
    f_globals f_locals f_builtins f_back f_code gi_frame gi_code cr_frame cr_code ag_frame ag_code tb_frame tb_next
'''.split())
BANNED_ATTR_PREFIXES = ('read_', 'savez')

MAX_RESULT_BYTES = 2_000_000    # result의 JSON 크기 상한
STDOUT_KEEP = 100_000           # 자식이 보관하는 출력 문자 수. 모델에 보여 주는 양은 CAPS['preview_chars']
STARTUP_LIMIT_S = 60            # 자식이 준비 표식을 낼 때까지의 상한. 넘으면 하니스 결함으로 본다
OUTPUT_CAP = 16 * 1024 * 1024   # 부모가 받는 stdout 바이트 상한

HINT_FORBIDDEN = ('파일, 프로세스, 네트워크는 쓸 수 없다. import할 수 있는 모듈은 ' + ', '.join(ALLOWED_IMPORTS)
                  + '뿐이고 자료는 inputs로 넘긴 결과(results, df)만 쓴다')
HINTS = dict(
    query='조건 선택은 df[df["열"] > 값]처럼 쓴다. ',
    path='to_csv, to_json, to_string은 경로 없이 호출해 문자열로만 받는다. ',
    dunder='밑줄 두 개로 시작하는 이름과 속성은 __name__, __init__, __version__만 쓸 수 있다. ',
    private='밑줄로 시작하는 속성은 self와 cls의 것만 쓸 수 있다. ',
    getattr='속성은 obj.attr로 직접 쓴다. ',
    compile='정규식은 re.compile로 쓴다. ',
)


# ── 1. 정적 검사 ────────────────────────────────────────────────────────────────────────────────────

class _Checker(ast.NodeVisitor):
    def __init__(self):
        self.problems = []          # (행, 열, 문구, 힌트 키)
        self.preload = set()        # 적재할 무거운 모듈
        self._path_free = set()     # 경로 없이 호출한 PATH_OPTIONAL 속성 노드의 id
        self._attrs, self._strings = set(), set()   # pandas가 scipy를 쓰는 연산을 찾는 데 쓴다

    def bad(self, node, text, hint=None):
        self.problems.append((getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0), text, hint))

    def need(self, module):
        top = module.partition('.')[0]
        if top == 'scipy':
            self.preload.add('scipy.stats')
        elif top in HEAVY:
            self.preload.add(top)

    def attr(self, node, name, owner=None):
        if name.startswith('__'):
            if name not in SAFE_DUNDER_ATTRS:
                self.bad(node, f'.{name}', 'dunder')
        elif name.startswith('_'):
            mine = isinstance(owner, ast.Name) and owner.id in ('self', 'cls')
            if name not in SAFE_PRIVATE_ATTRS and not mine:
                self.bad(node, f'.{name}', 'private')
        elif name in PATH_OPTIONAL:
            if id(node) not in self._path_free:
                self.bad(node, f'경로를 주는 .{name}', 'path')
        elif name in BANNED_ATTRS or name.startswith(BANNED_ATTR_PREFIXES):
            self.bad(node, f'.{name}', 'query' if name in ('query', 'eval') else 'getattr' if name == 'getattr' else None)

    def visit_Import(self, node):
        for a in node.names:
            if a.name in ALLOWED_IMPORTS or a.name == 'scipy':
                self.need(a.name)
            else:
                self.bad(node, f'import {a.name}')

    def visit_ImportFrom(self, node):
        module = node.module or ''
        if node.level:
            self.bad(node, '상대 import')
        elif module in ALLOWED_IMPORTS or (module == 'scipy' and all(a.name == 'stats' for a in node.names)):
            self.need(module)
        else:
            self.bad(node, f'from {module} import')
        for a in node.names:
            if a.name == '*':
                self.bad(node, 'import *')
            else:
                self.attr(node, a.name)     # 가져오는 이름도 속성과 같은 규칙을 따른다

    def visit_Name(self, node):
        if node.id in BANNED_NAMES:
            self.bad(node, node.id, 'getattr' if node.id == 'getattr' else 'compile' if node.id == 'compile' else None)
        elif node.id.startswith('__') and node.id not in SAFE_DUNDER_NAMES:
            self.bad(node, node.id, 'dunder')
        elif node.id in ('df', 'pd'):
            self.preload.add('pandas')
        elif node.id == 'np':
            self.preload.add('numpy')

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in PATH_OPTIONAL and not node.args \
                and not any(k.arg is None or k.arg in PATH_KEYWORDS for k in node.keywords):
            self._path_free.add(id(f))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self._attrs.add(node.attr)
        self.attr(node, node.attr, node.value)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and len(node.value) < 20:
            self._strings.add(node.value.lower())

    def pandas_needs_scipy(self):
        """pandas가 안에서 scipy를 가져오는 연산: 순위 상관(Series.corr, corrwith의 spearman·kendall)과 interpolate.

        자식은 scipy를 미리 적재하든지 막든지 둘 중 하나로 실행한다(감사 훅 아래에서 늦게 적재하지 않는다).
        """
        rank = self._attrs & {'corr', 'corrwith'} and self._strings & {'spearman', 'kendall'}
        return 'pandas' in self.preload and bool(rank or 'interpolate' in self._attrs)

    def visit_MatchClass(self, node):       # case C(attr=...)는 속성을 읽는다
        for name in node.kwd_attrs:
            self.attr(node, name)
        self.generic_visit(node)


def check(code):
    """-> (적재할 모듈 집합, 문제 목록). 문제는 (행, 열, 문구, 힌트 키)이고 행 순서다. 구문 오류는 SyntaxError"""
    c = _Checker()
    c.visit(ast.parse(code))
    if c.pandas_needs_scipy():
        c.preload.add('scipy.stats')
    return c.preload, sorted(c.problems, key=lambda p: p[:2])


# ── 4. 상한: Windows Job Object ─────────────────────────────────────────────────────────────────────

_JOB_API = None


def _job_api():
    """kernel32 함수와 구조체. 처음 쓸 때 한 번 만든다."""
    global _JOB_API
    if _JOB_API is None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                          'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IoCounters), ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        class UiRestrictions(ctypes.Structure):
            _fields_ = [('UIRestrictionsClass', wintypes.DWORD)]

        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        for f in (k.SetInformationJobObject, k.QueryInformationJobObject, k.AssignProcessToJobObject, k.TerminateJobObject,
                  k.CloseHandle):
            f.restype = wintypes.BOOL
        _JOB_API = (ctypes, k, Extended, UiRestrictions)
    return _JOB_API


class _Job:
    """프로세스 메모리(커밋) 상한, 활성 프로세스 1개, 핸들을 닫으면 종료, UI 접근(클립보드, 데스크톱) 제한."""
    EXTENDED_LIMIT, UI_RESTRICTIONS = 9, 4
    # ACTIVE_PROCESS | PROCESS_MEMORY | DIE_ON_UNHANDLED_EXCEPTION | KILL_ON_JOB_CLOSE
    FLAGS = 0x8 | 0x100 | 0x400 | 0x2000

    def __init__(self, mem_bytes):
        ctypes, k, Extended, UiRestrictions = _job_api()
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObjectW')
        info = Extended()
        info.BasicLimitInformation.LimitFlags = self.FLAGS
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = mem_bytes
        if not k.SetInformationJobObject(self.handle, self.EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, 'SetInformationJobObject')
        ui = UiRestrictions(0xFF)
        k.SetInformationJobObject(self.handle, self.UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui))   # 실패해도 진행한다

    def assign(self, process_handle):
        ctypes, k = _job_api()[:2]
        if not k.AssignProcessToJobObject(self.handle, int(process_handle)):
            raise OSError(ctypes.get_last_error(), 'AssignProcessToJobObject')

    def terminate(self):
        _job_api()[1].TerminateJobObject(self.handle, 1)

    def peak_bytes(self):
        ctypes, k, Extended, _ = _job_api()
        info = Extended()
        ok = k.QueryInformationJobObject(self.handle, self.EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info), None)
        return info.PeakProcessMemoryUsed if ok else None

    def close(self):
        if self.handle:
            _job_api()[1].CloseHandle(self.handle)
            self.handle = None


# ── 2. 자식 프로세스 ────────────────────────────────────────────────────────────────────────────────

def child_command(tmp):
    """-> (명령, 환경 변수). 환경 변수는 새로 구성한다. 부모의 변수는 SYSTEMROOT만 넘긴다(없으면 numpy가 적재되지 않는다)."""
    env = dict(PYTHONHASHSEED='0', TEMP=tmp, TMP=tmp, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    if sys.platform != 'win32':
        env['TMPDIR'] = tmp
    if os.environ.get('SYSTEMROOT'):
        env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
    exe, base = sys.executable, getattr(sys, '_base_executable', None)
    if sys.platform == 'win32' and sys.prefix != sys.base_prefix and base and os.path.isfile(base) \
            and os.path.normcase(base) != os.path.normcase(exe):
        env['__PYVENV_LAUNCHER__'] = exe    # venv 런처가 하는 일을 직접 한다(프로세스 1개)
        exe = base
    return [exe, '-s', '-B', CHILD], env


class _Pump:
    """자식의 표준 입출력을 스레드로 처리한다. 준비 표식을 본 시각을 기록한다."""

    def __init__(self, proc, payload, mark):
        self.out, self.err, self.ready_at = bytearray(), bytearray(), None
        self._ready = b'\n' + mark + b' READY'
        self._threads = [threading.Thread(target=f, args=a, daemon=True) for f, a in (
            (self._feed, (proc.stdin, payload)), (self._read, (proc.stdout, self.out, True)), (self._read, (proc.stderr, self.err, False)))]
        for t in self._threads:
            t.start()

    @staticmethod
    def _feed(pipe, payload):
        try:
            pipe.write(payload)
            pipe.close()
        except (OSError, ValueError):
            pass

    def _read(self, pipe, buf, watch):
        try:
            while True:
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                if len(buf) < OUTPUT_CAP:
                    buf += chunk
                if watch and self.ready_at is None and self._ready in buf:
                    self.ready_at = time.monotonic()
        except (OSError, ValueError):
            pass

    def join(self):
        for t in self._threads:
            t.join(5)


def _wait(proc, pump, watcher, t_start, timeout_s, mem_bytes):
    """자식이 끝나기를 기다린다. -> 부모가 끝낸 사유: None(스스로 끝남) | 'timeout' | 'memory' | 'startup'"""
    poll = 0.02 if watcher is not None else 0.05
    while True:
        try:
            proc.wait(timeout=poll)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if pump.ready_at is not None:
            if now - pump.ready_at > timeout_s:
                return 'timeout'
        elif now - t_start > STARTUP_LIMIT_S:
            return 'startup'
        if watcher is not None:
            try:
                m = watcher.memory_info()
            except Exception:
                continue
            if max(m.rss, m.vms) > mem_bytes:       # Windows의 vms는 커밋 메모리다(Job Object의 기준과 같다)
                return 'memory'


def _kill(proc, job):
    if job is not None:
        job.terminate()
    else:
        try:
            import psutil
            for c in psutil.Process(proc.pid).children(recursive=True):
                c.kill()
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn(code, inputs, preload, timeout_s, mem_mb, limiter):
    """자식을 1회 실행한다. -> dict(resp, reason, rc, ready, err, peak, limiter)"""
    mark = ('@@pysandbox:' + secrets.token_hex(16)).encode('ascii')
    request = dict(code=code, inputs=inputs, preload=sorted(preload), mark=mark.decode('ascii'), allowed_imports=ALLOWED_IMPORTS,
                   banned_builtins=[n for n in BANNED_NAMES if n != '__import__'], max_rows=config.CAPS['store_rows'],
                   max_bytes=MAX_RESULT_BYTES, stdout_keep=STDOUT_KEEP)
    payload = json.dumps(request, default=str).encode('ascii')
    mem_bytes = int(mem_mb) * 1024 * 1024
    tmp = tempfile.mkdtemp(prefix='pysbx_')
    proc = job = None
    try:
        cmd, env = child_command(tmp)
        t_start = time.monotonic()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        watcher = None
        if limiter is None and sys.platform == 'win32':
            try:        # 자식은 요청을 받기 전에는 사용자 코드를 실행하지 않는다. 요청을 보내기 전에 Job에 넣는다
                job = _Job(mem_bytes)
                job.assign(proc._handle)
                limiter = 'job'
            except OSError:
                if job is not None:
                    job.close()
                job = None
        if job is None:
            try:
                import psutil
                watcher, limiter = psutil.Process(proc.pid), 'psutil'
            except Exception:
                limiter = 'none'
        pump = _Pump(proc, payload, mark)
        reason = _wait(proc, pump, watcher, t_start, timeout_s, mem_bytes)
        if reason:
            _kill(proc, job)
        proc.wait(timeout=30)
        pump.join()
        resp, token = None, b'\n' + mark + b' RESULT '
        i = pump.out.rfind(token)
        if i >= 0:
            j = pump.out.find(b'\n', i + len(token))
            try:
                resp = json.loads(bytes(pump.out[i + len(token):j if j >= 0 else len(pump.out)]).decode('utf-8', 'replace'))
            except ValueError:
                resp = None
        return dict(resp=resp if isinstance(resp, dict) else None, reason=reason, rc=proc.returncode, ready=pump.ready_at is not None,
                    err=bytes(pump.err[-2000:]).decode('utf-8', 'replace'), peak=job.peak_bytes() if job else None, limiter=limiter)
    finally:
        if proc is not None and proc.poll() is None:
            _kill(proc, job)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        if job is not None:
            job.close()
        for _ in range(5):
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.exists(tmp):
                break
            time.sleep(0.05)


# ── 실행 ────────────────────────────────────────────────────────────────────────────────────────────

def run(code, inputs, timeout_s=config.CAPS['python_timeout_s'], mem_mb=config.CAPS['python_mem_mb'], _static=True, _limiter=None):
    """코드를 격리 실행한다. inputs: {"q1": {"columns": [...], "rows": [[...], ...]}, ...}

    -> dict(ok, stdout, result, result_table, error, violation, wall_s, limiter, blocked_imports)
       error: {code, message, hint} 또는 None. code는 PYTHON_ERROR, PYTHON_TIMEOUT, PYTHON_MEMORY, PYTHON_FORBIDDEN,
              PYTHON_RESULT, SANDBOX_FAILURE(하니스 결함. 모델의 잘못이 아니다)
       violation: 차단한 동작의 기록(정적 검사 또는 실행 중 차단). 없으면 None
       limiter: 메모리 상한을 지킨 수단. 'job' | 'psutil' | 'none'. 정적 검사에서 끝나면 None
       blocked_imports: 허용 라이브러리가 시도했다가 막힌 선택 의존성 import(위반이 아니다. 기록용)
    _static, _limiter는 시험용이다. _static=False는 정적 검사를 건너뛰고 실행 계층만 시험한다. _limiter='psutil'은 감시 경로를 시험한다.
    """
    t0 = time.monotonic()
    meta = dict(limiter=None, blocked_imports=[])

    def done(ok, stdout='', result=None, result_table=None, error=None, violation=None):
        if error:
            error = dict(code=error[0], message=error[1], hint=error[2])
        return dict(ok=ok, stdout=stdout, result=result, result_table=result_table, error=error, violation=violation,
                    wall_s=round(time.monotonic() - t0, 3), **meta)

    try:
        preload, problems = check(code)
    except SyntaxError as e:
        return done(False, error=('PYTHON_ERROR', f'SyntaxError: {e.msg} ({e.lineno}행)', '구문을 수정해 다시 실행한다'))
    except (ValueError, TypeError, RecursionError, MemoryError) as e:
        return done(False, error=('PYTHON_ERROR', f'코드를 읽을 수 없다: {type(e).__name__}', 'code에 Python 코드 문자열을 넣는다'))
    if problems and _static:
        shown = ', '.join(f'{text}({line}행)' for line, _, text, _ in problems[:3])
        more = f' 외 {len(problems) - 3}건' if len(problems) > 3 else ''
        hint = ''.join(dict.fromkeys(HINTS[h] for *_, h in problems[:3] if h)) + HINT_FORBIDDEN
        return done(False, error=('PYTHON_FORBIDDEN', f'허용되지 않는 코드다: {shown}{more}', hint),
                    violation='static: ' + '; '.join(f'{text} (line {line})' for line, _, text, _ in problems[:5]))

    try:
        r = _spawn(code, inputs, preload, timeout_s, mem_mb, _limiter)
    except (OSError, subprocess.SubprocessError) as e:
        return done(False, error=('SANDBOX_FAILURE', f'실행 프로세스를 다루지 못했다: {type(e).__name__}: {e}', ''))
    resp = r['resp']
    meta['limiter'] = r['limiter']
    memory = ('PYTHON_MEMORY', f'메모리 상한 {mem_mb} MB를 넘었다', '필요한 열과 행만 남기거나 조회 단계에서 집계한다')
    if resp is not None:
        stdout = str(resp.get('stdout') or '')
        if resp.get('dropped'):
            stdout += f"\n…(출력 {resp['dropped']:,}자 생략)"
        if isinstance(resp.get('blocked_imports'), list):
            meta['blocked_imports'] = [str(m) for m in resp['blocked_imports']]
        err = resp.get('error') or {}
        if resp.get('ok'):
            table = resp.get('result_table')
            if table is not None and not (isinstance(table, dict) and isinstance(table.get('columns'), list)
                                          and isinstance(table.get('rows'), list) and len(table['rows']) <= config.CAPS['store_rows']):
                return done(False, stdout=stdout, error=('PYTHON_RESULT', 'result의 표 형식이 맞지 않다', ''))
            return done(True, stdout=stdout, result=resp.get('result'), result_table=table)
        if err.get('code') == 'PYTHON_FORBIDDEN':
            return done(False, stdout=stdout, violation=str(resp.get('violation') or err.get('kind')),
                        error=('PYTHON_FORBIDDEN', f"허용되지 않는 동작을 차단했다: {err.get('kind')}", HINT_FORBIDDEN))
        if err.get('code') == 'PYTHON_MEMORY':
            return done(False, stdout=stdout, error=memory)
        if err.get('code') == 'PYTHON_ERROR':
            message = str(err.get('message'))
            hint = '표시된 행을 수정해 다시 실행한다'
            if message.startswith(('ModuleNotFoundError', 'ImportError')) and 'scipy' in message:
                hint = '이 연산은 scipy를 쓴다. 코드에 import scipy.stats를 넣고 다시 실행한다'
            elif message.startswith(('ModuleNotFoundError', 'ImportError')):
                hint = '선택 의존성(matplotlib, tabulate, pyarrow 등)이 필요한 기능은 쓸 수 없다. 다른 방법으로 계산하거나 출력한다'
            return done(False, stdout=stdout, error=('PYTHON_ERROR', message, hint))
        if err.get('code') == 'PYTHON_RESULT':
            return done(False, stdout=stdout, error=(
                'PYTHON_RESULT', str(err.get('message')),
                'result에는 숫자, 문자열, list, dict, DataFrame만 넣는다. 큰 결과는 집계하거나 필요한 열만 남긴다'))
        return done(False, error=('SANDBOX_FAILURE', f'자식의 응답을 해석할 수 없다: {str(resp)[:200]}', ''))
    if r['reason'] == 'timeout':
        return done(False, error=('PYTHON_TIMEOUT', f'실행 시간 상한 {timeout_s}초를 넘었다',
                                  '반복을 줄이거나 pandas 연산으로 바꾼다. 큰 자료는 조회 단계에서 집계한다'))
    if r['reason'] == 'memory':
        return done(False, error=memory)
    if not r['ready']:      # 사용자 코드를 실행하기 전에 끝났다
        return done(False, error=('SANDBOX_FAILURE',
                                  f"실행 환경을 준비하지 못했다(사유 {r['reason']}, 종료 코드 {r['rc']}): {r['err'][-800:]}", ''))
    if (r['peak'] or 0) >= 0.9 * mem_mb * 1024 * 1024 or r['rc'] == 0xC0000017 or 'MemoryError' in r['err']:
        return done(False, error=memory)
    return done(False, error=('PYTHON_ERROR', f"실행 프로세스가 비정상 종료했다(종료 코드 {r['rc']})",
                              '재귀 깊이와 자료 크기를 줄여 다시 실행한다'))


# ── 도구 정의 ───────────────────────────────────────────────────────────────────────────────────────

def _handler(ctx, code, inputs):
    caps = config.CAPS
    out = run(code, {h: ctx.store.get(h) for h in inputs}, timeout_s=caps['python_timeout_s'], mem_mb=caps['python_mem_mb'])
    trace = dict(sandbox_violation=out['violation'], wall_s=out['wall_s'], limiter=out['limiter'])
    if out['blocked_imports']:
        trace['blocked_imports'] = out['blocked_imports']
    if not out['ok']:
        e = out['error']
        if out['violation']:
            ctx.state.setdefault('sandbox_violations', []).append(out['violation'])
        if e['code'] == 'SANDBOX_FAILURE':      # 하니스 결함. registry가 INTERNAL로 처리하고 시행을 채점하지 않는다
            raise RuntimeError(e['message'])
        raise ToolError(e['code'], e['message'], e['hint'])
    stdout, limit = out['stdout'], caps['preview_chars']
    if len(stdout) > limit:
        stdout = stdout[:limit] + f'\n…(출력 {len(stdout):,}자 중 {limit:,}자만 표시)'
    res = dict(ok=True, stdout=stdout)
    if out['result_table'] is not None:
        res.update(envelope.table(ctx.store, out['result_table']['columns'], out['result_table']['rows']))
    else:
        res['result'] = out['result']
        text = envelope.render(out['result'])
        if len(text) > limit:
            res.update(result=text[:limit] + '…', truncated=True,
                       note=f'result가 {len(text):,}자다. 앞 {limit:,}자만 표시한다. 표(DataFrame 또는 dict의 list)로 돌려주면 result_id로 보관된다')
        elif out['result'] is None and not stdout:
            res['note'] = 'result 변수도 출력도 없다. 돌려줄 값을 result에 넣거나 print한다'
    res['trace'] = trace
    return res


def spec():
    t = config.CAPS['python_timeout_s']
    description = (
        '앞선 조회 결과만으로 Python 코드를 실행한다(구간 절단, 순위, 집합 연산, 검정 같은 후처리). '
        'inputs에 result_id를 넘기면 코드 안에서 results["q3"]는 행 dict의 list이고 df("q3")는 pandas DataFrame이며, '
        f'import할 수 있는 모듈은 {", ".join(ALLOWED_IMPORTS)}뿐이다. '
        '돌려줄 값은 변수 result에 넣거나 print한다(DataFrame과 dict의 list는 새 result_id로 보관된다). '
        f'파일과 네트워크는 쓸 수 없고 실행 시간 상한은 {t}초다.')
    schema = dict(type='object', additionalProperties=False, required=['code', 'inputs'], properties=dict(
        code=dict(type='string', description='실행할 Python 코드'),
        inputs=dict(type='array', items=dict(type='string'), description='코드에서 쓸 result_id 목록. 없으면 빈 배열')))
    return ToolSpec('run_python', description, schema, _handler)
