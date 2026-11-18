"""run_python 격리 실행기. 실행: .venv-agent\\Scripts\\python -m pytest tests/agent/test_pysandbox.py -q

정상 사용, 오류 부호, 탈출 시도(정적 검사 계층과 실행 계층을 따로 시험), 자식 환경, 소요 시간.
"""
import json
import os
import sys
import uuid
from datetime import datetime

import pytest

from agent import config
from agent.providers.base import ToolCall
from agent.tools import pysandbox
from agent.tools.registry import Context, Registry
from agent.trace import Tracer

WIN = sys.platform == 'win32'
win_only = pytest.mark.skipif(not WIN, reason='Windows 전용 API')
COLUMNS = ['lot', 'tool', 'qty', 't_in', 't_out']
ROWS = [[f'L{i:03d}', f'T{i % 3}', i, f'2018-03-0{1 + i % 3} 0{i % 9}:00:00', f'2018-03-0{1 + i % 3} 1{i % 9}:{10 + 10 * (i % 5)}:00']
        for i in range(30)]
INPUTS = {'q1': dict(columns=COLUMNS, rows=ROWS)}


def run(code, **kw):
    return pysandbox.run(code, kw.pop('inputs', INPUTS), **kw)


def show(out):
    return json.dumps(out, ensure_ascii=False, default=str)[:3000]


def ok(out):
    assert out['ok'] and out['error'] is None and out['violation'] is None, show(out)
    assert set(out) == {'ok', 'stdout', 'result', 'result_table', 'error', 'violation', 'wall_s', 'limiter', 'blocked_imports'}
    return out


def failed(out, code):
    assert not out['ok'] and out['error'] and out['error']['code'] == code, show(out)
    assert set(out['error']) == {'code', 'message', 'hint'} and out['result'] is None and out['result_table'] is None
    return out


def context():
    ctx = Context(task=None, condition='A', tracer=Tracer())
    assert ctx.store.put(COLUMNS, ROWS) == 'q1'
    return ctx


def call(ctx, code, inputs=('q1',)):
    res = Registry([pysandbox.spec()]).call(ctx, ToolCall('c1', 'run_python', dict(code=code, inputs=list(inputs))))
    assert 'trace' not in res.payload and '"trace"' not in res.content
    return res


# ── 정상 사용 ───────────────────────────────────────────────────────────────────────────────────────

def test_sum_and_sort_over_results():
    out = ok(run('rows = results["q1"]\n'
                 'result = {"total": sum(r["qty"] for r in rows), "top": [r["lot"] for r in sorted(rows, key=lambda r: -r["qty"])[:3]]}'))
    assert out['result'] == {'total': 435, 'top': ['L029', 'L028', 'L027']} and out['stdout'] == ''
    assert out['limiter'] == ('job' if WIN else 'psutil')


def test_trivial_run_is_fast():
    walls = [ok(run('result = len(results["q1"])'))['wall_s'] for _ in range(3)]
    assert max(walls) < 3.0, walls


def test_pandas_groupby_returns_table():
    out = ok(run('d = df("q1")\nresult = d.groupby("tool")["qty"].sum()'))
    assert out['result'] is None and out['result_table'] == dict(columns=['tool', 'qty'], rows=[['T0', 135], ['T1', 145], ['T2', 155]])


def test_numpy():
    out = ok(run('import numpy as np\na = np.array([r["qty"] for r in results["q1"]])\n'
                 'result = {"mean": a.mean(), "p90": float(np.percentile(a, 90)), "top": np.argsort(-a)[:2], "n": np.int64(a.size)}'))
    assert out['result'] == {'mean': 14.5, 'p90': 26.1, 'top': [29, 28], 'n': 30}


def test_scipy_fisher_exact():
    out = ok(run('from scipy.stats import fisher_exact\nodds, p = fisher_exact([[8, 2], [1, 5]])\nresult = {"odds": odds, "p": round(p, 6)}'))
    assert out['result'] == {'odds': 20.0, 'p': 0.034965}
    out = ok(run('from scipy import stats\nresult = stats.fisher_exact([[8, 2], [1, 5]], alternative="greater")'))
    assert set(out['result']) == {'statistic', 'pvalue'} and abs(out['result']['pvalue'] - 0.0244755) < 1e-6
    out = ok(run('import scipy.stats as st\nresult = st.fisher_exact([[5, 0], [0, 5]])[0]'))
    assert out['result'] == 'inf'           # JSON에 없는 값은 문자열로 돌려준다


def test_datetime_interval_clipping():
    code = ('from datetime import datetime\n'
            'fmt = "%Y-%m-%d %H:%M:%S"\n'
            'w0, w1 = datetime(2018, 3, 1, 5), datetime(2018, 3, 1, 12)\n'
            'total = 0.0\n'
            'for r in results["q1"]:\n'
            '    a, b = datetime.strptime(r["t_in"], fmt), datetime.strptime(r["t_out"], fmt)\n'
            '    lo, hi = max(a, w0), min(b, w1)\n'
            '    if hi > lo:\n'
            '        total += (hi - lo).total_seconds()\n'
            'result = total\n')
    fmt, w0, w1, want = '%Y-%m-%d %H:%M:%S', datetime(2018, 3, 1, 5), datetime(2018, 3, 1, 12), 0.0
    for r in ROWS:
        lo, hi = max(datetime.strptime(r[3], fmt), w0), min(datetime.strptime(r[4], fmt), w1)
        want += (hi - lo).total_seconds() if hi > lo else 0
    assert want > 0 and ok(run(code))['result'] == want
    out = ok(run('import pandas as pd\nd = df("q1")\n'
                 'a, b = pd.to_datetime(d["t_in"]), pd.to_datetime(d["t_out"])\n'
                 'lo, hi = a.clip(lower=pd.Timestamp("2018-03-01 05:00:00")), b.clip(upper=pd.Timestamp("2018-03-01 12:00:00"))\n'
                 'result = float((hi - lo).dt.total_seconds().clip(lower=0).sum())'))
    assert out['result'] == want


