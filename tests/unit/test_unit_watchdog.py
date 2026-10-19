"""Unit deadlines do not depend on output volume or another worker's clock."""
import io
import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.testing import runner, unit_watchdog
from api_agent.testing.collection import TestIdentity
from api_agent.testing.unit_watchdog import UnitWatchdog


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, duration): self.now += duration


def test_active_clock_spans_all_phases_without_refresh():
    clock = Clock()
    watchdog = UnitWatchdog(clock=clock)
    watchdog.observe('one', 'test_started')
    clock.now = 30
    watchdog.observe('one', 'report', phase='setup')
    clock.now = 59
    watchdog.observe('one', 'test_started')
    watchdog.observe('one', 'report', phase='call')
    assert not watchdog.expired()
    clock.now = 60
    deadline, = watchdog.expired()
    assert (deadline.identity, deadline.elapsed, deadline.limit) == ('one', 60, 60)
    assert deadline.diagnostic(4) == ('Selected unit #4 exceeded its setup-through-teardown deadline: '
                                     '60.00s elapsed; limit 60.00s.')


def test_parallel_units_own_independent_deadlines_clipped_to_caller():
    clock = Clock()
    watchdog = UnitWatchdog(clock=clock, deadline=80)
    watchdog.observe('one', 'test_started')
    clock.now = 30
    watchdog.observe('two', 'test_started')
    clock.now = 60
    assert [deadline.identity for deadline in watchdog.expired()] == ['one']
    watchdog.observe('one', 'report', phase='teardown')
    clock.now = 79
    assert not watchdog.expired()
    clock.now = 80
    deadline, = watchdog.expired()
    assert (deadline.identity, deadline.elapsed, deadline.limit) == ('two', 50, 50)


def test_no_active_guard_ignores_output_and_progress_then_restarts_after_teardown():
    clock = Clock()
    watchdog = UnitWatchdog(clock=clock)
    clock.now = 59
    watchdog.observe('one', 'output')
    watchdog.observe('one', 'component_progress')
    assert not watchdog.expired()
    clock.now = 60
    deadline, = watchdog.expired()
    assert deadline.identity is None
    assert deadline.diagnostic() == ('Unit pytest exceeded its startup/finalization gap deadline: '
                                     '60.00s elapsed; limit 60.00s.')
    watchdog.observe('one', 'test_started')
    clock.now = 70
    watchdog.observe('one', 'report', phase='teardown')
    clock.now = 129
    watchdog.observe('one', 'report', phase='teardown')
    watchdog.observe('one', 'test_started')
    assert not watchdog.expired()
    clock.now = 130
    assert watchdog.expired()[0].elapsed == 60


@pytest.mark.parametrize('stamp', [True, float('inf'), float('nan'), '0', None, 100, 10 ** 400])
def test_unusable_or_future_worker_timestamp_cannot_extend_deadline(stamp):
    clock = Clock()
    clock.now = 10
    watchdog = UnitWatchdog(clock=clock)
    watchdog.observe('one', 'test_started', at=stamp)
    clock.now = 70
    assert watchdog.expired()[0].elapsed == 60


def test_worker_timestamp_accounts_for_delayed_parent_delivery():
    clock = Clock()
    clock.now = 45
    watchdog = UnitWatchdog(clock=clock)
    watchdog.observe('one', 'report', phase='setup', at=10)
    clock.now = 70
    assert watchdog.expired()[0].elapsed == 60


def test_exhausted_caller_budget_never_acquires_a_fresh_sixty_seconds():
    clock = Clock()
    clock.now = 10
    watchdog = UnitWatchdog(clock=clock, deadline=5)
    deadline, = watchdog.expired()
    assert deadline.limit == 0


def phase(node, when, status='passed'):
    return dict(kind='report', node=node, phase=when, status=status,
                duration=.1, traceback='original failure' if status == 'failed' else '')


