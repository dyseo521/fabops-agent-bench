"""PySCFabSim 로깅 플러그인. MES 형태의 이벤트 로그를 CSV로 남긴다.

시각은 전부 시뮬레이션 시작 기준 초(second)다. 절대 시각 변환은 ETL에서 한다.
log_from 이전 구간은 warm-up으로 보고 기록하지 않는다. log_from 시점의 상태는 스냅샷으로 남긴다.

출력 파일
  tools.csv                 장비 마스터 (시뮬레이터 기준)
  lots.csv                  기록 구간에 존재한 lot
  lot_step_event.csv        lot의 step 처리 1건 = 1행 (track-in ~ 다음 step 도착)
  lot_step_skipped.csv      sampling으로 건너뛴 step
  lot_rework.csv            rework 발생
  lot_dedication.csv        LTL dedication 설정 기록
  tool_down.csv             장비 정지 구간 (BREAKDOWN, PM_TIME, PM_COUNTER)
  snapshot_wip.csv          log_from 시점의 lot 상태
  snapshot_dedication.csv   log_from 시점에 유효한 dedication
  snapshot_tool.csv         log_from 시점의 장비 상태
  snapshot_cqt.csv          log_from 시점에 CQT 구간 안에 있는 lot의 진입 step 종료 시각
  lot_step_history.csv      (옵션) history_from ~ log_from 사이에 끝난 step. 완료 lot의 공정 이력(YMS)용
  run_meta.json             실행 파라미터
"""
import csv
import json
import os

from simulation.plugins.interface import IPlugin

DAY = 86400


def lot_type(name, priority):
    eng = name.startswith('E_')
    if 'SuperHot' in name or priority >= 30:
        kind = 'SUPERHOT'
    elif 'Hot' in name or priority >= 20:
        kind = 'HOT'
    else:
        kind = 'REGULAR'
    return ('ENG_' if eng else '') + kind


