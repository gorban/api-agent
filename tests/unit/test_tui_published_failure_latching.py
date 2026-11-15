"""Preserve operation failures and runtime credential privacy in the TUI."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.testing.collection import collect_tests, TestIdentity
from api_agent.testing.selection import SelectionModel
from test_inspection_controls import handle
from test_integration_runner import context, fixture_repo
from test_recorded_integration import Storage
from test_tui import fixture


def test_published_readiness_failure_stays_failed_after_inspection_cleanup(tmp_path, monkeypatch):
    from api_agent.testing import recorded
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    storage = Storage()
    failure = OSError('readiness failure')
    item = handle()
    def fail(_):
        raise failure
    monkeypatch.setattr('api_agent.testing.inspection_gate.publish_manual_accounting_ready', fail)
    def runner(_context, tests, config, **options):
        try:
            return recorded.run_recorded_integration(ctx, tests,
                dict(config, ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path / 'artifacts',
                     TEST_PARALLEL_JOBS=0), accepted=True, storage=storage,
                content_policy=options['content_policy'], evidence_boundary=options.get('evidence_boundary'))
        except OSError as error:
            error.inspections = (item,)
            raise
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: selected)
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.selection = SelectionModel(selected)
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            deadline = asyncio.get_running_loop().time() + 20
            while app.test_run_active() and asyncio.get_running_loop().time() < deadline:
                await pilot.pause(.1)
            assert not app.test_run_active()
            assert app.screen.kind == 'test-results'
            assert app.unit_result.tests[0].status == 'passed' and app.integration_persisted
            assert app.integration_exit_status == 0 and app.operation_status != 0
            assert 'Test operation: FAIL' in str(app.screen.body)
            assert 'Allure publication: PASS' in str(app.screen.body)
            assert await app.end_inspection(force=True)
            assert item.closed and app.operation_status != 0
            assert 'Test operation: FAIL' in str(app.screen.body)
            prior = app.unit_result
            app.integration_runner = lambda *args, **kwargs: SimpleNamespace(
                execution=prior, persisted=True, exit_code=0, usage={})
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.operation_status == 0
            assert 'Test operation: FAIL' not in str(app.screen.body)
    asyncio.run(run())
    assert len(storage.calls) == 1


@pytest.mark.parametrize('allow', [False, True])
def test_tui_error_uses_runner_runtime_credential_boundary_for_display_and_copy(tmp_path, allow):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    narrative, secret = 'synthetic-run-narrative', 'synthetic-runtime-credential'
    observed = []
    def runner(*args, **options):
        boundary = options.get('evidence_boundary')
        observed.append(boundary)
        if boundary is not None:
            boundary.register_secret(secret)
        raise RuntimeError(narrative + ' ' + secret)
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
            assert app.screen.kind == 'error'
            body = str(app.screen.query_one('#body', Static).content)
            copied, _ = app.screen.copy_target()
            assert (narrative in body) is allow and (narrative in copied) is allow
            assert secret not in body and secret not in copied
            if allow:
                assert '[REDACTED]' in body
    asyncio.run(run())
    assert observed and observed[0] is not None