@pytest.fixture
def controlled_runner(tmp_path, monkeypatch):
    root = tmp_path / 'base'
    root.mkdir()
    context = SimpleNamespace(base_root=root, owner_root=root)
    tests = tuple(TestIdentity(f'base::test.py::{name}', 'base', 'test.py', f'test.py::{name}')
                  for name in ('first', 'second', 'third'))
    clock = Clock()
    monkeypatch.setattr(runner, 'collect_tests', lambda *a, **k: tests)
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=clock, sleep=lambda _: clock.sleep(1)))
    monkeypatch.setattr(unit_watchdog, 'UNIT_LIMIT', 3)
    monkeypatch.setattr(runner, 'UnitWatchdog', lambda **kw: UnitWatchdog(clock=clock, **kw))
    state = SimpleNamespace(stops=0, events=None, rows=[], late=[], process=None, output='', stop_at=None, tail='', args=None)
    def launch(args, **kwargs):
        state.args = args
        state.events = Path(args[3])
        state.events.write_text(''.join(json.dumps(row) + '\n' for row in state.rows) + state.tail)
        state.process = SimpleNamespace(stdout=io.StringIO(state.output), returncode=None, poll=lambda: state.process.returncode)
        return state.process
    def stop(process):
        state.stops += 1
        state.stop_at = clock.now
        if process.returncode is None:
            process.returncode = -15
        with state.events.open('a') as output:
            output.write(''.join(json.dumps(row) + '\n' for row in state.late))
    monkeypatch.setattr(runner.subprocess, 'Popen', launch)
    monkeypatch.setattr(runner, '_stop_execution', stop)
    return context, tests, clock, state


def test_timeout_is_sticky_after_late_pass_reports_and_keeps_prior_verdicts(controlled_runner):
    context, tests, clock, state = controlled_runner
    first, second, third = tests
    state.rows = [phase(first.node_id, part) for part in ('setup', 'call', 'teardown')]
    state.rows += [phase(second.node_id, 'setup'), phase(second.node_id, 'call', 'failed')]
    state.rows += [dict(kind='test_started', node=third.node_id),
                   phase(third.node_id, 'setup'), phase(third.node_id, 'call')]
    state.late = [phase(third.node_id, 'teardown')]
    result = runner.run_unit_tests(context, tests)
    assert result.exit_code == 1 and not result.cancelled
    assert [row.status for row in result.tests] == ['passed', 'failed', 'error']
    assert 'original failure' in result.tests[1].traceback
    assert 'Selected unit #3' in result.tests[2].traceback
    assert '3.00s elapsed; limit 3.00s' in result.tests[2].traceback
    assert state.stops == 1


def test_no_active_startup_guard_fails_without_reclassifying_as_cancelled(controlled_runner):
    context, tests, clock, state = controlled_runner
    result = runner.run_unit_tests(context, tests)
    assert result.exit_code == 1 and not result.cancelled
    assert all(row.status == 'error' for row in result.tests)
    assert 'startup/finalization gap deadline' in result.output
    assert clock.now == 3 and state.stops == 1



def test_durable_teardown_is_rechecked_before_timeout_decision(controlled_runner, monkeypatch):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [dict(kind='test_started', node=first.node_id),
                  phase(first.node_id, 'setup'), phase(first.node_id, 'call')]
    state.output = 'release teardown\n'
    def release(line):
        if line == 'release teardown\n':
            clock.now = 3
            with state.events.open('a') as stream:
                stream.write(json.dumps(phase(first.node_id, 'teardown')) + '\n')
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=clock,
        sleep=lambda _: setattr(state.process, 'returncode', 0)))
    result = runner.run_unit_tests(context, (first,), output=release)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert 'deadline' not in result.output
    assert state.stops == 1


def test_structured_completion_precedes_bounded_raw_batches(controlled_runner):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [phase(first.node_id, part) for part in ('setup', 'call', 'teardown')]
    state.output = 'raw evidence\n' * 100
    finished = []
    def observe(event):
        if event.kind == 'test_finished':
            finished.append(event.identity)
    def raw(line):
        if line == 'raw evidence\n':
            assert finished == [first]
            clock.now += 1
    result = runner.run_unit_tests(context, tests, output=raw, on_event=observe)
    assert result.exit_code == 1 and not result.cancelled
    assert state.stop_at == 32
    assert result.tests[0].status == 'passed'
    assert result.output.count('raw evidence') == 100


def test_acknowledged_disposal_is_not_repeated_after_final_drain_failure(controlled_runner, monkeypatch):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.output = 'raise during final drain\n'
    def stop(process):
        state.stops += 1
        process.returncode = -15
        process._api_agent_disposal_complete = True
        with state.events.open('a') as stream:
            stream.write('{"invalid": "event"}\n')
    monkeypatch.setattr(runner, '_stop_execution', stop)
    with pytest.raises(KeyError):
        runner.run_unit_tests(context, (first,))
    assert state.stops == 1
    assert state.process.stdout.closed


def test_acknowledged_disposal_interruption_preserves_original_without_resignalling(controlled_runner, monkeypatch):
    context, tests, clock, state = controlled_runner
    primary = KeyboardInterrupt('after disposal acknowledgement')
    def stop(process):
        state.stops += 1
        process._api_agent_disposal_complete = True
        raise primary
    monkeypatch.setattr(runner, '_stop_execution', stop)
    with pytest.raises(KeyboardInterrupt) as caught:
        runner.run_unit_tests(context, tests)
    assert caught.value is primary
    assert state.stops == 1