class FabEventLogger(IPlugin):

    def __init__(self, out_dir, log_from_day=0.0, meta=None, history_from_day=None):
        self.out_dir = out_dir
        self.log_from = log_from_day * DAY
        self.history_from = None if history_from_day is None else history_from_day * DAY
        self.meta = dict(meta or {})
        os.makedirs(out_dir, exist_ok=True)
        self.files, self.writers = {}, {}
        self.open_steps = {}      # lot.idx -> 진행 중인 step 행
        self.lots = {}            # lot.idx -> lot 행
        self.tool_seq = {}        # machine.idx -> toolgroup 내 순번(1부터)
        self.pm_seen = {}         # machine.idx -> 마지막으로 본 pmed_time
        self.setup_seen = {}      # machine.idx -> 마지막으로 본 setuped_time
        self.transport_seen = {}  # lot.idx -> 마지막으로 본 transport_time
        self.down_until = {}      # machine.idx -> 정지 종료 시각
        self.cqt_open = {}        # lot.idx -> {종료 step: (진입 step, 진입 step track-out, CQT 초)}
        self.busy_until = {}      # machine.idx -> 작업 종료 예정 시각
        self.dispatch_id = 0
        self.event_id = 0
        self.snapshot_done = False
        self.counts = {}

    # ---- 출력 -----------------------------------------------------------
    def _w(self, name, header):
        if name not in self.writers:
            f = open(os.path.join(self.out_dir, name), 'w', newline='', encoding='utf-8')
            self.files[name] = f
            self.writers[name] = csv.writer(f)
            self.writers[name].writerow(header)
        return self.writers[name]

    def _write(self, name, header, row):
        self._w(name, header).writerow(row)
        self.counts[name] = self.counts.get(name, 0) + 1

    @staticmethod
    def _t(x):
        return '' if x is None else round(x, 3)

    def lot_id(self, lot):
        return f'L{lot.idx:06d}'

    def tool_id(self, machine):
        return f'T{machine.idx:04d}'

    # ---- 헤더 -----------------------------------------------------------
    H_TOOL = ['tool_id', 'tool_idx', 'toolgroup', 'area', 'seq_in_group', 'location', 'cascading',
              'load_s', 'unload_s']
    H_LOT = ['lot_id', 'lot_idx', 'order_name', 'part', 'route', 'lot_type', 'priority', 'wafers',
             'release_s', 'due_s', 'done_s', 'initial_wip', 'initial_step']
    H_STEP = ['event_id', 'lot_id', 'route', 'step_seq', 'step_name', 'toolgroup', 'tool_id', 'dispatch_id',
              'batch_size', 'queue_enter_s', 'track_in_s', 'setup_s', 'track_out_s', 'transport_s',
              'next_arrival_s', 'planned_track_out_s', 'is_last_step']
    H_SKIP = ['lot_id', 'route', 'step_seq', 'step_name', 'toolgroup', 'sampling_pct', 'time_s']
    H_REWORK = ['lot_id', 'route', 'from_step_seq', 'to_step_seq', 'time_s']
    H_DED = ['lot_id', 'route', 'source_step_seq', 'target_step_seq', 'tool_id', 'time_s']
    H_DOWN = ['down_id', 'tool_id', 'toolgroup', 'kind', 'start_s', 'end_s', 'duration_s', 'tool_was_idle']
    H_SWIP = ['lot_id', 'state', 'step_seq', 'step_name', 'toolgroup', 'tool_id', 'since_s']
    H_SDED = ['lot_id', 'target_step_seq', 'tool_id']
    H_STOOL = ['tool_id', 'state', 'until_s']
    H_SCQT = ['lot_id', 'entry_step_seq', 'exit_step_seq', 'entry_track_out_s', 'cqt_s']
    H_HIST = ['event_id', 'lot_id', 'route', 'step_seq', 'step_name', 'toolgroup', 'tool_id', 'dispatch_id',
              'track_in_s', 'track_out_s']

    # ---- 스냅샷 ---------------------------------------------------------
    def _maybe_snapshot(self, instance):
        if self.snapshot_done or instance.current_time < self.log_from:
            return
        self.snapshot_done = True
        now = instance.current_time
        self.machines = {m.idx: m for m in instance.machines}
        for lot in instance.active_lots:
            if lot.actual_step is None:
                continue
            row = self.open_steps.get(lot.idx)
            state, tool, since = ('PROCESSING', row['tool_id'], row['track_in_s']) if row else \
                ('WAITING', '', lot.free_since)
            st = lot.actual_step
            self._write('snapshot_wip.csv', self.H_SWIP,
                        [self.lot_id(lot), state, st.order, st.step_name, st.family, tool, self._t(since)])
            for target, midx in lot.dedications.items():
                if target >= st.order:
                    self._write('snapshot_dedication.csv', self.H_SDED,
                                [self.lot_id(lot), target, self.tool_id(self.machines[midx])])
            for exit_seq, (entry_seq, out_s, cqt_s) in sorted(self.cqt_open.get(lot.idx, {}).items()):
                self._write('snapshot_cqt.csv', self.H_SCQT,
                            [self.lot_id(lot), entry_seq, exit_seq, self._t(out_s), self._t(cqt_s)])
        for m in instance.machines:
            if self.down_until.get(m.idx, -1) > now:
                state, until = 'DOWN', self.down_until[m.idx]
            elif self.busy_until.get(m.idx, -1) > now:
                state, until = 'BUSY', self.busy_until[m.idx]
            else:
                state, until = 'IDLE', None
            self._write('snapshot_tool.csv', self.H_STOOL, [self.tool_id(m), state, self._t(until)])

    # ---- 시뮬레이터 훅 ---------------------------------------------------
    def on_sim_init(self, instance):
        self.bind_routes(instance)
        seq = {}
        for m in instance.machines:
            seq[m.family] = seq.get(m.family, 0) + 1
            self.tool_seq[m.idx] = seq[m.family]
            self.pm_seen[m.idx] = m.pmed_time
            self.setup_seen[m.idx] = m.setuped_time
            self._write('tools.csv', self.H_TOOL,
                        [self.tool_id(m), m.idx, m.family, m.group, seq[m.family], m.loc, int(m.cascading),
                         m.load_time, m.unload_time])

    def on_lots_release(self, instance, lots):
        for lot in lots:
            st = lot.actual_step
            self.lots[lot.idx] = dict(
                lot_id=self.lot_id(lot), lot_idx=lot.idx, order_name=lot.name, part=lot.part_name,
                route=self._route(st) if st is not None else '', lot_type=lot_type(lot.name, lot.priority),
                priority=lot.priority, wafers=lot.pieces, release_s=instance.current_time,
                due_s=lot.deadline_at, done_s=None,
                initial_wip=int(len(lot.processed_steps) > 0 or lot.release_at < 0),
                initial_step=st.order if st is not None else '')
        self._maybe_snapshot(instance)

    def bind_routes(self, instance):
        """step 객체 -> 라우트 이름 색인."""
        self._step_route = {}
        for key, route in instance.routes.items():
            name = key[0] if isinstance(key, tuple) else key
            name = name.replace('.txt', '')
            for s in route.steps:
                self._step_route[id(s)] = name

    def _route(self, step):
        return self._step_route.get(id(step), '') if hasattr(self, '_step_route') else ''

    def on_dispatch(self, instance, machine, lots, machine_end_time, lot_end_time):
        self._maybe_snapshot(instance)
        now = instance.current_time
        self.dispatch_id += 1
        setup = machine.setuped_time - self.setup_seen[machine.idx]
        self.setup_seen[machine.idx] = machine.setuped_time
        pm = machine.pmed_time - self.pm_seen[machine.idx]
        self.pm_seen[machine.idx] = machine.pmed_time
        self.busy_until[machine.idx] = machine_end_time
        if pm > 0:  # 처리 매수 기준 PM. 작업 직후에 붙는다
            self._down(machine, 'PM_COUNTER', machine_end_time - pm, pm, False)
        for lot in lots:
            st = lot.actual_step
            self.cqt_open.get(lot.idx, {}).pop(st.order, None)  # CQT 종료 step 진입
            tt = lot.transport_time - self.transport_seen.get(lot.idx, 0)
            self.transport_seen[lot.idx] = lot.transport_time
            self.event_id += 1
            self.open_steps[lot.idx] = dict(
                event_id=self.event_id, lot_id=self.lot_id(lot), route=self._route(st), step_seq=st.order,
                step_name=st.step_name, toolgroup=st.family, tool_id=self.tool_id(machine),
                dispatch_id=self.dispatch_id, batch_size=len(lots), queue_enter_s=lot.free_since,
                track_in_s=now, setup_s=setup, transport_s=tt, planned_track_out_s=lot_end_time - tt,
                is_last_step=int(len(lot.remaining_steps) == 0), _step=st)
            if st.lot_to_lens_dedication is not None and now >= self.log_from:
                self._write('lot_dedication.csv', self.H_DED,
                            [self.lot_id(lot), self._route(st), st.order, st.lot_to_lens_dedication,
                             self.tool_id(machine), self._t(now)])

    def _close_step(self, instance, lot):
        row = self.open_steps.pop(lot.idx, None)
        if row is None:
            return None
        arrival = instance.current_time
        if arrival < self.log_from:
            if self.history_from is not None and row['track_in_s'] >= self.history_from:
                self._write('lot_step_history.csv', self.H_HIST, [
                    row['event_id'], row['lot_id'], row['route'], row['step_seq'], row['step_name'],
                    row['toolgroup'], row['tool_id'], row['dispatch_id'], self._t(row['track_in_s']),
                    self._t(arrival - row['transport_s'])])
            return row
        self._write('lot_step_event.csv', self.H_STEP, [
            row['event_id'], row['lot_id'], row['route'], row['step_seq'], row['step_name'], row['toolgroup'],
            row['tool_id'], row['dispatch_id'], row['batch_size'], self._t(row['queue_enter_s']),
            self._t(row['track_in_s']), self._t(row['setup_s']), self._t(arrival - row['transport_s']),
            self._t(row['transport_s']), self._t(arrival), self._t(row['planned_track_out_s']),
            row['is_last_step']])
        return row

    def on_step_done(self, instance, lot, step):
        # 주의: step을 건너뛴 경우 시뮬레이터가 넘기는 step은 완료된 step이 아니라 마지막으로 건너뛴 step이다.
        # 완료된 step은 열려 있던 행에서 가져온다.
        self._maybe_snapshot(instance)
        row = self._close_step(instance, lot)
        if row is not None and row['_step'].cqt_for_step not in (None, ''):  # CQT 진입 step 완료
            st = row['_step']
            self.cqt_open.setdefault(lot.idx, {})[int(st.cqt_for_step)] = (
                st.order, instance.current_time - row['transport_s'], st.cqt_time)
        if row is None or instance.current_time < self.log_from:
            return
        done, new = row['_step'], lot.actual_step
        if new.idx <= done.idx:
            self._write('lot_rework.csv', self.H_REWORK,
                        [self.lot_id(lot), self._route(done), done.order, new.order, self._t(instance.current_time)])
            return
        skipped = []
        for s in lot.processed_steps[::-1]:
            if s is done:
                break
            skipped.append(s)
        for s in skipped[::-1]:
            self._write('lot_step_skipped.csv', self.H_SKIP,
                        [self.lot_id(lot), self._route(s), s.order, s.step_name, s.family,
                         s.sampling_percent, self._t(instance.current_time)])

    def on_lot_done(self, instance, lot):
        self._close_step(instance, lot)
        self.cqt_open.pop(lot.idx, None)
        if lot.idx in self.lots:
            self.lots[lot.idx]['done_s'] = instance.current_time

    def _down(self, machine, kind, start, length, was_idle):
        end = start + length
        self.down_until[machine.idx] = max(self.down_until.get(machine.idx, -1), end)
        if end < self.log_from:
            return
        n = self.counts.get('tool_down.csv', 0) + 1
        self._write('tool_down.csv', self.H_DOWN,
                    [f'D{n:06d}', self.tool_id(machine), machine.family, kind, self._t(start), self._t(end),
                     self._t(length), int(was_idle)])

    def on_breakdown(self, instance, ev):
        self._maybe_snapshot(instance)
        self._down(ev.machine, 'BREAKDOWN', instance.current_time, ev.last_length, ev.machine_was_idle)

    def on_preventive_maintenance(self, instance, ev):
        self._maybe_snapshot(instance)
        self.pm_seen[ev.machine.idx] = ev.machine.pmed_time
        self._down(ev.machine, 'PM_TIME', instance.current_time, ev.last_length, ev.machine_was_idle)

    def on_sim_done(self, instance):
        end = instance.current_time
        for row in list(self.open_steps.values()):  # 종료 시점에 처리 중인 step
            self._write('lot_step_event.csv', self.H_STEP, [
                row['event_id'], row['lot_id'], row['route'], row['step_seq'], row['step_name'], row['toolgroup'],
                row['tool_id'], row['dispatch_id'], row['batch_size'], self._t(row['queue_enter_s']),
                self._t(row['track_in_s']), self._t(row['setup_s']), '', self._t(row['transport_s']), '',
                self._t(row['planned_track_out_s']), row['is_last_step']])
        # 종료 시점에 대기 중인 lot. step 행은 track-in 때 생기므로 이 lot들의 대기 step은 lot_step_event에 없다.
        # 이 스냅샷이 없으면 종료 전 시각 t의 WIP에서 "t에 대기 중이고 종료까지 투입되지 못한 lot"이 빠진다(연구노트 U-13)
        for lot in instance.active_lots:
            if lot.actual_step is None or lot.idx in self.open_steps:
                continue
            st = lot.actual_step
            self._write('snapshot_wip_end.csv', self.H_SWIP,
                        [self.lot_id(lot), 'WAITING', st.order, st.step_name, st.family, '', self._t(lot.free_since)])
        for r in self.lots.values():
            if r['done_s'] is not None and r['done_s'] < self.log_from:
                continue
            self._write('lots.csv', self.H_LOT, [r[k] if k not in ('release_s', 'due_s', 'done_s') else self._t(r[k])
                                                 for k in self.H_LOT])
        for f in self.files.values():
            f.close()
        self.meta.update(log_from_s=self.log_from, sim_end_s=end, row_counts=self.counts)
        with open(os.path.join(self.out_dir, 'run_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=2)
