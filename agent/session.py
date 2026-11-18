"""조건별 도구 조립. 과제 1건 x 조건 1개의 도구 목록(Registry)과 시행 상태(Context)를 만든다.

- 조건 간에 달라지는 것은 도구 목록과 검증·게이트 적용 여부뿐이다(config.CONDITIONS)
- 자료는 과제의 기준 시각 스냅샷(기준 시각으로 자른 사본)을 연다(tasks.HarnessView.data_name). 기준 시각이 없으면 원본이다
- 게이트는 원본 KG에서 실행한다. 조치 과제는 전 조건에서 게이트를 실행하고(기록용), 게이트 조건에서만 반려한다
- 자료 접근 객체는 (자료 이름, 조건)이 같으면 다시 쓴다. 한 번에 하나만 열어 둔다(메모리)
"""
import os
import sys

from . import config
from .tools import actions, kg_tools, pysandbox, sql_tools
from .tools.registry import Context, Registry


def specs(condition, ablation='none'):
    """조건의 도구 정의. 순서는 config.tool_names와 같다. ablation: 제거 실험의 변형 이름(주 실험은 'none')"""
    c = config.CONDITIONS[condition]
    by_name = {s.name: s for s in (sql_tools.specs() if c['access'] == 'sql' else kg_tools.specs(c['verify']))}
    by_name['run_python'] = pysandbox.spec()
    by_name['submit_answer'] = actions.spec()
    return [by_name[n] for n in config.tool_names(condition, ablation)]


class Sessions:
    """자료 접근 객체와 게이트의 보관소. 러너 하나가 하나를 쓴다."""

    def __init__(self, use_gate=True, ablation='none'):
        self.use_gate, self.ablation = use_gate, ablation
        self._key = self._access = None
        self._gates = {}
        self._registries = {}

    def registry(self, condition):
        if condition not in self._registries:
            self._registries[condition] = Registry(specs(condition, self.ablation))
        return self._registries[condition]

    def _open(self, name, condition):
        c = config.CONDITIONS[condition]
        key = (name, c['access'], c['db'], c['verify'])
        if key != self._key:
            self.close_access()
            if c['access'] == 'sql':
                path = config.db_path(name, c['db'])
                if not os.path.exists(path):
                    raise FileNotFoundError(f'DB가 없다: {path}')
                self._access = ('sql', sql_tools.SqlAccess(path))
            else:
                path = config.kg_dir(name)
                if not os.path.exists(os.path.join(path, 'store')):
                    raise FileNotFoundError(f'KG가 없다: {path}')
                self._access = ('kg', kg_tools.KgAccess(path, verify=c['verify']))
            self._key = key
        return self._access

    def gate(self, scenario):
        if scenario not in self._gates:
            sys.path.insert(0, config.SCRIPTS)
            from gate import Gate
            self._gates[scenario] = Gate(config.kg_dir(scenario))
        return self._gates[scenario]

    def context(self, task, condition):
        """-> (Registry, Context)"""
        kind, access = self._open(task.data_name, condition)
        data = {kind: access}
        if task.layer == 'action' and self.use_gate:
            data['gate'] = self.gate(task.scenario)
            data['gate_blocks'] = config.CONDITIONS[condition]['gate']
        return self.registry(condition), Context(task=task, condition=condition, data=data)

    def close_access(self):
        if self._access is not None:
            self._access[1].close()
        self._key = self._access = None

    def close(self):
        self.close_access()
        self._gates.clear()