def test_active_cancellation_remains_130_and_preserves_earlier_failure(controlled_runner, monkeypatch):
    import threading
    context, tests, clock, state = controlled_runner
    first, second, third = tests
    state.rows = [phase(first.node_id, 'setup'), phase(first.node_id, 'call', 'failed')]
    state.rows += [dict(kind='test_started', node=second.node_id), phase(second.node_id, 'call')]
    cancel = threading.Event()
    def sleep(_):
        cancel.set()
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=clock, sleep=sleep))
    result = runner.run_unit_tests(context, tests, cancel_event=cancel)
    assert result.cancelled and result.exit_code == 130
    assert [row.status for row in result.tests] == ['failed', 'cancelled', 'cancelled']
    assert 'original failure' in result.tests[0].traceback
    assert state.stops == 1


@pytest.mark.skipif(os.name != 'posix', reason='Owned process groups require POSIX')
def test_real_owned_group_timeout_reaps_descendant_and_cannot_accept_late_pass(tmp_path, monkeypatch):
    import subprocess
    import sys
    import textwrap
    from api_agent.testing.processes import _group_running, disposal_complete
    root = tmp_path / 'base'
    root.mkdir()
    context = SimpleNamespace(base_root=root, owner_root=root)
    tests = tuple(TestIdentity(f'base::test.py::{name}', 'base', 'test.py', f'test.py::{name}')
                  for name in ('first', 'second', 'third'))
    monkeypatch.setattr(runner, 'collect_tests', lambda *a, **k: tests)
    monkeypatch.setattr(unit_watchdog, 'UNIT_LIMIT', 1)
    original_popen = subprocess.Popen
    original_stop = runner._stop_execution
    processes, stops = [], []
    pid_file, late_file = tmp_path / 'owned-pids.json', tmp_path / 'late-teardown'
    worker = textwrap.dedent('''
        import json, os, signal, subprocess, sys, time
        from pathlib import Path
        events, pids, late = map(Path, sys.argv[1:])
        child = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
        pids.write_text(json.dumps([os.getpid(), child.pid]))
        def send(**row):
            row['at'] = time.monotonic()
            with events.open('a') as stream:
                stream.write(json.dumps(row) + '\\n')
        def report(node, phase):
            send(kind='report', node=node, phase=phase, status='passed', duration=.01, traceback='')
        def finish_on_term(*args):
            for phase in ('setup', 'call', 'teardown'):
                report('test.py::second', phase)
            late.touch()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, finish_on_term)
        for phase in ('setup', 'call', 'teardown'):
            report('test.py::first', phase)
        send(kind='test_started', node='test.py::second')
        while True:
            time.sleep(.01)
    ''')
    def launch(args, **kwargs):
        process = original_popen([sys.executable, '-u', '-c', worker, args[3], str(pid_file), str(late_file)], **kwargs)
        processes.append(process)
        return process
    def stop(process):
        stops.append(process)
        original_stop(process)
    monkeypatch.setattr(runner.subprocess, 'Popen', launch)
    monkeypatch.setattr(runner, '_stop_execution', stop)
    result = runner.run_unit_tests(context, tests)
    process, = processes
    assert result.exit_code == 1 and not result.cancelled
    assert [row.status for row in result.tests] == ['passed', 'error', 'error']
    assert late_file.is_file(), 'TERM handler never emitted the controlled late passing teardown'
    assert 'Selected unit #2' in result.tests[1].traceback
    assert stops == [process]
    assert disposal_complete(process)
    leader, child = json.loads(pid_file.read_text())
    assert leader == process.pid and not _group_running(leader)
    child_stat = Path(f'/proc/{child}/stat')
    assert not child_stat.exists() or child_stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'



def test_already_exited_owner_does_not_timeout_due_to_delayed_raw_delivery(controlled_runner):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [dict(kind='test_started', node=first.node_id),
                  phase(first.node_id, 'setup'), phase(first.node_id, 'call')]
    state.output = 'delayed evidence\n'
    def release(line):
        if line == 'delayed evidence\n':
            clock.now = 100
            teardown = dict(phase(first.node_id, 'teardown'), at=1)
            with state.events.open('a') as stream:
                stream.write(json.dumps(teardown) + '\n')
            state.process.returncode = 0
    result = runner.run_unit_tests(context, (first,), output=release)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert 'deadline' not in result.output