def test_dataframe_result_is_stored_under_new_handle():
    ctx = context()
    res = call(ctx, 'import pandas as pd\nd = df("q1")\nd["t_in"] = pd.to_datetime(d["t_in"])\n'
                    'print("rows", len(d))\nresult = d[d["qty"] >= 27][["lot", "qty", "t_in"]]')
    p = res.payload
    assert not res.is_error and p['ok'] and p['result_id'] == 'q2' and p['columns'] == ['lot', 'qty', 't_in'] and p['row_count'] == 3
    assert p['rows'] == [['L027', 27, '2018-03-01 00:00:00'], ['L028', 28, '2018-03-02 01:00:00'], ['L029', 29, '2018-03-03 02:00:00']]
    assert p['stdout'] == 'rows 30\n' and not p['truncated'] and 'result' not in p
    assert ctx.store.get('q2')['rows'] == p['rows'] and ctx.store.handles() == ['q1', 'q2']
    detail = ctx.tracer.of('tool_call')[0]['detail']
    assert detail['sandbox_violation'] is None and detail['wall_s'] > 0 and detail['limiter'] == ('job' if WIN else 'psutil')
    # 새 핸들을 다음 호출의 입력으로 쓴다. 긴 표는 미리보기만 보여 주고 전체를 보관한다
    res = call(ctx, 'result = [{"lot": r["lot"], "i": i} for r in results["q2"] for i in range(40)]', inputs=['q2'])
    p = res.payload
    assert p['result_id'] == 'q3' and p['row_count'] == 120 and len(p['rows']) == config.CAPS['preview_rows'] and p['truncated']
    assert len(ctx.store.get('q3')['rows']) == 120


def test_dict_result_and_print_through_handler():
    ctx = context()
    p = call(ctx, 'print("hello")\nprint([1, 2])\nresult = {"n": len(results["q1"]), "tools": sorted({r["tool"] for r in results["q1"]})}').payload
    assert p == dict(ok=True, stdout='hello\n[1, 2]\n', result={'n': 30, 'tools': ['T0', 'T1', 'T2']})
    p = call(ctx, 'x = 1').payload
    assert p['ok'] and p['result'] is None and 'result' in p['note']
    p = call(ctx, 'print("x" * 20000)').payload
    assert len(p['stdout']) < config.CAPS['preview_chars'] + 100 and '20,001자 중' in p['stdout']
    p = call(ctx, 'result = list(range(5000))').payload
    assert p['truncated'] and len(p['result']) <= config.CAPS['preview_chars'] + 1 and 'result_id' in p['note']
    assert ctx.store.handles() == ['q1'] and 'sandbox_violations' not in ctx.state


def test_result_conversion():
    out = ok(run('import numpy as np, pandas as pd\nfrom datetime import date, timedelta\nfrom decimal import Decimal\n'
                 'from collections import Counter, namedtuple\nP = namedtuple("P", "a b")\n'
                 'result = {"nan": float("nan"), "ninf": -float("inf"), "ts": pd.Timestamp("2018-03-01 10:00:00"), "nat": pd.NaT,\n'
                 '          "i": np.int64(3), "b": np.bool_(True), "set": {3, 1, 2}, "tup": (1, "a"), "dec": Decimal("1.5"),\n'
                 '          "d": date(2018, 1, 2), "td": timedelta(hours=1), "arr": np.arange(3), "cnt": Counter("aab"), 5: "int key",\n'
                 '          "nt": P(1, 2), "s": pd.Series([1, 2], index=["x", "y"]), "dt64": np.datetime64("2018-03-01T10:00:00")}'))
    assert out['result'] == {'nan': None, 'ninf': '-inf', 'ts': '2018-03-01 10:00:00', 'nat': None, 'i': 3, 'b': True, 'set': [1, 2, 3],
                             'tup': [1, 'a'], 'dec': 1.5, 'd': '2018-01-02', 'td': '1:00:00', 'arr': [0, 1, 2], 'cnt': {'a': 2, 'b': 1},
                             '5': 'int key', 'nt': {'a': 1, 'b': 2}, 's': {'x': 1, 'y': 2}, 'dt64': '2018-03-01 10:00:00'}
    out = ok(run('import pandas as pd\nd = df("q1").head(4)\nd.loc[1, "qty"] = None\n'
                 'result = d.groupby(["tool"]).agg(n=("lot", "count"), q=("qty", "mean"))'))
    assert out['result_table'] == dict(columns=['tool', 'n', 'q'], rows=[['T0', 2, 1.5], ['T1', 1, None], ['T2', 1, 2.0]])
    out = ok(run('result = [{"a": 1, "b": None}, {"b": 2.5, "a": 3}]'))
    assert out['result_table'] == dict(columns=['a', 'b'], rows=[[1, None], [3, 2.5]])
    assert ok(run('result = [{"a": 1}, {"b": 2}]'))['result'] == [{'a': 1}, {'b': 2}]       # 키가 다르면 표가 아니다
    assert ok(run('result = df("q1").iloc[0:0]'))['result_table'] == dict(columns=COLUMNS, rows=[])


PANDAS_OPS = '''
import pandas as pd
import numpy as np
d = df("q1")
d["t_in"] = pd.to_datetime(d["t_in"])
d["t_out"] = pd.to_datetime(d["t_out"], format="%Y-%m-%d %H:%M:%S")
d["dur"] = (d["t_out"] - d["t_in"]).dt.total_seconds()
g = d.groupby("tool").agg(n=("lot", "nunique"), s=("qty", "sum"), m=("dur", "mean")).reset_index()
m = d.merge(g, on="tool", how="left").sort_values(["tool", "qty"], ascending=[True, False])
p = d.pivot_table(index="tool", columns=d["qty"] % 2, values="qty", aggfunc="sum", fill_value=0)
c = pd.crosstab(d["tool"], d["qty"] > 10)
d["rk"] = d["qty"].rank(method="dense", ascending=False)
d["bin"] = pd.cut(d["qty"], 3)
d["q4"] = pd.qcut(d["qty"], 4, labels=False)
sub = d[d["qty"] > 3]
sub["x"] = 1                                     # 사본 대입 경고 경로(gc.get_referents)
sub = d[d["qty"] > 3].copy()
sub.loc[:, "x"] = 2
r = d.set_index("t_in").sort_index()["qty"].resample("1D").sum()
ro = d.set_index("t_in").sort_index()["qty"].rolling("6h").mean()
s = d["lot"].str.replace("L", "X").str.upper().str.contains("X0")
a = d.apply(lambda row: row["qty"] * 2, axis=1)
cc = pd.concat([d.head(3), d.tail(3)], ignore_index=True)
text = [repr(d), str(g), d.to_string(max_rows=5), d.head().to_csv(index=False), d.head().to_json(orient="records"), d.head().to_html()]
recs = d.head().to_dict("records")
d.info()
print(d.describe())
print(d.head(3))
misc = [d.isna().sum(), d["qty"].between(1, 5), d["qty"].astype(float), d.melt(id_vars=["lot"], value_vars=["qty", "dur"]),
        d["qty"].cumsum(), d["qty"].diff(), d["qty"].shift(1), d["t_in"].dt.floor("h"), d["t_in"].dt.strftime("%Y-%m-%d"),
        d.groupby("tool")["qty"].transform("mean"), d.groupby("tool").size(), d.nlargest(3, "qty"), d["qty"].quantile([0.1, 0.9]),
        d[["qty", "dur"]].corr(method="spearman"), d["qty"].corr(d["dur"]), d["tool"].value_counts(), d.drop_duplicates(["tool"]),
        d["tool"].map({"T0": "a"}), d["tool"].unique(), d.where(d["qty"] > 3), d.select_dtypes("number").sum(), d.nunique(),
        pd.to_numeric(pd.Series(["1", "x"]), errors="coerce"), pd.date_range("2018-01-01", periods=3, freq="D"),
        pd.Interval(1, 3).overlaps(pd.Interval(2, 5)), d.sample(3, random_state=0), d.T.shape, d["qty"].idxmax(), d.iloc[0],
        pd.merge_asof(d[["t_in", "qty"]].sort_values("t_in"), d[["t_in", "dur"]].sort_values("t_in"), on="t_in"),
        pd.Timestamp("2018-01-01 09:00", tz="Asia/Seoul").tz_convert("UTC"), d["qty"].isin([1, 2]), d.rename(columns={"qty": "n"}),
        d.assign(z=lambda t: t["qty"].clip(lower=3, upper=9)), d.fillna({"x": 0}), d.explode("lot"), list(d.itertuples())[:2]]
rank_corr = d["qty"].corr(d["dur"], method="spearman")   # pandas가 실행 중에 scipy를 가져온다
result = {"n": len(d), "g": g.shape[0], "sub": len(sub), "rho": round(float(rank_corr), 3)}
'''

