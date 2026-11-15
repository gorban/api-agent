"""Cancellation retains late direct runner cleanup duties."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.lifecycle import TestRunLifecycle
from test_tui import fixture


def test_cancelled_runner_late_direct_receipt_is_retryable(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    entered, release = threading.Event(), threading.Event()
    calls = []
    async def retry():
        calls.append('retried')
    receipt = CleanupPendingError(('native-owner',), retry)
    execution = IntegrationRunResult(0, (TestResult(test, 'passed'),), '')
    receipt.recorded_result = SimpleNamespace(execution=execution, persisted=True, exit_code=0, usage={})
    def runner(*args, **options):
        entered.set()
        assert release.wait(5)
        assert options['cancel_event'].is_set()
        raise receipt
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
            assert not task.done() and app.collection_cancel.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert app.unit_result is execution and app.integration_persisted
            assert app.operation_status == 130 and app.inspection_cleanup_active()
            assert caught.value.cleanup_pending.inspection_group is app.inspections
            assert calls == []
            await caught.value.cleanup_pending.retry()
            assert calls == ['retried'] and not app.inspection_cleanup_active()
            assert app.operation_status == 130
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_cancelled_runner_preserves_distinct_interruption_and_late_receipts(tmp_path, monkeypatch):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def late_retry():
        calls.append('late')
    async def interruption_retry():
        calls.append('interruption')
    late = CleanupPendingError(('late-owner',), late_retry)
    independent = CleanupPendingError(('interrupt-owner',), interruption_retry)
    interruption = asyncio.CancelledError()
    interruption.cleanup_pending = independent
    def runner(*args, **kwargs):
        raise late
    async def offload(action, *args, **kwargs):
        try:
            action()
        except CleanupPendingError:
            raise interruption
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            app.selection.preselect((test.id,))
            app.test_run = TestRunLifecycle()
            app.collection_cancel = app.test_run.cancel_event
            with monkeypatch.context() as patcher:
                patcher.setattr('api_agent.runtime.tasks.offload', offload)
                with pytest.raises(asyncio.CancelledError) as caught:
                    await app._run_integrations()
            assert caught.value is interruption and app.inspection_cleanup_active()
            # Use the group directly: this test's offload stub targets execution only.
            import asyncio as aio
            await aio.to_thread(app.inspections.close, force=True)
            assert calls.count('late') == 1 and calls.count('interruption') == 1
            assert not app.inspection_cleanup_active() and app.operation_status == 130
    asyncio.run(run())


def test_cancelled_runner_retains_existing_interruption_group_and_both_receipts(tmp_path, monkeypatch):
    from api_agent.testing.inspection_controls import InspectionGroup
    from test_inspection_controls import handle

    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def late_retry():
        calls.append('late')
    async def interruption_retry():
        calls.append('interruption')
    late = CleanupPendingError(('late-owner',), late_retry)
    independent = CleanupPendingError(('interrupt-owner',), interruption_retry)
    item = handle()
    original_group = InspectionGroup((item,))
    interruption = asyncio.CancelledError()
    interruption.cleanup_pending = independent
    interruption.inspection_group = original_group
    def runner(*args, **kwargs):
        raise late
    apps = []
    async def offload(action, *args, **kwargs):
        if 'cancellation' not in kwargs:
            return await asyncio.to_thread(action, *args, **kwargs)
        from textual.widgets import DataTable
        deadline = asyncio.get_running_loop().time() + 5
        while not (apps[0].screen.kind == 'test-results' and apps[0].screen.query(DataTable)):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.01)
        try:
            action()
        except CleanupPendingError:
            raise interruption
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        apps.append(app)
        app.integration_runner = runner
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(.2)
            app.selection.preselect((test.id,))
            app.test_run = TestRunLifecycle()
            app.collection_cancel = app.test_run.cancel_event
            with monkeypatch.context() as patcher:
                patcher.setattr('api_agent.runtime.tasks.offload', offload)
                with pytest.raises(asyncio.CancelledError) as caught:
                    await app._run_integrations()
            assert caught.value is interruption
            assert any(owned is item for owned in app.inspections.handles)
            assert app.inspection_cleanup_active() and not item.closed
            await caught.value.cleanup_pending.retry()
            assert item.closed and not app.inspection_cleanup_active()
            assert calls.count('late') == 1 and calls.count('interruption') == 1
            assert app.operation_status == 130
    asyncio.run(run())
