from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from api_agent.testing.inspection_controls import InspectionGroup, finish_terminal_inspection
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult


def handle(*, pending=False, error=False):
    subject = SimpleNamespace(closed=False)
    def end():
        if error:
            raise RuntimeError('safe synthetic failure')
        if not pending:
            subject.closed = True
        return subject.closed
    subject.end_inspection = Mock(side_effect=end)
    subject.abort = Mock(side_effect=lambda: setattr(subject, 'closed', True))
    subject.wait_closed = Mock(side_effect=lambda: setattr(subject, 'closed', True))
    return subject


@pytest.mark.parametrize('pending,error,force', [(False, False, False), (True, False, False),
    (True, False, True), (False, True, False), (False, True, True)])
def test_group_retains_unacknowledged_handles_and_force_is_explicit(pending, error, force):
    item = handle(pending=pending, error=error)
    group = InspectionGroup((item,))
    assert group.active
    assert group.close(force=force) == (force or not pending and not error)
    assert group.active == (not force and (pending or error))
    assert item.abort.call_count == int(force and (pending or error))
    if not group.active:
        assert group.close()
        assert item.end_inspection.call_count == 1


def result(items=(), *, cancelled=False, persisted=True):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    execution = IntegrationRunResult(130 if cancelled else 0, (TestResult(test, 'passed'),), '', cancelled, items)
    return SimpleNamespace(execution=execution, persisted=persisted)


def test_terminal_reports_results_before_waiting_for_windows():
    lines = []
    item = handle()
    def wait():
        assert 'Integration results: PASS' in lines[0]
        assert lines[1] == 'Allure publication: PASS'
        assert 'inspection remains open' in lines[2]
        item.closed = True
    item.wait_closed = Mock(side_effect=wait)
    finish_terminal_inspection(result((item,)), lines.append)
    assert item.wait_closed.call_count == 1
    assert item.end_inspection.call_count == 0


@pytest.mark.parametrize('cancelled,persisted', [(False, True), (True, False)])
def test_terminal_without_inspection_reports_status(cancelled, persisted):
    lines = []
    finish_terminal_inspection(result(cancelled=cancelled, persisted=persisted), lines.append)
    assert ('CANCELLED' if cancelled else 'PASS') in lines[0]
    assert lines[1] == 'Allure publication: ' + ('PASS' if persisted else 'FAIL')
    assert len(lines) == 2
    finish_terminal_inspection(SimpleNamespace(), lines.append)
    assert len(lines) == 2


def test_terminal_keyboard_interrupt_synchronously_closes_own_group():
    item = handle(pending=True)
    item.wait_closed = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        finish_terminal_inspection(result((item,)), lambda line: None)
    assert item.abort.call_count == 1 and item.closed


def test_terminal_unconfirmed_cleanup_cannot_return_success():
    item = handle(pending=True)
    item.wait_closed = Mock()
    item.abort = Mock()
    lines = []
    from api_agent.providers.control import CleanupPendingError
    with pytest.raises(CleanupPendingError) as caught:
        finish_terminal_inspection(result((item,)), lines.append)
    assert caught.value.inspections == (item,)
    assert 'cleanup remains pending' in lines[-1]
    assert not item.closed
    async def retry():
        with pytest.raises(CleanupPendingError):
            await caught.value.retry()
        item.abort.side_effect = lambda: setattr(item, 'closed', True)
        await caught.value.retry()
    import asyncio
    asyncio.run(retry())


def test_force_cleanup_attempts_siblings_even_when_abort_fails():
    first, second = handle(pending=True), handle()
    first.abort.side_effect = RuntimeError('private cleanup details')
    group = InspectionGroup((first, second))
    assert group.close(force=True) is False
    assert first.abort.call_count == 1 and not first.closed
    assert second.closed


def test_second_interrupt_is_deferred_until_all_owned_siblings_close():
    first, second = handle(), handle()
    first.wait_closed.side_effect = KeyboardInterrupt
    first.end_inspection.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt) as error:
        finish_terminal_inspection(result((first, second)), lambda line: None)
    assert first.closed and second.closed
    assert first.abort.call_count == 1
    assert error.value.inspection_group.handles == (first, second)


def test_terminal_lost_worker_has_nonzero_completion_receipt():
    item = handle()
    item.closed = item.lost = True
    lines = []
    assert finish_terminal_inspection(result((item,)), lines.append) is False
    assert 'worker exited unexpectedly' in lines[-1]