NUMPY_OPS = '''
import numpy as np
a = np.array([[1.0, 2.0], [3.0, 4.0]])
x = [a.sum(), a.mean(axis=0), np.percentile(a, 50), np.linalg.inv(a), np.linalg.norm(a), a @ a, np.argsort(a, axis=None),
     np.unique(a, return_counts=True), np.random.default_rng(0).normal(size=5), np.where(a > 2, 1, 0), np.clip(a, 1, 3), np.cumsum(a),
     np.histogram(a.ravel(), bins=3), np.corrcoef(a), np.median(a), np.std(a, ddof=1), np.datetime64("2018-01-01") + np.timedelta64(1, "D"),
     np.intersect1d([1, 2], [2, 3]), np.setdiff1d([1, 2], [2]), repr(a), np.array2string(a), np.polyfit([1, 2, 3], [2, 4, 6], 1),
     np.searchsorted([1, 2, 3], 2), np.interp(1.5, [1, 2], [10, 20]), np.fft.fft([1, 2, 3, 4]).real, np.einsum("ij,ij->", a, a)]
print(a)
result = float(np.linalg.det(a))
'''

SCIPY_OPS = '''
import numpy as np
from scipy import stats as st
x = [st.fisher_exact([[8, 2], [1, 5]]), st.chi2_contingency([[10, 20], [30, 40]])[1], st.ttest_ind([1, 2, 3, 4], [2, 3, 4, 9]).pvalue,
     st.mannwhitneyu([1, 2, 3], [4, 5, 6]).pvalue, st.pearsonr([1, 2, 3, 4], [1, 2, 3, 5])[0], st.spearmanr([1, 2, 3], [3, 2, 1])[0],
     st.norm.cdf(1.96), st.norm.ppf(0.975), st.binomtest(3, 10, 0.5).pvalue, st.hypergeom.sf(2, 50, 5, 10), st.poisson.pmf(2, 3),
     st.kruskal([1, 2, 3], [4, 5, 6], [7, 8, 9]).pvalue, st.rankdata([3, 1, 2]), st.zscore([1, 2, 3]), st.linregress([1, 2, 3], [2, 4, 7]).slope,
     st.kendalltau([1, 2, 3], [1, 3, 2])[0], st.ks_2samp([1, 2, 3], [2, 3, 4]).pvalue, st.wilcoxon([1, 2, 3, 4], [2, 1, 5, 7]).pvalue,
     st.f_oneway([1, 2, 3], [2, 3, 4]).pvalue, st.contingency.odds_ratio([[8, 2], [1, 5]]).statistic, st.boschloo_exact([[8, 2], [1, 5]]).pvalue,
     st.barnard_exact([[8, 2], [1, 5]]).pvalue, st.false_discovery_control([0.01, 0.2, 0.03]), st.chisquare([10, 20, 30]).pvalue,
     st.bootstrap(([1, 2, 3, 4, 5],), np.mean, n_resamples=50, random_state=0).confidence_interval.low,
     st.permutation_test(([1, 2, 3], [4, 5, 6]), lambda a, b: np.mean(a) - np.mean(b), n_resamples=50, random_state=0).pvalue,
     st.gaussian_kde([1, 2, 3, 4])(2.0), st.expon.fit([1, 2, 3]), st.beta.ppf(0.5, 2, 3), st.t.interval(0.95, 10)]
result = round(float(st.fisher_exact([[8, 2], [1, 5]])[1]), 6)
'''

STDLIB_OPS = '''
import math, statistics, datetime, collections, itertools, functools, json, re, bisect, heapq, decimal, fractions
dt = datetime.datetime.strptime("2018-03-01 10:20:30", "%Y-%m-%d %H:%M:%S")
d2 = datetime.datetime.fromisoformat("2018-03-02 11:00:00")
P = collections.namedtuple("P", "a b")

class Span:
    def __init__(self, lo, hi):
        self._lo, self.hi = lo, hi

    def length(self):
        return self.hi - self._lo

class Wide(Span):
    def __init__(self):
        super().__init__(0, 10)

x = [(d2 - dt).total_seconds(), dt.strftime("%Y-%m-%d"), dt.isoformat(), datetime.date(2018, 1, 1).weekday(), dt + datetime.timedelta(hours=3),
     dt.replace(tzinfo=datetime.timezone.utc).timestamp(), statistics.mean([1, 2, 3]), statistics.median([1, 2, 3]), statistics.stdev([1, 2, 3]),
     statistics.quantiles([1, 2, 3, 4, 5], n=4), statistics.NormalDist(0, 1).cdf(1.0), P(1, 2)._asdict(), collections.Counter("abca").most_common(1),
     collections.defaultdict(list), collections.deque([1], maxlen=2), list(itertools.groupby(sorted("aabb"))), list(itertools.combinations([1, 2, 3], 2)),
     functools.reduce(lambda a, b: a + b, [1, 2]), functools.lru_cache(None)(lambda v: v * 2)(2), re.findall(r"\\d+", "a1b22"),
     re.compile(r"(?P<x>a+)").match("aab").group("x"), re.sub("a", "b", "aa"), bisect.bisect_left([1, 2, 3], 2), heapq.nlargest(2, [1, 5, 3]),
     decimal.Decimal("1.10") + decimal.Decimal("2.2"), fractions.Fraction(1, 3) + 1, math.comb(5, 2), json.dumps({"a": [1, 2]}, ensure_ascii=False),
     json.loads('{"a": 1}'), "한글".encode("utf-8"), "한글".encode("cp949").decode("cp949"), "abc".encode("ascii"), 2 ** 100, sorted({3, 1, 2}),
     Wide().length(), type(Wide()).__name__, f"{1.5:.2f}"]
try:
    {}["x"]
except KeyError as e:
    caught = repr(e)
if __name__ == "__main__":
    result = {"n": len(x), "name": type(Wide()).__name__, "caught": caught}
'''


