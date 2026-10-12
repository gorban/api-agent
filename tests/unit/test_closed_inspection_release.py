"""Real inspection monitors transfer pending release ownership to every consumer."""
import asyncio
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import report_timeout_sync
from api_agent.testing import suite_lock as locks
from api_agent.testing.inspection import InspectionDirectory, IntegrationInspection
from api_agent.testing.inspection_controls import InspectionGroup, finish_terminal_inspection
from test_suite_cleanup_lease import assert_competing_suite_allowed, assert_competing_suite_blocked
from test_tui import fixture as tui_fixture


def unavailable(*args):
    raise RuntimeError('Synthetic release-thread startup failure')


@pytest.fixture
def closed_pending_inspection(tmp_path, monkeypatch):
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    from pathlib import Path
    event_file = Path(directory.name) / 'events.jsonl'
    event_file.write_text('{"kind":"inspection_closed"}\n')
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    handle = None
    try:
        with locks.suite_lock():
            lease = locks._CURRENT.get()
            handle = IntegrationInspection(process, InspectionDirectory(directory), event_file, reader)
        with monkeypatch.context() as failure, report_timeout_sync('Inspection monitor handoff', 5):
            failure.setattr(locks, 'start_new_thread', unavailable)
            process.stdin.write('x')
            process.stdin.flush()
            process.wait(timeout=2)
            handle._monitor.join(timeout=2)
        assert not handle._monitor.is_alive()
        assert handle.closed and handle.pending and len(handle._cleanup_callbacks) == 1
        assert lease.fd is not None and lease.closing is None
        assert_competing_suite_blocked()
        yield SimpleNamespace(handle=handle, lease=lease)
    finally:
        if process.poll() is None:
            process.stdin.close()
        process.wait(timeout=2)
        if handle is not None:
            handle.abort()
            handle._monitor.join(timeout=2)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
        directory.cleanup()


def assert_release_finished(subject):
    assert subject.handle.closed and not subject.handle.pending
    assert not subject.handle._cleanup_callbacks and subject.lease.fd is None
    assert not InspectionGroup((subject.handle,)).active
    assert_competing_suite_allowed()


def recorded(subject):
    return SimpleNamespace(execution=SimpleNamespace(inspections=(subject.handle,), tests=(), cancelled=False),
                           persisted=True, usage=None)


def test_wait_closed_acknowledges_the_pending_suite_release(closed_pending_inspection):
    subject = closed_pending_inspection
    assert subject.handle.wait_closed(timeout=.01)
    assert_release_finished(subject)


@pytest.mark.parametrize('force', [False, True])
def test_group_cleanup_retries_closed_but_unacknowledged_handles(closed_pending_inspection, force):
    subject = closed_pending_inspection
    group = InspectionGroup((subject.handle,))
    assert group.active
    assert group.close(force=force)
    assert_release_finished(subject)


def test_terminal_completion_waits_for_the_closed_handle_release(closed_pending_inspection):
    subject = closed_pending_inspection
    lines = []
    assert finish_terminal_inspection(recorded(subject), lines.append)
    assert 'Shared authentication cleanup remains pending' in lines[2]
    assert_release_finished(subject)


def test_tui_does_not_discard_closed_pending_ownership(closed_pending_inspection, tmp_path):
    subject = closed_pending_inspection
    app = tui_fixture(tmp_path)
    group = InspectionGroup((subject.handle,))
    app.inspections = group
    app.integration_exit_status = 0
    app.discard_finished_inspection()
    assert app.inspections is group and app.inspection_cleanup_active()
    assert asyncio.run(app.end_inspection(force=True))
    assert app.operation_status == 0 and not app.inspection_cleanup_active()
    assert_release_finished(subject)


