"""Retained TUI content protects credentials retired before presentation."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.selection import SelectionModel
from test_tui import fixture


@pytest.mark.parametrize('failure', [False, True])
def test_retired_scoped_runtime_secret_stays_hidden_in_retained_tui_text(tmp_path, failure):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    secret, narrative = 'synthetic-scoped-runtime-secret', 'synthetic-scoped-narrative'
    def runner(*args, **options):
        owner = options['evidence_boundary'].credential_owner()
        owner.register(secret, slot='credential')
        owner.close()
        if failure:
            raise RuntimeError(narrative + ' ' + secret)
        row = TestResult(test, 'failed', traceback=narrative + ' ' + secret,
                         local_diagnostic=narrative + ' ' + secret,
                         local_stderr=narrative + ' ' + secret)
        return SimpleNamespace(execution=IntegrationRunResult(1, (row,), ''),
                               persisted=True, exit_code=1, usage={})
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
        }, argv=['--log-content=True'])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            if not failure:
                app.toggle_row(test.id)
                await pilot.pause()
            body_widget = app.screen.query_one('#body', Static)
            body = str(body_widget.content)
            if not failure:
                app.screen.selections = {body_widget: Selection(Offset(0, 0), None)}
            copied, _ = app.screen.copy_target()
            assert narrative in body and narrative in copied
            assert secret not in body and secret not in copied
            assert '[REDACTED]' in body
            history = app.integration_redaction_history
            assert history is not None
        with pytest.raises(ValueError, match='closed'):
            history.snapshot()
    asyncio.run(run())


def test_lazy_exception_string_credential_is_captured_before_redaction_snapshot(tmp_path):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    secret, narrative = 'synthetic-late-scoped-secret', 'synthetic-late-narrative'
    class LazyFailure(RuntimeError):
        def __init__(self, boundary):
            self.boundary = boundary
        def __str__(self):
            owner = self.boundary.credential_owner()
            owner.register(secret, slot='credential')
            owner.close()
            return narrative + ' ' + secret
    def runner(*args, **options):
        raise LazyFailure(options['evidence_boundary'])
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
        }, argv=['--log-content=True'])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            body = str(app.screen.query_one('#body', Static).content)
            copied, _ = app.screen.copy_target()
            assert narrative in body and narrative in copied
            assert secret not in body and secret not in copied
            assert '[REDACTED]' in body
    asyncio.run(run())


@pytest.mark.parametrize('failure_mode', ['creation', 'closed_error', 'closed_details'])
def test_unavailable_history_withholds_tui_content(tmp_path, monkeypatch, failure_mode):
    from api_agent.telemetry.privacy import EmissionBoundary

    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    secret, narrative = 'synthetic-unavailable-secret', 'synthetic-unavailable-narrative'
    if failure_mode == 'creation':
        def fail_creation(self):
            raise RuntimeError('history creation unavailable')
        monkeypatch.setattr(EmissionBoundary, 'retain_redaction_history', fail_creation)

    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
        }, argv=['--log-content=True'])
        def runner(*args, **options):
            owner = options['evidence_boundary'].credential_owner()
            owner.register(secret, slot='credential')
            owner.close()
            if failure_mode != 'creation':
                app.integration_redaction_history.close()
            if failure_mode != 'closed_details':
                raise RuntimeError(narrative + ' ' + secret)
            row = TestResult(test, 'failed', traceback=narrative + ' ' + secret,
                             local_diagnostic=narrative + ' ' + secret, failure_phase='call')
            return SimpleNamespace(execution=IntegrationRunResult(1, (row,), ''),
                                   persisted=True, exit_code=1, usage={})
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            if failure_mode == 'closed_details':
                app.toggle_row(test.id)
                await pilot.pause()
            body_widget = app.screen.query_one('#body', Static)
            body = str(body_widget.content)
            if failure_mode == 'closed_details':
                app.screen.selections = {body_widget: Selection(Offset(0, 0), None)}
            copied, _ = app.screen.copy_target()
            assert narrative not in body + copied and secret not in body + copied
            assert 'withheld' in body
            if failure_mode == 'closed_details':
                assert test.id in body and 'Failure phase: call' in body
                assert 'Failure phase: call' in copied and 'withheld' in copied
            elif failure_mode == 'closed_error':
                assert 'credential protection unavailable' in body
    asyncio.run(run())