@pytest.mark.parametrize('code, want', [
    (PANDAS_OPS, {'n': 30, 'g': 3, 'sub': 26, 'rho': -0.07}),
    (NUMPY_OPS, -2.0000000000000004),
    (SCIPY_OPS, 0.034965),
    (STDLIB_OPS, {'n': 37, 'name': 'Wide', 'caught': "KeyError('x')"}),
], ids=['pandas', 'numpy', 'scipy', 'stdlib'])
def test_common_operations_are_not_blocked(code, want):
    out = ok(run(code))
    if isinstance(want, dict) and 'rho' in want:
        want = dict(want, rho=out['result']['rho'])
    assert out['result'] == pytest.approx(want)


def test_missing_optional_dependency_is_an_error_not_a_violation():
    for code in ('print(df("q1").to_markdown())', 'df("q1").plot()', 'import pandas as pd\npd.array([1], dtype="int64[pyarrow]")'):
        out = run(code)
        assert not out['ok'] and out['error']['code'] == 'PYTHON_ERROR' and out['violation'] is None, show(out)
        if 'pyarrow' not in code:       # pyarrow가 없을 때 pandas는 ImportError가 아니라 NameError를 낸다
            assert 'ImportError' in out['error']['message'] or 'ModuleNotFoundError' in out['error']['message'], show(out)
            assert '선택 의존성' in out['error']['hint'], show(out)
    # 허용 라이브러리가 확인하는 선택 의존성은 미설치로 응답하고 기록만 한다
    assert failed(run('df("q1").plot()'), 'PYTHON_ERROR')['blocked_imports'] == ['email']


def test_pandas_operations_that_use_scipy():
    # 순위 상관과 interpolate는 정적 검사가 scipy를 미리 적재한다
    out = ok(run('d = df("q1")\nresult = round(float(d["qty"].corr(d["qty"] ** 2, method="spearman")), 6)'))
    assert out['result'] == 1.0 and out['blocked_imports'] == []
    out = ok(run('import pandas as pd\ns = pd.Series([1.0, None, 4.0, None, 16.0])\nresult = s.interpolate(method="pchip").round(3).tolist()'))
    assert out['result'][0] == 1.0 and None not in out['result']
    # 그 밖의 연산은 scipy가 없다고 응답하고, import scipy.stats를 넣으면 실행된다
    code = 'd = df("q1")\nresult = d["qty"].rolling(3, win_type="triang").mean().round(3).tolist()'
    out = failed(run(code), 'PYTHON_ERROR')
    assert 'scipy' in out['error']['message'] and 'import scipy.stats' in out['error']['hint'] and out['violation'] is None
    out = ok(run('import scipy.stats\n' + code))
    assert out['result'][:3] == [None, None, 1.0]


def test_spec():
    s = pysandbox.spec()
    assert s.name == 'run_python' and s.handler is not None
    assert s.input_schema == dict(type='object', additionalProperties=False, required=['code', 'inputs'], properties=dict(
        code=dict(type='string', description=s.input_schema['properties']['code']['description']),
        inputs=dict(type='array', items=dict(type='string'), description=s.input_schema['properties']['inputs']['description'])))
    for word in ('results["q3"]', 'df("q3")', 'inputs', 'result', 'print', 'scipy.stats', 'pandas', 'numpy', '파일', '네트워크',
                 f"{config.CAPS['python_timeout_s']}초"):
        assert word in s.description, word
    assert 3 <= s.description.count('. ') + 1 <= 4 and chr(0x2014) not in s.description      # 3~4문장, 엠대시 없음
    ctx = context()
    reg = Registry([s])
    bad = reg.call(ctx, ToolCall('c1', 'run_python', dict(code='result = 1')))
    assert bad.is_error and bad.payload['code'] == 'INVALID_ARGUMENTS'
    bad = reg.call(ctx, ToolCall('c2', 'run_python', dict(code='result = 1', inputs=['q1'], timeout=99)))
    assert bad.payload['code'] == 'INVALID_ARGUMENTS'
    unknown = reg.call(ctx, ToolCall('c3', 'run_python', dict(code='result = 1', inputs=['q9'])))
    assert unknown.is_error and unknown.payload['code'] == 'UNKNOWN_RESULT'


# ── 오류 ────────────────────────────────────────────────────────────────────────────────────────────

def test_exception_reports_type_message_and_line():
    out = failed(run('x = 1\ny = 2\nz = results["q1"][0]["nope"]\n'), 'PYTHON_ERROR')
    assert "KeyError: 'nope'" in out['error']['message'] and '3행' in out['error']['message'] and 'Traceback' not in out['error']['message']
    out = failed(run('def f(v):\n    return 1 / v\n\nprint("before")\nresult = f(0)\n'), 'PYTHON_ERROR')
    assert 'ZeroDivisionError' in out['error']['message'] and '2행' in out['error']['message'] and out['stdout'] == 'before\n'
    out = failed(run('import pandas as pd\nd = df("q1")\nd["nope"].sum()\n'), 'PYTHON_ERROR')       # 라이브러리 안에서 난 오류도 사용자 행으로 보고한다
    assert 'KeyError' in out['error']['message'] and '3행' in out['error']['message']
    out = failed(run('x = results["q7"]'), 'PYTHON_ERROR')
    assert 'q7' in out['error']['message'] and 'inputs' in out['error']['message']
    out = failed(run('x = (1,\n'), 'PYTHON_ERROR')
    assert 'SyntaxError' in out['error']['message'] and '1행' in out['error']['message'] and out['limiter'] is None


def test_infinite_loop_times_out():
    out = failed(run('n = 0\nwhile True:\n    n += 1\n', timeout_s=1.5), 'PYTHON_TIMEOUT')
    assert 1.5 <= out['wall_s'] < 6.0 and '1.5초' in out['error']['message']