def test_abort_interrupt_keeps_failed_owner_and_continues_other_cleanup():
    first, second = handle(pending=True), handle()
    first.abort.side_effect = KeyboardInterrupt
    group = InspectionGroup((first, second))
    with pytest.raises(KeyboardInterrupt) as error:
        group.close(force=True)
    assert not first.closed and second.closed
    assert error.value.inspection_group is group


@pytest.mark.parametrize('failure', [KeyboardInterrupt, BrokenPipeError])
@pytest.mark.parametrize('line_number', [1, 3])
def test_output_failure_closes_owned_inspection_and_retains_receipt(failure, line_number):
    item = handle(pending=True)
    calls = []
    def output(line):
        calls.append(line)
        if len(calls) == line_number:
            raise failure()
    with pytest.raises(failure) as caught:
        finish_terminal_inspection(result((item,)), output)
    assert item.closed
    assert caught.value.inspections == (item,)
    assert caught.value.inspection_group.handles == (item,)


def test_failed_cleanup_diagnostic_output_retains_retry_group():
    item = handle(pending=True)
    item.wait_closed = Mock()
    item.abort.side_effect = RuntimeError('private cleanup detail')
    def output(line):
        if line.startswith('Inspection cleanup:'):
            raise BrokenPipeError()
    with pytest.raises(BrokenPipeError) as caught:
        finish_terminal_inspection(result((item,)), output)
    assert not item.closed
    assert caught.value.inspections == (item,)
    item.abort.side_effect = lambda: setattr(item, 'closed', True)
    assert caught.value.inspection_group.close(force=True)


def test_bootstrap_keeps_terminal_pending_receipt_in_process(monkeypatch):
    from pathlib import Path
    from unittest.mock import patch
    from api_agent.runtime.bootstrap import main
    item = handle(pending=True)
    pending = InspectionGroup((item,)).cleanup_receipt()
    with patch.dict('os.environ', {}, clear=True), \
         patch('api_agent.config.read_dotenv', return_value={}), \
         patch('api_agent.runtime.worktrees.verify_worktrees'), \
         patch('sys.stdin.isatty', return_value=True), \
         patch('sys.stdout.isatty', return_value=True), \
         patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
         patch('api_agent.testing.collection.collect_tests', return_value=[]), \
         patch('api_agent.testing.launched.run_launched_integration', side_effect=pending):
        from api_agent.providers.control import CleanupPendingError
        with pytest.raises(CleanupPendingError) as caught:
            main(['--tui-mode=integration', '--no-std-in', '--start=True', '--debug', '--allure-s3-bucket=synthetic'], base_root=Path(__file__).parents[2])
    assert caught.value is pending and caught.value.inspections == (item,)


@pytest.mark.parametrize('failure', [KeyboardInterrupt, BrokenPipeError])
def test_bootstrap_does_not_drop_active_primary_cleanup_receipt(failure):
    from pathlib import Path
    from unittest.mock import patch
    from api_agent.runtime.bootstrap import main
    item = handle(pending=True)
    primary = failure()
    primary.inspection_group = InspectionGroup((item,))
    with patch.dict('os.environ', {}, clear=True), \
         patch('api_agent.config.read_dotenv', return_value={}), \
         patch('api_agent.runtime.worktrees.verify_worktrees'), \
         patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
         patch('api_agent.testing.collection.collect_tests', return_value=[]), \
         patch('api_agent.testing.launched.run_launched_integration', side_effect=primary):
        with pytest.raises(failure) as caught:
            main(['--tui-mode=integration', '--no-std-in', '--start=True', '--debug', '--allure-s3-bucket=synthetic'],
                 base_root=Path(__file__).parents[2])
    assert caught.value is primary and caught.value.inspection_group.active


@pytest.mark.parametrize('disposition', ['closed', 'pending', 'recording', 'recording_done'])
def test_bootstrap_pending_ownership_classification(disposition):
    from api_agent.runtime.bootstrap import _retains_cleanup
    error = KeyboardInterrupt()
    if disposition == 'closed':
        item = handle()
        item.closed = True
        error.inspection_group = InspectionGroup((item,))
    elif disposition == 'pending':
        error.cleanup_pending = InspectionGroup().cleanup_receipt()
    else:
        error.recording_prerequisites = SimpleNamespace(pending_cleanup=object() if disposition == 'recording' else None)
    assert _retains_cleanup(error) == (disposition in {'pending', 'recording'})
