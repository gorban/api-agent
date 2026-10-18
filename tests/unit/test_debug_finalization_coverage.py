"""Deterministic fault injection at worker ownership and publication boundaries."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.testing import inspection, integration
from api_agent.testing.collection import TestIdentity


class DormantThread:
    def __init__(self, target, **kwargs):
        self.target = target
    def start(self):
        if self.target.__name__ == 'read_output':
            self.target()
    def join(self, timeout=None):
        pass
    def is_alive(self):
        return False


@pytest.fixture
def worker_harness(tmp_path, monkeypatch):
    """Keep pytest wire facts real while controlling process/reader scheduling."""
    identity = TestIdentity('base::test_case.py::test_ok', 'base', 'test_case.py',
                            'test_case.py::test_ok', root=tmp_path)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    state = SimpleNamespace(identity=identity, context=context, processes=[], finalized=[],
                            events=[], handles=[], descriptors=[], retained=False, rows=None,
                            output='worker evidence\n')
    original_pipe, original_close = os.pipe, os.close
    original_inspection_init = inspection.IntegrationInspection.__init__
    def pipe():
        pair = original_pipe()
        for descriptor in pair:
            metadata = os.fstat(descriptor)
            state.descriptors.append((descriptor, metadata.st_dev, metadata.st_ino))
        return pair
    def track_inspection(handle, *args, **kwargs):
        original_inspection_init(handle, *args, **kwargs)
        state.handles.append(handle)
    def close_owned_descriptor(descriptor, device, inode):
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            return
        # A previously closed descriptor may have been reused by pytest.
        if (metadata.st_dev, metadata.st_ino) == (device, inode):
            original_close(descriptor)
    monkeypatch.setattr(integration.os, 'pipe', pipe)
    monkeypatch.setattr(inspection.IntegrationInspection, '__init__', track_inspection)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: (identity,))
    monkeypatch.setattr(integration.threading, 'Thread', DormantThread)
    def popen(args, **kwargs):
        event_file = Path(args[3])
        rows = state.rows
        if rows is None:
            rows = [dict(kind='report', node=identity.node_id, phase=phase,
                         status='passed', duration=.1, traceback='')
                    for phase in ('setup', 'call', 'teardown')]
            rows += [dict(kind='run_complete', exit_code=0)]
            if state.retained:
                rows += [dict(kind='inspection_open', endpoint='http://127.0.0.1:1234')]
        event_file.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        process = SimpleNamespace(stdout=io.StringIO(state.output), returncode=None,
                                  kwargs=kwargs, args=args, calls=0, event_file=event_file)
        def poll():
            process.calls += 1
            if not state.retained and process.calls > 1:
                process.returncode = 0
            return process.returncode
        process.poll = poll
        state.processes.append(process)
        return process
    def finalize(process):
        state.finalized.append(process)
        process.returncode = 0
    monkeypatch.setattr(integration.subprocess, 'Popen', popen)
    monkeypatch.setattr(integration, 'finalize', finalize)
    monkeypatch.setattr(inspection, 'finalize', finalize)
    state.run = lambda **kwargs: integration.run_integration_tests(context, (identity,),
        accepted=True, on_event=state.events.append, **kwargs)
    yield state
    try:
        for process in state.processes:
            assert process in state.finalized, 'owned worker was not reaped'
    finally:
        # Check the tested ownership outcome before forcing fixture cleanup.
        # Assertion failures and injected cleanup errors must not leak real FDs.
        monkeypatch.setattr(inspection, 'finalize', finalize)
        with ExitStack() as cleanup:
            for descriptor in state.descriptors:
                cleanup.callback(close_owned_descriptor, *descriptor)
            for process in state.processes:
                cleanup.callback(process.stdout.close)
                if process not in state.finalized:
                    cleanup.callback(finalize, process)
            for handle in state.handles:
                cleanup.callback(handle.directory.temporary.cleanup)
            for handle in state.handles:
                handle.on_event = None
                handle.abort()


@pytest.fixture
def retained_handle(tmp_path, monkeypatch):
    monkeypatch.setattr(inspection.threading, 'Thread', DormantThread)
    temporary = SimpleNamespace(name=str(tmp_path), cleaned=False)
    temporary.cleanup = lambda: setattr(temporary, 'cleaned', True)
    directory = inspection.InspectionDirectory(temporary)
    process = SimpleNamespace(stdout=io.StringIO(), returncode=None)
    process.poll = lambda: process.returncode
    reader = DormantThread(lambda: None)
    finalized = []
    monkeypatch.setattr(inspection, 'finalize', lambda process: finalized.append(process))
    events = []
    handle = inspection.IntegrationInspection(process, directory, tmp_path / 'events', reader,
                                             on_event=events.append)
    yield SimpleNamespace(handle=handle, process=process, temporary=temporary,
                          events=events, finalized=finalized)
    handle.on_event = None
    handle.reader.is_alive = lambda: False
    handle.abort()


def test_missing_event_file_does_not_invent_cleanup_ack(retained_handle):
    state = retained_handle
    assert state.handle._rows() == []
    state.process.returncode = 9
    assert state.handle.end_inspection()
    assert state.handle.closed and state.temporary.cleaned
    assert [row.kind for row in state.events] == ['inspection_lost']


def test_closed_monitor_and_callback_failure_are_safe(retained_handle):
    state = retained_handle
    state.handle.abort()
    state.handle.on_event = lambda row: (_ for _ in ()).throw(ValueError('private callback'))
    state.handle._emit('inspection_closed')
    state.handle._watch()
    assert state.handle.closed and len(state.finalized) == 1
    assert state.handle.close() is True


def test_no_callback_and_wait_timeout_preserve_ownership(retained_handle):
    state = retained_handle
    state.handle.on_event = None
    state.handle._emit('inspection_open')
    assert not state.handle.wait_closed(timeout=0)
    assert not state.handle.closed and not state.finalized
    state.handle.abort()
    assert state.handle.wait_closed() and state.temporary.cleaned


@pytest.mark.parametrize('registry', [None, []])
def test_monitor_start_interrupt_rolls_back_transfer(tmp_path, monkeypatch, registry):
    class InterruptThread(DormantThread):
        def start(self):
            raise KeyboardInterrupt
    monkeypatch.setattr(inspection.threading, 'Thread', InterruptThread)
    directory = inspection.InspectionDirectory(SimpleNamespace(name=str(tmp_path)))
    with pytest.raises(KeyboardInterrupt):
        inspection.IntegrationInspection(object(), directory, tmp_path / 'events', object(), registry=registry)
    assert not directory.retained
    if registry is not None:
        assert registry == []


def test_cleanup_pending_reply_keeps_worker_for_explicit_retry(retained_handle):
    state = retained_handle
    state.handle.event_file.write_text('{"kind":"inspection_cleanup_pending","attempt":1}\n')
    assert not state.handle.end_inspection(timeout=.1)
    command = json.loads((Path(state.temporary.name) / 'inspection-command.json').read_text())
    assert command == {'version': 1, 'attempt': 1, 'command': 'end_inspection'}
    assert state.handle.pending and not state.finalized
    state.handle.abort()
    assert state.handle.closed


def test_wait_loop_observes_ack_during_sleep(retained_handle, monkeypatch):
    state = retained_handle
    monkeypatch.setattr(inspection.time, 'sleep', lambda seconds: state.handle.abort())
    assert state.handle.wait_closed(timeout=1)
    assert len(state.finalized) == 1


def test_end_inspection_observes_ack_during_poll(retained_handle, monkeypatch):
    state = retained_handle
    monkeypatch.setattr(inspection.time, 'sleep', lambda seconds: state.handle.abort())
    assert state.handle.end_inspection(timeout=1)
    assert not state.handle.pending and len(state.finalized) == 1


def test_final_deadline_check_preserves_concurrent_ack(retained_handle, monkeypatch):
    state = retained_handle
    calls = []
    def now():
        calls.append(1)
        if len(calls) == 2:
            state.handle.abort()
        return float(len(calls))
    monkeypatch.setattr(inspection.time, 'monotonic', now)
    assert state.handle.end_inspection(timeout=.1)
    assert not state.handle.pending and len(state.finalized) == 1


def test_nonposix_launcher_does_not_allocate_inheritable_fd(worker_harness, monkeypatch):
    state = worker_harness
    monkeypatch.setattr(integration, 'os', SimpleNamespace(**{name: getattr(os, name)
                        for name in dir(os) if name != 'name'}, name='nt'))
    result = state.run()
    assert result.exit_code == 0
    assert 'pass_fds' not in state.processes[0].kwargs
    assert state.processes[0].kwargs['start_new_session'] is False


@pytest.mark.parametrize('error', [OSError('private start'), KeyboardInterrupt()])
def test_start_failure_closes_both_liveness_fds(worker_harness, monkeypatch, error):
    state = worker_harness
    descriptors = []
    original_pipe = os.pipe
    def pipe():
        pair = original_pipe()
        descriptors.extend(pair)
        return pair
    monkeypatch.setattr(integration.os, 'pipe', pipe)
    monkeypatch.setattr(integration.subprocess, 'Popen', lambda *args, **kwargs: (_ for _ in ()).throw(error))
    if isinstance(error, OSError):
        result = state.run()
        assert result.exit_code == 1 and 'private start' not in result.output
    else:
        with pytest.raises(KeyboardInterrupt) as captured:
            state.run()
        assert captured.value.inspections == ()
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_reader_start_failure_reaps_worker_before_return(worker_harness, monkeypatch):
    state = worker_harness
    class FailedReader(DormantThread):
        def start(self):
            raise RuntimeError('private reader failure')
    monkeypatch.setattr(integration.threading, 'Thread', FailedReader)
    result = state.run()
    assert result.exit_code == 1 and 'private reader failure' not in result.output
    assert state.processes[0].stdout.closed


@pytest.mark.parametrize('exit_code', [True, '0', None])
def test_run_complete_requires_integer_exit_status(worker_harness, exit_code):
    state = worker_harness
    state.rows = [{'kind': 'run_complete', 'exit_code': exit_code}]
    result = state.run()
    assert result.exit_code == 1
    assert 'Invalid integration test event' in result.output
    assert not result.inspections


def test_retained_publication_failure_keeps_reviewable_cleanup_handle(worker_harness):
    state = worker_harness
    state.retained = True
    result = state.run(after_run=lambda row: (_ for _ in ()).throw(RuntimeError('private publish')))
    assert result.exit_code == 1 and result.tests[0].status == 'passed'
    assert 'private publish' not in result.output
    assert len(result.inspections) == 1 and not result.inspections[0].closed
    result.inspections[0].abort()


@pytest.mark.parametrize('boundary', ['publication', 'finished'])
def test_interrupt_after_transfer_exposes_and_reaps_owned_handle(worker_harness, boundary):
    state = worker_harness
    state.retained = True
    kwargs = {}
    def interrupt(row):
        if boundary == 'publication' or row.kind == 'finished':
            raise KeyboardInterrupt
        state.events.append(row)
    if boundary == 'publication':
        kwargs['after_run'] = interrupt
    else:
        kwargs['on_event'] = interrupt
        # Harness supplies on_event by default.
        state.run = lambda **kw: integration.run_integration_tests(state.context,
            (state.identity,), accepted=True, **kw)
    with pytest.raises(KeyboardInterrupt) as captured:
        state.run(**kwargs)
    assert len(captured.value.inspections) == 1
    assert captured.value.inspections[0].closed
    assert state.processes[0].stdout.closed


def test_partial_and_nondict_event_neighbors_do_not_create_ack(retained_handle):
    state = retained_handle
    state.handle.event_file.write_text('[]\n{"kind":"inspection_closed"}\n{"kind":')
    assert state.handle._rows() == [{'kind': 'inspection_closed'}]
    state.handle.abort()
    state.handle.abort()
    assert len(state.finalized) == 1


@pytest.mark.parametrize('acknowledged', [False, True])
def test_monitor_waits_for_worker_then_reports_durable_outcome(retained_handle, monkeypatch, acknowledged):
    state = retained_handle
    state.handle.event_file.write_text('{"kind":"inspection_closed"}\n' if acknowledged else '')
    monkeypatch.setattr(inspection.time, 'sleep', lambda seconds: setattr(state.process, 'returncode', 0))
    state.handle._watch()
    assert state.handle.closed and state.handle.lost is not acknowledged
    assert [row.kind for row in state.events] == ['inspection_closed' if acknowledged else 'inspection_lost']


@pytest.mark.parametrize('entry', ['monitor', 'command'])
def test_unjoined_reader_keeps_ack_and_worker_ownership_for_retry(retained_handle, entry):
    state = retained_handle
    state.handle.event_file.write_text('{"kind":"inspection_closed"}\n')
    state.process.returncode = 0
    state.handle.reader.is_alive = lambda: True
    if entry == 'monitor':
        state.handle._watch()
    else:
        assert not state.handle.end_inspection()
    assert state.handle.pending and not state.handle.closed
    assert not state.handle.lost
    assert not state.temporary.cleaned and not state.process.stdout.closed
    assert [row.kind for row in state.events] == ['inspection_cleanup_pending']
    state.handle.reader.is_alive = lambda: False
    assert state.handle.end_inspection()
    assert state.handle.closed and not state.handle.pending and not state.handle.lost


@pytest.mark.parametrize('failure', ['oserror', 'interrupt', 'reader'])
def test_nonposix_failure_cleanup_never_closes_foreign_fd(worker_harness, monkeypatch, failure):
    state = worker_harness
    native = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if name != 'name'}, name='nt')
    native.close = lambda fd: pytest.fail('non-POSIX execution closed a foreign descriptor')
    monkeypatch.setattr(integration, 'os', native)
    if failure == 'reader':
        class FailedReader(DormantThread):
            def start(self):
                raise RuntimeError('reader unavailable')
        monkeypatch.setattr(integration.threading, 'Thread', FailedReader)
    else:
        error = OSError('spawn unavailable') if failure == 'oserror' else KeyboardInterrupt()
        monkeypatch.setattr(integration.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(error))
    if failure == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            state.run()
    else:
        result = state.run()
        assert result.exit_code == 1 and not result.inspections
    if state.processes:
        assert state.processes[0].stdout.closed


@pytest.mark.parametrize('retained', [False, True])
def test_event_directory_disappearance_respects_transferred_ownership(worker_harness, monkeypatch, retained):
    state = worker_harness
    state.retained = retained
    if retained:
        original = inspection.IntegrationInspection
        def transferred(*args, **kwargs):
            handle = original(*args, **kwargs)
            handle.abort()
            return handle
        monkeypatch.setattr(inspection, 'IntegrationInspection', transferred)
    else:
        original = integration.subprocess.Popen
        def disappeared(*args, **kwargs):
            process = original(*args, **kwargs)
            process.event_file.unlink()
            return process
        monkeypatch.setattr(integration.subprocess, 'Popen', disappeared)
    result = state.run()
    assert result.exit_code == (0 if retained else 1)
    if retained:
        assert result.inspections[0].closed and result.tests[0].status == 'passed'
    else:
        assert result.tests[0].status == 'error'
        diagnostic = 'Integration pytest execution and report collection failure category: unavailable'
        assert diagnostic in result.output
        assert diagnostic in result.tests[0].traceback
        assert 'Stack locations' in result.tests[0].traceback


def test_postpublication_output_is_drained_without_growing_final_evidence(worker_harness, monkeypatch):
    state = worker_harness
    state.retained = True
    class DeferredReader(DormantThread):
        def start(self):
            pass
    monkeypatch.setattr(integration.threading, 'Thread', DeferredReader)
    observed = []
    def publish(result):
        reader = result.inspections[0].reader
        reader.target()
        queue = reader.target.__defaults__[1]
        observed.append((queue.qsize(), result.output))
    result = state.run(after_run=publish)
    try:
        assert observed == [(0, '')]
        assert 'worker evidence' not in result.output
        assert result.tests[0].status == 'passed'
    finally:
        result.inspections[0].abort()


def test_cancelled_publication_retains_failed_emergency_reap_handle(worker_harness, monkeypatch):
    import threading
    state = worker_harness
    state.retained = True
    cancelled = threading.Event()
    original = inspection.finalize
    monkeypatch.setattr(inspection, 'finalize', lambda process: (_ for _ in ()).throw(TimeoutError('private reap')))
    result = state.run(cancel_event=cancelled, after_run=lambda row: cancelled.set())
    assert result.exit_code == 130 and result.cancelled
    assert result.inspections and not result.inspections[0].closed
    assert 'Inspection cleanup pending; retry required.' in result.output
    assert 'private reap' not in result.output
    monkeypatch.setattr(inspection, 'finalize', original)
    result.inspections[0].abort()


def test_initialization_interrupt_after_reports_reaps_untransferred_worker(worker_harness, monkeypatch):
    state = worker_harness
    state.retained = True
    class InterruptedMonitor(DormantThread):
        def start(self):
            if self.target.__name__ == '_watch':
                raise KeyboardInterrupt
            super().start()
    monkeypatch.setattr(integration.threading, 'Thread', InterruptedMonitor)
    with pytest.raises(KeyboardInterrupt) as captured:
        state.run()
    assert captured.value.inspections == ()
    assert state.processes[0].stdout.closed


def test_inspection_cleanup_retains_local_cause_after_report(worker_harness, monkeypatch):
    import threading
    state = worker_harness
    state.retained = True
    cancel = threading.Event()
    originals = []
    def fail_abort():
        raise RuntimeError('synthetic local inspection cleanup cause')
    def cancel_after_report(run):
        for handle in run.inspections:
            originals.append((handle, handle.abort))
            monkeypatch.setattr(handle, 'abort', fail_abort)
        cancel.set()
    result = state.run(cancel_event=cancel, after_run=cancel_after_report)
    assert result.exit_code == 130 and result.tests[0].status == 'passed'
    assert 'synthetic local inspection cleanup cause' in result.tests[0].local_diagnostic
    assert 'synthetic local inspection cleanup cause' not in result.output
    assert all('synthetic local inspection cleanup cause' not in event.traceback for event in state.events)
    for handle, original_abort in originals:
        monkeypatch.setattr(handle, 'abort', original_abort)
        handle.abort()