def test_wait_closed_reports_a_still_unacknowledged_release(closed_pending_inspection, monkeypatch):
    subject = closed_pending_inspection
    with monkeypatch.context() as failure:
        failure.setattr(locks, 'start_new_thread', unavailable)
        assert not subject.handle.wait_closed(timeout=.01)
        assert subject.handle.pending and InspectionGroup((subject.handle,)).active
        assert_competing_suite_blocked()
    assert subject.handle.wait_closed(timeout=.01)
    assert_release_finished(subject)


def test_terminal_cannot_report_success_after_release_retry_fails(closed_pending_inspection, monkeypatch):
    subject = closed_pending_inspection
    lines = []
    with monkeypatch.context() as failure:
        failure.setattr(locks, 'start_new_thread', unavailable)
        with pytest.raises(CleanupPendingError) as caught:
            finish_terminal_inspection(recorded(subject), lines.append)
        assert caught.value.inspections == (subject.handle,)
        assert caught.value.inspection_group.active
        assert lines[-1].startswith('Inspection cleanup: FAIL')
        assert_competing_suite_blocked()
    asyncio.run(caught.value.retry())
    assert_release_finished(subject)


def test_tui_keeps_failed_release_retry_until_it_acknowledges(closed_pending_inspection, tmp_path, monkeypatch):
    subject = closed_pending_inspection
    app = tui_fixture(tmp_path)
    app.inspections = InspectionGroup((subject.handle,))
    app.integration_exit_status = 0
    with monkeypatch.context() as failure:
        failure.setattr(locks, 'start_new_thread', unavailable)
        assert not asyncio.run(app.end_inspection(force=True))
        app.discard_finished_inspection()
        assert app.inspection_cleanup_active() and app.inspection_failed and app.operation_status == 3
        assert_competing_suite_blocked()
    assert asyncio.run(app.end_inspection(force=True))
    assert app.operation_status == 0 and not app.inspection_failed
    assert_release_finished(subject)


def test_release_interruption_remains_visible_to_wait_consumers(closed_pending_inspection, monkeypatch):
    subject = closed_pending_inspection
    def interrupted(*args): raise KeyboardInterrupt()
    with monkeypatch.context() as failure:
        failure.setattr(locks, 'start_new_thread', interrupted)
        with pytest.raises(KeyboardInterrupt):
            subject.handle.wait_closed(timeout=.01)
        assert subject.handle.pending and subject.handle._cleanup_callbacks
        assert_competing_suite_blocked()
    assert subject.handle.wait_closed(timeout=.01)
    assert_release_finished(subject)


def test_callback_ownership_is_active_even_without_the_pending_flag():
    subject = SimpleNamespace(closed=True, pending=False, _cleanup_callbacks=[lambda: None])
    group = InspectionGroup((subject,))
    assert group.active
    subject._cleanup_callbacks.clear()
    assert not group.active


@pytest.mark.parametrize('pending_flag', [False, True])
def test_tui_completed_run_reports_closed_release_failure(closed_pending_inspection, tmp_path, pending_flag):
    from unittest.mock import AsyncMock, Mock
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.lifecycle import TestRunLifecycle
    from api_agent.testing.selection import SelectionModel
    subject = closed_pending_inspection
    subject.handle.pending = pending_flag
    identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    execution = IntegrationRunResult(0, (TestResult(identity, 'passed'),), '', inspections=(subject.handle,))
    app = tui_fixture(tmp_path, 'integration')
    app.selection = SelectionModel((identity,))
    app.discover_tests = AsyncMock(return_value=(identity,))
    app.test_run = TestRunLifecycle()
    app.collection_cancel = app.test_run.cancel_event
    app.show_unit_results = Mock()
    app.integration_runner = lambda *a, **kw: SimpleNamespace(execution=execution, exit_code=0, persisted=True)
    asyncio.run(app._run_integrations())
    assert app.unit_result is execution and app.integration_persisted
    assert app.inspection_failed and app.operation_status == 3
    assert app.inspection_cleanup_active()
    assert_competing_suite_blocked()
    assert asyncio.run(app.end_inspection(force=True))
    assert app.operation_status == 0 and not app.inspection_failed
    assert_release_finished(subject)
