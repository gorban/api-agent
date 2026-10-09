"""Consumer transitions retain inspection and recording ownership until acknowledgement."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.inspection_controls import InspectionGroup
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.selection import SelectionModel
from test_inspection_controls import handle
from test_tui import fixture


def test_terminal_lost_worker_overrides_successful_publication(monkeypatch, capsys):
    from api_agent.runtime.bootstrap import main
    import os
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *a, **k: ())
    item = handle()
    item.closed = item.lost = True
    recorded = SimpleNamespace(execution=IntegrationRunResult(0, (), '', inspections=(item,)),
                               persisted=True, exit_code=0)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', lambda *a, **k: recorded)
    monkeypatch.setattr(os, 'environ', {})
    status = main(['--tui-mode=integration', '--no-std-in', '--debug',
                   '--allure-s3-bucket=synthetic'], base_root=Path(__file__).resolve().parents[2])
    assert status == 3
    evidence = capsys.readouterr().out
    assert 'Allure publication: PASS' in evidence and 'worker exited unexpectedly' in evidence


def test_back_to_selection_waits_for_inspection_ack(tmp_path):
    async def run():
        app = fixture(tmp_path)
        item = handle(pending=True)
        app.inspections = InspectionGroup((item,))
        identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
        app.selection = SelectionModel((identity,))
        app.show = Mock()
        app.discover_tests = AsyncMock(return_value=(identity,))
        await app.back_to_selection()
        app.discover_tests.assert_not_awaited()
        assert app.inspections.active and app.operation_status == 3
    asyncio.run(run())


@pytest.mark.parametrize('cancelled,items', [(False, 'empty'), (False, 'duplicates'), (True, 'duplicates')])
def test_recorded_error_preserves_receipt_and_deduplicates_handles(tmp_path, cancelled, items):
    async def run():
        app = fixture(tmp_path, 'integration')
        identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
        app.selection = SelectionModel((identity,))
        app.selection.preselect((identity.id,))
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.discover_tests = AsyncMock(return_value=(identity,))
        app.show_unit_results = Mock()
        first, second = handle(), handle()
        execution = IntegrationRunResult(0, (TestResult(identity, 'passed'),), '',
            inspections=() if items == 'empty' else (first, second, first))
        recorded = SimpleNamespace(execution=execution, exit_code=0, persisted=True)
        error = RuntimeError('private publication details')
        error.inspections = (first,)
        error.recorded_result = recorded
        def runner(*args, **kwargs):
            if cancelled:
                app.collection_cancel.set()
            raise error
        app.integration_runner = runner
        await app._run_integrations()
        assert not app.test_run_active()
        assert app.unit_result is execution and app.integration_persisted
        assert app.inspection_failed and app.operation_status == 3
        assert app.inspections.handles == ((first,) if items == 'empty' else (first, second))
        assert app.show_unit_results.call_count == (1 if cancelled else 2)
        assert await app.end_inspection(force=True)
    asyncio.run(run())


def test_recording_receipt_failed_retry_keeps_same_owner_until_success(tmp_path):
    async def run():
        app = fixture(tmp_path)
        retry = AsyncMock(side_effect=RuntimeError('private store details'))
        recording = SimpleNamespace(pending_cleanup=CleanupPendingError((), retry))
        app.recording_cleanup = recording
        receipt = app.inspection_cleanup_receipt()
        with pytest.raises(CleanupPendingError) as caught:
            await receipt.retry()
        assert caught.value is receipt and app.recording_cleanup is recording
        assert recording in receipt.owners
        retry.side_effect = None
        await receipt.retry()
        assert app.recording_cleanup is None and app.pending_inspection_cleanup is None
        assert retry.await_count == 2
    asyncio.run(run())


def test_bound_receipt_keeps_later_recording_owner_with_same_inspection_group(tmp_path):
    async def run():
        app = fixture(tmp_path)
        later = SimpleNamespace(pending_cleanup=CleanupPendingError((), AsyncMock()))
        later_receipts = []
        async def replace():
            app.recording_cleanup = later
            app.pending_inspection_cleanup = app.inspection_cleanup_receipt()
            later_receipts.append(app.pending_inspection_cleanup)
            app.inspection_failed = True
            app.operation_status = 3
        first = SimpleNamespace(pending_cleanup=CleanupPendingError((), replace))
        app.recording_cleanup = first
        receipt = app.inspection_cleanup_receipt()
        await receipt.retry()
        assert app.recording_cleanup is later
        assert app.pending_inspection_cleanup is later_receipts[0]
        assert app.inspection_failed and app.operation_status == 3
        later.pending_cleanup.retry.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('receipt', [False, True])
def test_successful_disposal_preserves_durable_worker_loss(tmp_path, receipt):
    async def run():
        app = fixture(tmp_path)
        item = handle()
        def end():
            item.closed = item.lost = True
            return True
        item.end_inspection.side_effect = end
        app.inspections = InspectionGroup((item,))
        app.integration_exit_status = 0
        if receipt:
            await app.inspection_cleanup_receipt().retry()
        else:
            assert await app.end_inspection()
        assert item.closed and app.inspection_lost and app.inspection_failed
        assert app.operation_status == 3
    asyncio.run(run())


def test_continue_routes_to_owned_inspection_close_without_restarting_tests(tmp_path):
    async def run():
        app = fixture(tmp_path, 'unit')
        app.mode = 'unit'
        identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
        app.selection = SelectionModel((identity,))
        app.selection.preselect((identity.id,))
        app.inspections = InspectionGroup((handle(),))
        app.close_view = AsyncMock()
        app.start_unit_run = Mock()
        await app.choose('continue')
        app.close_view.assert_awaited_once()
        app.start_unit_run.assert_not_called()
        assert app.inspections.active
    asyncio.run(run())


def test_quit_retains_view_until_forced_cleanup_ack(tmp_path):
    async def run():
        app = fixture(tmp_path)
        item = handle(pending=True)
        item.abort.side_effect = RuntimeError('owned process still alive')
        app.inspections = InspectionGroup((item,))
        app.exit = Mock()
        await app.confirm_quit()
        app.exit.assert_not_called()
        assert app.inspections.active and app.operation_status == 3
    asyncio.run(run())


def test_unmount_drains_active_tests_before_inspection_cleanup(tmp_path):
    async def run():
        app = fixture(tmp_path)
        order = []
        app.test_run = SimpleNamespace(active=True)
        async def stop():
            order.append('tests-stopped')
            app.test_run.active = False
        app.test_run.stop_and_wait = AsyncMock(side_effect=stop)
        async def end(**kwargs):
            assert app.collection_cancel.is_set() and not app.test_run_active()
            assert kwargs['force']
            order.append('inspection-ended')
            return True
        app.end_inspection = AsyncMock(side_effect=end)
        await app.on_unmount()
        assert order == ['tests-stopped', 'inspection-ended']
    asyncio.run(run())


def test_stopped_tests_do_not_navigate_away_from_pending_inspection(tmp_path):
    async def run():
        app = fixture(tmp_path)
        app.test_run = SimpleNamespace(stop_and_wait=AsyncMock())
        app.end_inspection = AsyncMock(return_value=False)
        app.show_menu = Mock()
        await app.stop_tests_to_menu(True)
        app.test_run.stop_and_wait.assert_awaited_once()
        app.end_inspection.assert_awaited_once_with(force=True)
        app.show_menu.assert_not_called()
        assert app.operation_status == 130
    asyncio.run(run())
