"""Retained TUI results keep credential and cleanup ownership."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from api_agent.providers.control import CleanupPendingError
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.selection import SelectionModel
from test_tui import fixture


@pytest.mark.parametrize('allow', [False, True])
def test_result_details_scrub_registered_runtime_credential_in_display_and_copy(tmp_path, allow):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    secret, narrative = 'synthetic-runtime-secret', 'synthetic-result-narrative'
    def runner(*args, **options):
        options['evidence_boundary'].register_secret(secret)
        row = TestResult(test, 'failed', traceback=narrative + ' ' + secret,
                         local_diagnostic=narrative + ' ' + secret,
                         local_stderr=narrative + ' ' + secret,
                         local_stdout=narrative + ' ' + secret)
        return SimpleNamespace(execution=IntegrationRunResult(1, (row,), ''),
                               persisted=True, exit_code=1, usage={})
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
        }, argv=['--log-content=True' if allow else '--log-content=False'])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            app.toggle_row(test.id)
            await pilot.pause()
            body_widget = app.screen.query_one('#body', Static)
            body = str(body_widget.content)
            app.screen.selections = {body_widget: Selection(Offset(0, 0), None)}
            copied, selected = app.screen.copy_target()
            assert selected and copied is not None
            assert (narrative in body) is allow and (narrative in copied) is allow
            assert secret not in body and secret not in copied
            if allow:
                assert '[REDACTED]' in body
    asyncio.run(run())


def test_direct_cleanup_receipt_with_finalized_result_remains_retryable(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def retry():
        calls.append('retried')
    receipt = CleanupPendingError((object(),), retry)
    execution = IntegrationRunResult(0, (TestResult(test, 'passed'),), '')
    receipt.recorded_result = SimpleNamespace(execution=execution, persisted=True, exit_code=0, usage={})
    def runner(*args, **kwargs):
        raise receipt
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'test-results'
            assert app.unit_result is execution and app.integration_persisted
            assert app.inspection_cleanup_active() and app.operation_status == 3
            assert calls == []
            assert await app.end_inspection(force=True)
            assert calls == ['retried'] and not app.inspection_cleanup_active()
            assert app.operation_status == 0
    asyncio.run(run())


def test_distinct_native_and_reporting_receipts_each_retry_once(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def retry_native():
        calls.append('native')
    async def retry_reporting():
        calls.append('reporting')
    native = CleanupPendingError(('native-owner',), retry_native)
    reporting = CleanupPendingError(('reporting-owner',), retry_reporting)
    native.cleanup_pending = reporting
    native.recording_prerequisites = SimpleNamespace(pending_cleanup=reporting)
    execution = IntegrationRunResult(0, (TestResult(test, 'passed'),), '')
    native.recorded_result = SimpleNamespace(execution=execution, persisted=True, exit_code=0, usage={})
    def runner(*args, **kwargs):
        raise native
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.inspection_cleanup_active() and app.operation_status == 3
            assert await app.end_inspection(force=True)
            assert calls.count('native') == 1 and calls.count('reporting') == 1
            assert not app.inspection_cleanup_active() and app.operation_status == 0
    asyncio.run(run())


def test_recording_successor_remains_owned_and_is_not_retried_by_native_group(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    calls = []
    async def retry_native():
        calls.append('native')
    async def retry_successor():
        calls.append('successor')
    successor = CleanupPendingError(('successor',), retry_successor)
    recording = SimpleNamespace(pending_cleanup=None)
    native = CleanupPendingError(('native',), retry_native)
    async def retry_reporting():
        calls.append('reporting')
        recording.pending_cleanup = successor
        native.cleanup_pending = successor
    reporting = CleanupPendingError(('reporting',), retry_reporting)
    recording.pending_cleanup = reporting
    native.cleanup_pending = reporting
    native.recording_prerequisites = recording
    execution = IntegrationRunResult(0, (TestResult(test, 'passed'),), '')
    native.recorded_result = SimpleNamespace(execution=execution, persisted=True, exit_code=0, usage={})
    def runner(*args, **kwargs):
        raise native
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            assert not await app.end_inspection(force=True)
            assert calls.count('reporting') == 1 and calls.count('native') == 1
            assert 'successor' not in calls
            assert app.recording_cleanup is recording and recording.pending_cleanup is successor
            assert app.inspection_cleanup_active() and app.operation_status == 3
            assert await app.end_inspection(force=True)
            assert calls.count('successor') == 1 and not app.inspection_cleanup_active()
            assert app.operation_status == 0
    asyncio.run(run())