def test_partial_event_tail_cannot_hide_no_active_deadline(controlled_runner):
    context, tests, clock, state = controlled_runner
    state.tail = '{"partial":'
    result = runner.run_unit_tests(context, tests)
    assert result.exit_code == 1 and clock.now == 3
    assert 'startup/finalization gap deadline' in result.output


def test_output_reader_start_failure_disposes_owned_process(controlled_runner, monkeypatch):
    context, tests, clock, state = controlled_runner
    class Reader:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('synthetic reader start failure')
    monkeypatch.setattr(runner, 'threading', SimpleNamespace(Thread=Reader))
    with pytest.raises(RuntimeError, match='synthetic reader start failure'):
        runner.run_unit_tests(context, tests)
    assert state.stops == 1 and state.process.stdout.closed



def test_duplicate_teardown_keeps_one_completed_verdict(controlled_runner):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [phase(first.node_id, part) for part in ('setup', 'call', 'teardown', 'teardown')]
    state.output = 'owner exited\n'
    finished = []
    def exit_owner(line):
        state.process.returncode = 0
    result = runner.run_unit_tests(context, (first,), output=exit_owner,
        on_event=lambda event: finished.append(event) if event.kind == 'test_finished' else None)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert len(finished) == 1


@pytest.mark.parametrize('rows, expected', [
    (['call', 'teardown'], 'error'),
    (['setup', 'call'], 'error'),
    (['setup', 'teardown'], 'error'),
    (['setup', 'call', 'teardown'], 'passed'),
])
def test_only_complete_passing_phase_reports_can_pass(controlled_runner, rows, expected):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [phase(first.node_id, part) for part in rows]
    state.output = 'owner exited\n'
    result = runner.run_unit_tests(context, (first,),
        output=lambda line: setattr(state.process, 'returncode', 0))
    assert result.tests[0].status == expected
    assert result.exit_code == int(expected != 'passed')



def test_out_of_order_teardowns_anchor_idle_guard_to_latest_actual_completion():
    clock = Clock()
    watchdog = UnitWatchdog(clock=clock)
    watchdog.observe('one', 'test_started')
    watchdog.observe('two', 'test_started')
    clock.now = 40
    watchdog.observe('one', 'report', phase='teardown', at=30)
    watchdog.observe('two', 'report', phase='teardown', at=20)
    clock.now = 89
    assert not watchdog.expired()
    clock.now = 90
    assert watchdog.expired()[0].elapsed == 60



def test_distributed_unit_execution_disables_hidden_worker_restarts(controlled_runner):
    context, tests, clock, state = controlled_runner
    first = tests[0]
    state.rows = [phase(first.node_id, part) for part in ('setup', 'call', 'teardown')]
    state.output = 'owner exited\n'
    result = runner.run_unit_tests(context, (first,), parallel_jobs=2,
        output=lambda line: setattr(state.process, 'returncode', 0))
    assert result.exit_code == 0
    assert state.args[state.args.index('-n') + 1] == '2'
    assert '--max-worker-restart=0' in state.args



@pytest.mark.parametrize('construction', ['watchdog', 'reader'])
def test_post_launch_constructor_failure_disposes_group_and_closes_stdout(
        controlled_runner, monkeypatch, construction):
    context, tests, clock, state = controlled_runner
    primary = RuntimeError('synthetic constructor failure')
    def fail(*args, **kwargs):
        raise primary
    if construction == 'watchdog':
        monkeypatch.setattr(runner, 'UnitWatchdog', fail)
    else:
        monkeypatch.setattr(runner, 'threading', SimpleNamespace(Thread=fail))
    with pytest.raises(RuntimeError) as caught:
        runner.run_unit_tests(context, tests)
    assert caught.value is primary
    assert state.stops == 1 and state.process.stdout.closed



def test_post_start_interrupt_remains_inside_continuous_process_ownership(controlled_runner):
    import inspect
    import sys
    context, tests, clock, state = controlled_runner
    primary = KeyboardInterrupt('between reader start and event initialization')
    source, first_line = inspect.getsourcelines(runner.run_unit_tests.__wrapped__)
    interrupt_line = first_line + next(index for index, line in enumerate(source)
                                       if line.strip() == 'offset = 0')
    def interrupt(frame, event, arg):
        if event == 'line' and frame.f_code.co_filename == runner.__file__ and frame.f_lineno == interrupt_line:
            raise primary
        return interrupt
    previous = sys.gettrace()
    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            runner.run_unit_tests(context, tests)
    finally:
        sys.settrace(previous)
    assert caught.value is primary
    assert state.stops == 1 and state.process.stdout.closed
