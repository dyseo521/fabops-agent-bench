"""PySCFabSim을 로깅 플러그인과 함께 실행해 팹 이벤트 로그를 만든다.

예) python scripts/run_fab_sim.py --dataset SMT2020_LVHM_E --days 65 --log-from-day 60 --history-from-day 0 --out data/derived/simlog/lvhm_e_cr_s0

본 로그 창은 60~65일이다. 초기 WIP가 60일 무렵 소진되므로 이 창에서 완료되는 lot은 전부 시뮬레이션 안에서 투입된 lot이고,
--history-from-day 0으로 남긴 이력과 합치면 완료 lot의 전 공정 이력이 된다(wafer sort 기반 commonality용).
"""
import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(ROOT, 'data', 'PySCFabSim-release')
sys.path.insert(0, SIM)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simulation.dispatching.dispatcher import dispatcher_map  # noqa: E402
from simulation.file_instance import FileInstance  # noqa: E402
from simulation.greedy import get_lots_to_dispatch_by_machine  # noqa: E402
from simulation.randomizer import Randomizer  # noqa: E402
from simulation.read import read_all  # noqa: E402
from fab_event_logger import FabEventLogger  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dataset', default='SMT2020_LVHM_E')
    p.add_argument('--days', type=float, default=65, help='총 시뮬레이션 일수 (warm-up 포함)')
    p.add_argument('--log-from-day', type=float, default=60, help='이 시점부터 기록')
    p.add_argument('--history-from-day', type=float, default=0,
                   help='이 시점부터 기록 시작 전까지 끝난 step을 lot_step_history.csv로 남긴다. 음수면 끔')
    p.add_argument('--dispatcher', default='cr', choices=list(dispatcher_map.keys()),
                   help='테스트베드 정의 순위(우선순위, 셋업, CR). fifo는 민감도 확인용')
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--out', required=True)
    p.add_argument('--upstream-downtime', action='store_true',
                   help='유휴 장비 다운을 막지 않는 원본 동작을 그대로 쓴다')
    a = p.parse_args()

    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    files = read_all(os.path.join(SIM, 'datasets', a.dataset))
    run_to = 86400 * a.days
    Randomizer().random.seed(a.seed)
    hist = None if a.history_from_day < 0 else a.history_from_day
    logger = FabEventLogger(out, a.log_from_day, history_from_day=hist, meta=dict(
        dataset=a.dataset, days=a.days, log_from_day=a.log_from_day, history_from_day=hist,
        dispatcher=a.dispatcher, seed=a.seed, strict_downtime=not a.upstream_downtime,
        generated_at=datetime.now().isoformat(timespec='seconds')))
    instance = FileInstance(files, run_to, True, [logger])
    instance.strict_downtime = not a.upstream_downtime
    dispatcher = dispatcher_map[a.dispatcher]

    t0 = datetime.now()
    while not instance.done:
        done = instance.next_decision_point()
        instance.print_progress_in_days()
        if done or instance.current_time > run_to:
            break
        machine, lots = get_lots_to_dispatch_by_machine(instance, dispatcher)
        if lots is None:
            instance.usable_machines.remove(machine)
        else:
            instance.dispatch(machine, lots)
    instance.finalize()
    sys.stderr.write(f'\n{instance.current_time_days:.2f}일 시뮬레이션, 소요 {datetime.now() - t0}\n')
    for k, v in sorted(logger.counts.items()):
        print(f'{k:28s} {v:>9,d}')


if __name__ == '__main__':
    main()