def test_memory_limit():
    out = failed(run('x = [0] * (2 * 10 ** 9)', mem_mb=256), 'PYTHON_MEMORY')
    assert '256 MB' in out['error']['message'] and out['wall_s'] < 5
    out = failed(run('c = []\nwhile True:\n    c.append(bytearray(16 * 1024 * 1024))\n', mem_mb=256), 'PYTHON_MEMORY')
    out = failed(run('import numpy as np\nx = np.ones((40000, 40000))', mem_mb=256), 'PYTHON_MEMORY')
    # 같은 300 MB 할당이 상한 256 MB에서는 실패하고 512 MB에서는 된다. 상한이 설정한 값에서 지켜진다
    big = 'x = bytearray(300 * 1024 * 1024)\nresult = len(x)'
    failed(run(big, mem_mb=256), 'PYTHON_MEMORY')
    out = ok(run(big, mem_mb=512))
    assert out['result'] == 300 * 1024 * 1024 and out['limiter'] == ('job' if WIN else 'psutil')


@win_only
def test_job_object_limits_process_count(tmp_path):
    """감사 훅과 별개로 Job Object가 자식의 프로세스 생성을 막는다(활성 프로세스 1개)."""
    import subprocess
    cmd, env = pysandbox.child_command(str(tmp_path))
    body = ('import subprocess, sys\n'
            'try:\n    subprocess.run([sys.executable, "-c", "pass"])\n    print("spawned")\n'
            'except OSError as e:\n    print("blocked", e.winerror)\n')
    proc = subprocess.Popen([cmd[0], '-s', '-B', '-c', 'import sys; sys.stdin.readline(); exec(sys.stdin.read())'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(tmp_path))
    job = pysandbox._Job(256 * 1024 * 1024)
    try:
        job.assign(proc._handle)                    # 자식은 첫 줄을 받기 전에는 아무것도 실행하지 않는다
        out, err = proc.communicate(b'\n' + body.encode(), timeout=60)
        assert out.split()[0] == b'blocked', (out, err)
        assert 0 < job.peak_bytes() < 256 * 1024 * 1024
    finally:
        job.close()
        proc.wait(timeout=30)


def test_memory_limit_by_psutil_fallback():
    # Job Object를 쓰지 못할 때의 경로. 커밋 메모리를 8 MB씩 늘리고, 감시가 놓쳐도 800 MB에서 멈춘다
    code = 'c = []\nfor i in range(100):\n    c.append(bytearray(8 * 1024 * 1024))\n    s = sum(range(200000))\nresult = len(c)\n'
    out = failed(run(code, mem_mb=200, _limiter='psutil'), 'PYTHON_MEMORY')
    assert out['limiter'] == 'psutil'
    out = ok(run('result = 1', _limiter='psutil'))
    assert out['limiter'] == 'psutil'


def test_result_not_serializable_or_too_large():
    for code in ('result = object()', 'result = {"f": lambda: 1}', 'result = (i for i in range(3))', 'import math\nresult = math',
                 'result = b"bytes"', 'result = 1j'):
        out = failed(run(code), 'PYTHON_RESULT')
        assert 'result' in out['error']['message'], out
    out = failed(run('result = [{"a": i} for i in range(20001)]'), 'PYTHON_RESULT')
    assert '20,001행' in out['error']['message'] and '20,000행' in out['error']['message']
    out = failed(run('import pandas as pd\nresult = pd.DataFrame({"a": range(20001)})'), 'PYTHON_RESULT')
    out = failed(run('result = "x" * 3000000'), 'PYTHON_RESULT')
    assert 'MB' in out['error']['message']
    assert len(ok(run('result = [{"a": i} for i in range(20000)]'))['result_table']['rows']) == 20000


def test_handler_raises_tool_errors_and_records_violations():
    ctx = context()
    res = call(ctx, 'result = 1 / 0')
    assert res.is_error and res.payload['code'] == 'PYTHON_ERROR' and set(res.payload) == {'ok', 'code', 'message', 'hint'}
    res = call(ctx, 'result = object()')
    assert res.payload['code'] == 'PYTHON_RESULT' and 'sandbox_violations' not in ctx.state
    res = call(ctx, 'import os\nresult = os.listdir(".")')
    assert res.is_error and res.payload['code'] == 'PYTHON_FORBIDDEN' and 'import os' in res.payload['message']
    assert 'scipy.stats' in res.payload['hint'] and 'results' in res.payload['hint']
    res = call(ctx, 'result = df("q1").apply("to_csv", path_or_buf="leak.csv")')       # 정적 검사를 통과하고 실행 중에 차단된다
    assert res.payload['code'] == 'PYTHON_FORBIDDEN' and 'open' in res.payload['message']
    assert len(ctx.state['sandbox_violations']) == 2 and ctx.state['sandbox_violations'][0].startswith('static: import os')
    assert 'leak.csv' in ctx.state['sandbox_violations'][1] and 'leak.csv' not in res.content
    assert 'internal_error' not in ctx.state and ctx.store.handles() == ['q1']
    events = ctx.tracer.of('tool_call')
    assert [e['code'] for e in events] == ['PYTHON_ERROR', 'PYTHON_RESULT', 'PYTHON_FORBIDDEN', 'PYTHON_FORBIDDEN']


def test_sandbox_failure_is_internal_error(monkeypatch):
    monkeypatch.setattr(pysandbox, 'CHILD', os.path.join(os.path.dirname(pysandbox.CHILD), 'no_such_child.py'))
    out = failed(run('result = 1'), 'SANDBOX_FAILURE')
    ctx = context()
    res = call(ctx, 'result = 1')
    assert res.payload['code'] == 'INTERNAL' and 'internal_error' in ctx.state


# ── 탈출 시도 ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def secret(tmp_path):
    """저장소의 정답 파일 역할을 하는 표식 파일. 어떤 시도에서도 내용이 결과에 나오면 안 된다."""
    text = 'MARKER-' + uuid.uuid4().hex
    path = tmp_path / 'answer_key.json'
    path.write_text(text, encoding='utf-8')
    return dict(text=text, path=str(path), target=str(tmp_path / 'written.out'), dir=str(tmp_path))


def attempt(secret, code, **kw):
    out = run(code.replace('{p}', repr(secret['path'])).replace('{w}', repr(secret['target'])).replace('{d}', repr(secret['dir'])), **kw)
    assert secret['text'] not in json.dumps(out, ensure_ascii=False, default=str), out
    assert not os.path.exists(secret['target']) and open(secret['path'], encoding='utf-8').read() == secret['text']
    assert os.listdir(secret['dir']) == ['answer_key.json']
    failed(out, 'PYTHON_FORBIDDEN')
    assert out['violation'], out
    return out


STATIC_ESCAPES = {
    'open': 'result = open({p}).read()',
    'open via builtins': 'import builtins\nresult = builtins.open({p}).read()',
    '__import__ os': 'result = __import__("os").listdir({d})',
    'import os': 'import os\nresult = os.listdir({d})',
    'import subprocess': 'import subprocess\nresult = subprocess.run(["cmd", "/c", "type", {p}], capture_output=True).stdout',
    'import socket': 'import socket\ns = socket.socket()',
    'import ctypes': 'import ctypes\nresult = ctypes.windll.kernel32.GetCurrentProcessId()',
    'import sqlite3': 'import sqlite3\nresult = str(sqlite3.connect({p}))',
    'pd.read_csv': 'import pandas as pd\nresult = pd.read_csv({p})',
    'pd.read_json': 'import pandas as pd\nresult = pd.read_json({p})',
    'np.load': 'import numpy as np\nresult = np.load({p})',
    'np.fromfile': 'import numpy as np\nresult = np.fromfile({p}, dtype="uint8").tobytes().decode()',
    'df.to_csv path': 'df("q1").to_csv({w})',
    'df.to_csv keyword path': 'df("q1").to_csv(path_or_buf={w})',
    'df.to_csv kwargs': 'df("q1").to_csv(**{"path_or_buf": {w}})',
    'df.to_csv reference': 'f = df("q1").to_csv\nf({w})',
    'df.to_pickle': 'df("q1").to_pickle({w})',
    'df.to_json path': 'df("q1").to_json({w})',
    'df.to_string buf': 'df("q1").to_string(buf={w})',
    'pd.io.common': 'import pandas as pd\nresult = pd.io.common.get_handle({p}, "r").handle.read()',
    'subclasses': 'result = [c for c in ().__class__.__bases__[0].__subclasses__()]',
    'getattr': 'import pandas as pd\nresult = getattr(pd, "read_csv")({p})',
    'pd.__dict__': 'import pandas as pd\nresult = pd.__dict__["read_csv"]({p})',
    'eval': 'result = eval("open")({p}).read()',
    'exec': 'exec("import os")',
    'compile': 'c = compile("1 + 1", "x", "eval")',
    'importlib': 'import importlib\nresult = importlib.import_module("os").listdir({d})',
    'pathlib': 'import pathlib\nresult = pathlib.Path({p}).read_text()',
    'json.load open': 'import json\nresult = json.load(open({p}))',
    'json.load': 'import json\nresult = json.load({p})',
    'np.genfromtxt': 'import numpy as np\nresult = np.genfromtxt({p}, dtype=str)',
    'np.loadtxt': 'import numpy as np\nresult = np.loadtxt({p}, dtype=str)',
    'np.memmap': 'import numpy as np\nresult = np.memmap({p}, mode="r")',
    'np.save': 'import numpy as np\nnp.save({w}, np.arange(3))',
    'np.savez': 'import numpy as np\nnp.savez_compressed({w}, a=np.arange(3))',
    'ndarray.tofile': 'import numpy as np\nnp.arange(3).tofile({w})',
    'DataFrame.eval': 'import pandas as pd\nresult = pd.DataFrame.eval(df("q1"), "qty + 1")',
    'df.query': 'result = df("q1").query("qty > 1")',
    'pd.eval': 'import pandas as pd\nresult = pd.eval("1 + 1")',
    'os.environ': 'import os\nresult = dict(os.environ)',
    'os.environ via pandas': 'import pandas as pd\nresult = dict(pd.io.common.os.environ)',
    'sys.modules': 'import sys\nresult = sys.modules["os"].listdir({d})',
    'sys via collections': 'import collections\nresult = collections._sys.modules["os"].listdir({d})',
    'from os import': 'from os import listdir\nresult = listdir({d})',
    'from pandas import read_csv': 'from pandas import read_csv\nresult = read_csv({p})',
    'from numpy import star': 'from numpy import *\nresult = fromfile({p}, dtype="uint8")',
    'submodule import': 'import numpy.lib.npyio as n\nresult = n.loadtxt({p}, dtype=str)',
    'scipy.io': 'from scipy import io\nresult = io.loadmat({p})',
    'relative import': 'from . import os',
    'breakpoint': 'breakpoint()',
    'input': 'x = input()',
    'globals': 'result = list(globals())',
    'vars': 'import pandas as pd\nresult = vars(pd)["read_csv"]({p})',
    '__builtins__': 'result = __builtins__["open"]({p}).read()',
    'frame walk': 'def g():\n    yield\nresult = g().gi_frame.f_globals',
    'match class attribute': 'match ():\n    case object(__class__=c):\n        result = str(c)',
    'f-string': 'name = {p}\nresult = f"{open(name).read()}"',
    'lambda default': 'f = lambda path, reader=open: reader(path).read()\nresult = f({p})',
}


@pytest.mark.parametrize('name', list(STATIC_ESCAPES))
def test_static_check_blocks(name, secret):
    out = attempt(secret, STATIC_ESCAPES[name])
    assert out['violation'].startswith('static: ') and out['limiter'] is None and out['wall_s'] < 1      # 프로세스를 만들지 않고 반려한다
    assert '행)' in out['error']['message'] and 'scipy.stats' in out['error']['hint']


def test_static_messages_name_construct_and_line():
    out = failed(run('import numpy as np\nx = 1\nd = np.load("a.npy")\nimport os\n'), 'PYTHON_FORBIDDEN')
    assert out['error']['message'] == '허용되지 않는 코드다: .load(3행), import os(4행)'
    assert out['violation'] == 'static: .load (line 3); import os (line 4)'
    out = failed(run('d = df("q1").query("qty > 1")'), 'PYTHON_FORBIDDEN')
    assert '.query(1행)' in out['error']['message'] and 'df[df["열"] > 값]' in out['error']['hint']
    for fine in ('import re\np = re.compile("a")\nresult = bool(p.match("a"))', 'result = df("q1").head(2).to_csv(index=False)',
                 'result = df("q1").to_json(orient="records")[:5]', 'x = [3, 1]\nx.remove(3)\nresult = "a-b".replace("-", "")',
                 'result = df("q1").rename(columns={"qty": "n"}).columns.tolist()'):
        ok(run(fine))


PRE = 'import collections\nM = collections._sys.modules\nB = M["builtins"]\nos = M["os"]\nload = M["_frozen_importlib"]._gcd_import\n'
WALK = ('def walk(c, seen):\n    for s in type.__subclasses__(c):\n        if s not in seen:\n            seen.add(s)\n            yield s\n'
        '            yield from walk(s, seen)\n')
RUNTIME_ESCAPES = {
    'open': 'result = open({p}).read()',
    'real open from builtins module': PRE + 'result = B.open({p}).read()',
    'open for write': PRE + 'B.open({w}, "w").write("x")',
    '__import__ os': 'result = __import__("os").listdir({d})',
    'import os': 'import os\nresult = os.listdir({d})',
    'import subprocess': 'import subprocess\nresult = subprocess.run(["cmd", "/c", "type", {p}], capture_output=True).stdout',
    'import socket': 'import socket\ns = socket.socket()',
    'import ctypes': 'import ctypes\nresult = ctypes.windll.kernel32.GetCurrentProcessId()',
    'import sqlite3': 'import sqlite3\nresult = str(sqlite3.connect({p}))',
    'import importlib': 'import importlib\nresult = importlib.import_module("os").listdir({d})',
    'import pathlib': 'import pathlib\nresult = pathlib.Path({p}).read_text()',
    'import builtins': 'import builtins\nresult = builtins.open({p}).read()',
    'import sys': 'import sys\nresult = sys.modules["os"].listdir({d})',
    'import io': 'import io\nresult = io.open({p}).read()',
    'import numpy submodule': 'import numpy.lib.npyio as n\nresult = n.loadtxt({p}, dtype=str)',
    'from scipy import io': 'from scipy import io\nresult = io.loadmat({p})',
    'real __import__ socket': PRE + 's = B.__import__("socket").socket()',
    'real __import__ sqlite3': PRE + 'result = str(B.__import__("sqlite3").connect({p}))',
    'real __import__ urllib.request': PRE + 'u = B.__import__("urllib.request")\nresult = u.request.urlopen("http://127.0.0.1:9/").read()',
    'real __import__ http.client': PRE + 'h = B.__import__("http.client")\nresult = h.client.HTTPConnection("127.0.0.1", 9).connect()',
    'real __import__ webbrowser': PRE + 'B.__import__("webbrowser").open("http://127.0.0.1:9/")',
    'real __import__ asyncio': PRE + 'B.__import__("asyncio")',
    'import_module sqlite3': PRE + 'result = str(load("sqlite3").connect({p}))',
    'import_module pathlib': PRE + 'result = load("pathlib").Path({p}).read_text()',
    'import_module shutil': PRE + 'load("shutil").copyfile({p}, {w})',
    'import_module glob': PRE + 'result = load("glob").glob({d} + "/*")',
    'import_module tempfile': PRE + 'result = load("tempfile").mkstemp(dir={d})',
    'import_module pickle': PRE + 'result = load("pickle").loads(b"cos\\nsystem\\n(S\'echo hi\'\\ntR.")',
    'import_module gc': PRE + 'result = len(load("gc").get_objects())',
    'import_module mmap': PRE + 'result = bytes(load("mmap").mmap(-1, 16))',
    'os.listdir': PRE + 'result = os.listdir({d})',
    'os.scandir': PRE + 'result = [e.name for e in os.scandir({d})]',
    'os.walk': PRE + 'result = list(os.walk({d}))',
    'os.open': PRE + 'fd = os.open({p}, os.O_RDONLY)\nresult = os.read(fd, 100).decode()',
    'os.open create': PRE + 'fd = os.open({w}, os.O_WRONLY | os.O_CREAT)',
    'os.remove': PRE + 'os.remove({p})',
    'os.rename': PRE + 'os.rename({p}, {w})',
    'os.mkdir': PRE + 'os.mkdir({w})',
    'os.chdir': PRE + 'os.chdir({d})',
    'os.system': PRE + 'result = os.system("echo hi")',
    'os.popen': PRE + 'result = os.popen("type " + {p}).read()',
    'os.spawnv': PRE + 'result = os.spawnv(os.P_WAIT, M["sys"].executable, ["python", "-c", "pass"])',
    'os.kill': PRE + 'os.kill(os.getpid(), 9)',
    'io.open': PRE + 'result = M["io"].open({p}).read()',
    'io.FileIO': PRE + 'result = M["io"].FileIO({p}).read().decode()',
    'io.open_code': PRE + 'result = M["io"].open_code({p}).read().decode()',
    'subclasses to FileIO': WALK + 'cls = [c for c in walk(().__class__.__bases__[0], set()) if c.__name__ == "FileIO"][0]\n'
                                   'result = cls({p}).read().decode()',
    'frame walk to real builtins': 'import collections\nf = collections._sys._getframe()\nwhile f.f_back is not None:\n    f = f.f_back\n'
                                   'result = f.f_globals["__builtins__"].open({p}).read()',
    'json.codecs.open': 'import json\nresult = json.codecs.open({p}).read()',
    'json.load open': 'import json\nresult = json.load(open({p}))',
    'getattr': 'import pandas as pd\nresult = getattr(pd, "read_csv")({p})',
    'eval': 'result = eval("1 + 1")',
    'exec': 'exec("x = 1")',
    'compile': 'c = compile("1 + 1", "x", "eval")',
    'globals': 'result = list(globals())',
    'input': 'x = input()',
    'sys.addaudithook': PRE + 'M["sys"].addaudithook(lambda e, a: None)',
    'sys.settrace': PRE + 'M["sys"].settrace(lambda *a: None)',
    'code replace': 'f = lambda: 0\nc = f.__code__.replace(co_consts=(1,))',
    'set __code__': 'def f():\n    return 1\ndef g():\n    return 2\nf.__code__ = g.__code__\nresult = f()',
    'pd.read_csv': 'import pandas as pd\nresult = pd.read_csv({p}, header=None).to_dict()',
    'pd.read_json': 'import pandas as pd\nresult = pd.read_json({p}).to_dict()',
    'pd.read_table': 'import pandas as pd\nresult = pd.read_table({p}, header=None).to_dict()',
    'pd.read_pickle': 'import pandas as pd\nresult = pd.read_pickle({p})',
    'pd.__dict__': 'import pandas as pd\nresult = pd.__dict__["read_csv"]({p}, header=None).to_dict()',
    'pd.io.common.get_handle': 'import pandas as pd\nresult = pd.io.common.get_handle({p}, "r").handle.read()',
    'df.to_csv path': 'df("q1").to_csv({w})',
    'df.to_pickle': 'df("q1").to_pickle({w})',
    'df.to_json path': 'df("q1").to_json({w})',
    'df.to_string buf': 'df("q1").to_string(buf={w})',
    'df.apply to_csv': 'df("q1").apply("to_csv", path_or_buf={w})',
    'np.load': 'import numpy as np\nresult = np.load({p}, allow_pickle=True)',
    'np.fromfile': 'import numpy as np\nresult = np.fromfile({p}, dtype="uint8").tobytes().decode()',
    'np.genfromtxt': 'import numpy as np\nresult = np.genfromtxt({p}, dtype=str).tolist()',
    'np.loadtxt': 'import numpy as np\nresult = np.loadtxt({p}, dtype=str).tolist()',
    'np.memmap': 'import numpy as np\nresult = bytes(np.memmap({p}, mode="r")).decode()',
    'np.save': 'import numpy as np\nnp.save({w}, np.arange(3))',
    'ndarray.tofile': 'import numpy as np\nnp.arange(3).tofile({w})',
    'socket via pandas': 'import pandas as pd\nfrom scipy import stats\ns = pd.io.common.os.sys.modules["socket"].socket()',
    'subprocess via pandas': 'import pandas as pd\nS = pd.io.common.os.sys.modules["subprocess"]\n'
                             'result = S.run(["cmd", "/c", "type", {p}], capture_output=True).stdout.decode()',
    'ctypes via numpy': 'import numpy as np\nresult = np.ctypeslib.ctypes.CDLL("kernel32").GetCurrentProcessId()',
    'os.startfile': pytest.param(PRE + 'os.startfile({p})', marks=win_only),
    'winreg': pytest.param(PRE + 'w = load("winreg")\nresult = str(w.OpenKey(w.HKEY_CURRENT_USER, "Environment"))', marks=win_only),
    '_winapi.CreateFile': pytest.param(PRE + 'w = load("_winapi")\nh = w.CreateFile({p}, 0x80000000, 1, 0, 3, 0, 0)\n'
                                             'result = w.ReadFile(h, 100)[0].decode()', marks=win_only),
    '_winapi.CreateProcess': pytest.param(PRE + 'w = load("_winapi")\nresult = str(w.CreateProcess(None, "cmd /c echo hi", None, None, 0, 0, None, None, None))',
                                          marks=win_only),
    'msvcrt': pytest.param(PRE + 'result = load("msvcrt").get_osfhandle(0)', marks=win_only),
    'subinterpreter': PRE + 'result = str(load("_xxsubinterpreters").create())',
}


@pytest.mark.parametrize('code', [v if not isinstance(v, str) else pytest.param(v) for v in RUNTIME_ESCAPES.values()], ids=list(RUNTIME_ESCAPES))
def test_runtime_layer_blocks(code, secret):
    """정적 검사를 건너뛰어도(_static=False) 제한한 builtins와 감사 훅이 차단하고 보고한다."""
    out = attempt(secret, code, _static=False)
    assert not out['violation'].startswith('static') and out['limiter'] is not None


def test_static_check_would_also_block_every_runtime_case(secret):
    passing = []
    for name, code in RUNTIME_ESCAPES.items():
        code = code if isinstance(code, str) else code.values[0]
        if not pysandbox.check(code.replace('{p}', repr(secret['path'])).replace('{w}', repr(secret['target'])).replace('{d}', repr(secret['dir'])))[1]:
            passing.append(name)
    assert passing == ['df.apply to_csv'], passing      # 문자열로 메서드를 고르는 경로만 정적 검사를 통과한다. 실행 계층이 막는다


# ── 자식 환경 ───────────────────────────────────────────────────────────────────────────────────────

PROBE = ('import collections\nsys = collections._sys\nos = sys.modules["os"]\n'
         'result = dict(env=sorted(os.environ), ppid=os.getppid(), cwd=os.getcwd(), path=sys.path, prefix=sys.prefix, tmp=os.environ.get("TEMP"),\n'
         '              no_user_site=sys.flags.no_user_site, no_bytecode=sys.flags.dont_write_bytecode, randomized=sys.flags.hash_randomization,\n'
         '              h=hash("abc"), order=list({"a", "b", "c", "d", "e", "f"}), pyarrow=sys.modules.get("pyarrow", "absent") is None,\n'
         '              argv0=sys.argv[0], blas=os.environ.get("OPENBLAS_NUM_THREADS"))')


def test_child_environment_has_no_secrets(monkeypatch, tmp_path):
    for name in ('ANTHROPIC_API_KEY', 'GATEWAY_TOKEN', 'DB_SECRET', 'PYTHONPATH', 'PYTHONSTARTUP'):
        monkeypatch.setenv(name, 'sk-should-not-leak' if 'PYTHON' not in name else str(tmp_path))
    cmd, env = pysandbox.child_command(str(tmp_path))
    assert not [k for k in env if any(w in k.upper() for w in ('KEY', 'TOKEN', 'SECRET'))]
    assert set(env) <= {'PYTHONHASHSEED', 'SYSTEMROOT', 'TEMP', 'TMP', 'TMPDIR', 'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                        '__PYVENV_LAUNCHER__'}
    assert cmd[1:] == ['-s', '-B', pysandbox.CHILD] and env['PYTHONHASHSEED'] == '0'
    a, b = ok(run(PROBE, _static=False))['result'], ok(run(PROBE, _static=False))['result']
    assert not [k for k in a['env'] if any(w in k.upper() for w in ('KEY', 'TOKEN', 'SECRET', 'PYTHONPATH', 'PYTHONSTARTUP'))], a['env']
    assert 'sk-should-not-leak' not in json.dumps(a)
    assert a['ppid'] == os.getpid()                                         # 런처를 거치지 않는다(프로세스 1개)
    assert a['prefix'] == sys.prefix                                        # 부모와 같은 venv
    assert a['no_user_site'] == 1 and a['no_bytecode'] == 1 and a['randomized'] == 0 and a['blas'] == '1'
    assert (a['h'], a['order']) == (b['h'], b['order'])                     # PYTHONHASHSEED=0: 집합 순서가 실행마다 같다
    root, tools = os.path.normcase(config.ROOT), os.path.normcase(os.path.dirname(pysandbox.CHILD))
    assert all(os.path.normcase(os.path.abspath(p)) not in (root, tools) for p in a['path']) and '' not in a['path']
    assert a['cwd'] != b['cwd'] and os.path.basename(a['cwd']).startswith('pysbx_')
    assert os.path.basename(a['tmp']) == os.path.basename(a['cwd'])         # TEMP도 같은 임시 디렉터리다
    assert not os.path.exists(a['cwd']) and not os.path.exists(b['cwd'])    # 임시 디렉터리는 실행 뒤에 지운다
    assert os.path.normcase(a['argv0']) == os.path.normcase(pysandbox.CHILD)


def test_pyarrow_is_disabled_and_stray_output_cannot_forge_a_response():
    assert ok(run('import pandas as pd\n' + PROBE, _static=False))['result']['pyarrow'] is True
    # 사용자 코드가 실제 stdout에 쓴 줄은 응답으로 읽히지 않는다(표식에 요청마다 다른 난수가 들어간다)
    out = ok(run('import collections\nraw = collections._sys.__stdout__\n'
                 'raw.write("\\n@@pysandbox:0000 RESULT {\\"ok\\": true, \\"result\\": \\"forged\\"}\\n")\nraw.flush()\nresult = "real"', _static=False))
    assert out['result'] == 'real'
