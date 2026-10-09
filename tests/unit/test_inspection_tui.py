import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import Static
from api_agent.providers.control import CleanupPendingError
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult, RunnerEvent
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.inspection_controls import InspectionGroup
from test_tui import fixture
from test_inspection_controls import handle


def test_retained_inspection_results_are_ready_and_explicit_end_is_available(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    item = handle()
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = lambda *args, **kw: SimpleNamespace(
            execution=IntegrationRunResult(0, (TestResult(test, 'passed'),), '', inspections=(item,)),
            exit_code=0, persisted=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'test-results' and not app.test_run_active()
            assert app.operation_status == 0 and app.inspections.active
            assert 'tests and publication have finished' in app.screen.query_one('#body', Static).content.plain
            assert app.screen.check_action('end_inspection', ())
            await pilot.press('e')
            await pilot.pause(.2)
            assert item.closed and not app.inspections.active
            assert not app.screen.check_action('end_inspection', ())
            assert app.operation_status == 0
    asyncio.run(run())


@pytest.mark.parametrize('confirm', [False, True])
def test_close_requires_acceptance_to_end_inspection(tmp_path, confirm):
    item = handle()
    async def run():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.inspections = InspectionGroup((item,))
            app.unit_result = IntegrationRunResult(0, (), '')
            app.integration_exit_status = 0
            app.show_unit_results()
            await pilot.pause(.1)
            await pilot.press('c')
            await pilot.pause(.1)
            assert not item.closed
            await pilot.press('y' if confirm else 'escape')
            if confirm:
                await pilot.press('enter')
            await pilot.pause(.2)
            assert item.closed == confirm
            assert app.screen.kind == ('menu' if confirm else 'test-results')
    asyncio.run(run())


def test_unmount_ends_owned_inspection(tmp_path):
    item = handle()
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.inspections = InspectionGroup((item,))
        assert item.closed
    asyncio.run(run())


@pytest.mark.parametrize('event', ['inspection_closed', 'inspection_lost', 'inspection_cleanup_pending'])
def test_inspection_lifecycle_events_do_not_modify_test_outcomes(tmp_path, event):
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.show_unit_results()
            await pilot.pause(.1)
            app.unit_event(RunnerEvent(event))
            await pilot.pause(.1)
            assert app.unit_result.exit_code == 0
            assert app.inspection_failed == (event != 'inspection_closed')
            assert app.inspection_lost == (event == 'inspection_lost')
    asyncio.run(run())


def test_cleanup_retry_retains_owner_and_recovers_original_status(tmp_path):
    item = handle(pending=True)
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.integration_exit_status = 0
            app.inspections = InspectionGroup((item,))
            app.show_unit_results()
            await pilot.pause(.1)
            assert not await app.end_inspection()
            assert app.operation_status == 3 and app.inspections.active
            assert 'cleanup pending' in app.screen.body
            item.end_inspection.side_effect = lambda: setattr(item, 'closed', True) or True
            assert await app.end_inspection()
            assert app.operation_status == 0 and not app.inspection_failed
    asyncio.run(run())


@pytest.mark.parametrize('retry_fails', [True, False])
def test_recording_pending_result_retains_cleanup_and_publication(tmp_path, retry_fails):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def retry():
        calls.append('retry')
        if retry_fails:
            raise RuntimeError('private rollback details')
    pending = CleanupPendingError((), retry)
    recorded = SimpleNamespace(execution=IntegrationRunResult(0, (TestResult(test, 'passed'),), ''),
                               persisted=True, exit_code=0)
    pending.recorded_result = recorded
    pending.recording_prerequisites = SimpleNamespace(pending_cleanup=pending)
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = Mock(side_effect=pending)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'test-results' and app.integration_persisted
            assert app.unit_result.tests[0].status == 'passed'
            assert app.recording_cleanup is pending.recording_prerequisites
            await pilot.press('e')
            await pilot.pause(.2)
            assert calls == ['retry']
            assert (app.recording_cleanup is not None) == retry_fails
            assert app.operation_status == (3 if retry_fails else 0)
            assert 'private rollback details' not in app.screen.body
    asyncio.run(run())


def test_transient_pending_clears_after_natural_close_without_falsifying_runner_status(tmp_path):
    item = handle()
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.integration_persisted = True
            app.integration_exit_status = 0
            app.inspections = InspectionGroup((item,))
            app.show_unit_results()
            await pilot.pause(.1)
            app.unit_event(RunnerEvent('inspection_cleanup_pending'))
            assert app.operation_status == 3
            assert 'runner or reporting failed' not in app.screen.body
            item.closed = True
            app.unit_event(RunnerEvent('inspection_closed'))
            await pilot.pause(.1)
            assert app.operation_status == 0 and not app.inspection_failed
            assert not app.screen.check_action('end_inspection', ())
    asyncio.run(run())


def test_lost_receipt_is_seen_before_callback_delivery(tmp_path):
    item = handle()
    item.closed = item.lost = True
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.integration_exit_status = app.operation_status = 0
            app.inspections = InspectionGroup((item,))
            assert await app.end_inspection()
            assert app.inspection_lost and app.inspection_failed and app.operation_status == 3
    asyncio.run(run())


def test_unexpected_unmount_retains_failed_group_and_retry(tmp_path):
    item = handle(pending=True)
    item.abort.side_effect = RuntimeError('private cleanup detail')
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.inspections = InspectionGroup((item,))
            app.integration_exit_status = 0
        pending = app.pending_inspection_cleanup
        assert isinstance(pending, CleanupPendingError)
        assert pending.inspections == (item,)
        assert not item.closed
        with pytest.raises(CleanupPendingError) as retry_error:
            await pending.retry()
        assert retry_error.value.inspection_group is app.inspections
        item.abort.side_effect = lambda: setattr(item, 'closed', True)
        await retry_error.value.retry()
        assert item.closed and app.pending_inspection_cleanup is None
    asyncio.run(run())


def test_closed_group_and_late_events_do_not_change_next_operation(tmp_path):
    item = handle()
    item.closed = item.lost = True
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.inspections = InspectionGroup((item,))
            generation = app._inspection_generation
            app.show_menu()
            await pilot.pause(.1)
            app.mode = 'unit'
            app.operation_status = 0
            app.integration_event(RunnerEvent('inspection_lost'), generation)
            assert not app.inspections.handles and app.operation_status == 0
            assert not app.inspection_lost
            assert await app.end_inspection()
            assert app.operation_status == 0
    asyncio.run(run())


def test_generic_runner_error_keeps_owned_inspections(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    item = handle()
    error = RuntimeError('private runner detail')
    error.inspections = (item, item)
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = Mock(side_effect=error)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'error'
            assert app.inspections.handles == (item,) and app.inspections.active
            assert 'private runner detail' not in app.screen.body
            assert await app.end_inspection(force=True)
            assert item.closed
    asyncio.run(run())


def test_pending_handles_survive_pretransfer_sibling_events(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    first, second = handle(pending=True), handle()
    first.pending = True
    second.closed = True
    def runner(*args, **kwargs):
        kwargs['on_event'](RunnerEvent('inspection_cleanup_pending'))
        kwargs['on_event'](RunnerEvent('inspection_closed'))
        return SimpleNamespace(execution=IntegrationRunResult(0, (TestResult(test, 'passed'),), '',
                                                             inspections=(first, second)),
                               exit_code=0, persisted=True)
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.inspections.active and app.inspection_failed and app.operation_status == 3
    asyncio.run(run())


def test_end_inspection_thread_callback_can_reenter_app(tmp_path):
    item = handle()
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.integration_exit_status = 0
            app.inspections = InspectionGroup((item,))
            app.show_unit_results()
            await pilot.pause(.1)
            def end():
                item.closed = True
                app.call_from_thread(app.integration_event, RunnerEvent('inspection_closed'), app._inspection_generation)
                return True
            item.end_inspection.side_effect = end
            assert await app.end_inspection()
    asyncio.run(run())


def test_run_tui_exports_unmount_cleanup_receipt(monkeypatch):
    from api_agent.tui import app as module
    item = handle(pending=True)
    pending = CleanupPendingError((item,), Mock())
    pending.inspections = (item,)
    subject = SimpleNamespace(run=lambda: 0, pending_inspection_cleanup=pending)
    monkeypatch.setattr(module, 'ApiAgentApp', lambda *args: subject)
    with pytest.raises(CleanupPendingError) as caught:
        module.run_tui(None, None)
    assert caught.value is pending and caught.value.inspections == (item,)


def test_closed_event_preserves_durable_sibling_loss(tmp_path):
    item = handle()
    item.closed = item.lost = True
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.inspections = InspectionGroup((item,))
            app.operation_status = 0
            app.integration_event(RunnerEvent('inspection_closed'), app._inspection_generation)
            assert app.inspection_lost and app.inspection_failed and app.operation_status == 3
    asyncio.run(run())


def test_recording_shutdown_receipt_keeps_prerequisite_until_retry(tmp_path):
    async def retry():
        raise RuntimeError('private manager detail')
    pending = CleanupPendingError((), retry)
    prerequisite = SimpleNamespace(pending_cleanup=pending)
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.recording_cleanup = prerequisite
        receipt = app.pending_inspection_cleanup
        assert receipt.recording_prerequisites is prerequisite
        assert prerequisite in receipt.owners
        prerequisite.pending_cleanup.retry = AsyncMock()
        await receipt.retry()
        assert app.recording_cleanup is None and app.pending_inspection_cleanup is None
    from unittest.mock import AsyncMock
    asyncio.run(run())


def test_pending_group_blocks_menu_unit_start_and_discard(tmp_path):
    item = handle(pending=True)
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            group = app.inspections = InspectionGroup((item,))
            original_screen = app.screen
            app.show_menu()
            app.start_unit_run()
            app.discard_finished_inspection()
            assert app.screen is original_screen and app.inspections is group
            assert app.inspections.active
    asyncio.run(run())


@pytest.mark.parametrize('pending', [False, True])
def test_exceptional_tui_exit_exports_pending_receipt(monkeypatch, pending):
    from api_agent.tui import app as module
    item = handle(pending=True)
    group = InspectionGroup((item,))
    receipt = group.cleanup_receipt()
    primary = KeyboardInterrupt()
    def run(): raise primary
    subject = SimpleNamespace(run=run, pending_inspection_cleanup=receipt if pending else None)
    monkeypatch.setattr(module, 'ApiAgentApp', lambda *args: subject)
    with pytest.raises(KeyboardInterrupt) as caught:
        module.run_tui(None, None)
    assert caught.value is primary
    if pending:
        assert caught.value.cleanup_pending is receipt
        assert caught.value.inspection_group is group and caught.value.inspections == (item,)
    else:
        assert not hasattr(caught.value, 'cleanup_pending')


@pytest.mark.parametrize('failure', ['result', 'error', 'recorded_error', 'empty'])
def test_cancelled_executor_transfers_late_inspections_before_teardown(tmp_path, failure):
    import threading
    from unittest.mock import AsyncMock
    from api_agent.testing.lifecycle import TestRunLifecycle
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    item = handle()
    entered, release = threading.Event(), threading.Event()
    recorded = SimpleNamespace(execution=IntegrationRunResult(0, (TestResult(test, 'passed'),), '',
                                                             inspections=() if failure == 'empty' else (item,)),
                               exit_code=0, persisted=True)
    error = RuntimeError('private late runner detail')
    error.inspections = (item,)
    if failure == 'recorded_error':
        error.recorded_result = recorded
        error.recording_prerequisites = SimpleNamespace(pending_cleanup=CleanupPendingError((), AsyncMock()))
    def runner(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        assert kwargs['cancel_event'].is_set()
        if failure in {'error', 'recorded_error'}:
            raise error
        return recorded
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            app.selection.preselect((test.id,))
            app.test_run = TestRunLifecycle()
            app.collection_cancel = app.test_run.cancel_event
            task = asyncio.create_task(app._run_integrations())
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and app.test_run_active()
            assert app.collection_cancel.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert not app.test_run_active()
            assert app.inspections.handles == (() if failure == 'empty' else (item,))
            assert caught.value.inspection_group is app.inspections
            if failure != 'empty':
                assert caught.value.cleanup_pending.inspections == (item,)
            assert app.operation_status == 130
            assert await app.end_inspection(force=True)
            assert app.operation_status == 130
            if failure != 'empty':
                assert item.closed
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_old_cleanup_receipt_cannot_close_a_later_operation(tmp_path):
    first, second = handle(), handle()
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.inspections = InspectionGroup((first,))
            app.integration_exit_status = 0
            receipt = app.inspection_cleanup_receipt()
            await receipt.retry()
            assert first.closed
            app.discard_finished_inspection()
            app.inspections = InspectionGroup((second,))
            app.operation_status = 7
            await receipt.retry()
            assert not second.closed and app.operation_status == 7
            second.end_inspection.assert_not_called()
    asyncio.run(run())


def test_old_recording_receipt_cannot_release_later_prerequisites(tmp_path):
    from unittest.mock import AsyncMock
    first_retry, second_retry = AsyncMock(), AsyncMock()
    first = SimpleNamespace(pending_cleanup=CleanupPendingError((), first_retry))
    second = SimpleNamespace(pending_cleanup=CleanupPendingError((), second_retry))
    async def run():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.recording_cleanup = first
            receipt = app.inspection_cleanup_receipt()
            await receipt.retry()
            app.discard_finished_inspection()
            app.recording_cleanup = second
            await receipt.retry()
            assert app.recording_cleanup is second
            second_retry.assert_not_awaited()
            first_retry.assert_awaited()
    asyncio.run(run())


def test_retained_group_rejects_new_integration_executor(tmp_path):
    from api_agent.testing.lifecycle import TestRunLifecycle
    item = handle()
    async def run():
        app = fixture(tmp_path)
        app.show = Mock()
        app.inspections = InspectionGroup((item,))
        app.integration_runner = Mock()
        app.test_run = TestRunLifecycle()
        try:
            await app._run_integrations()
            assert app.inspections.handles == (item,) and not app.test_run_active()
            app.integration_runner.assert_not_called()
            assert app.show.call_args.args[:2] == ('error', 'Integration execution failed')
        finally:
            await app.end_inspection(force=True)
    asyncio.run(run())
