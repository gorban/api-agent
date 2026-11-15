"""Published outcomes survive later failures without exposing denied content."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.testing import recorded
from api_agent.testing.collection import collect_tests, TestIdentity
from api_agent.testing.inspection_controls import finish_terminal_inspection
from test_integration_runner import context, fixture_repo
from test_recorded_integration import Storage
from test_tui import fixture


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_readiness_failure_retains_published_execution_and_usage(tmp_path, monkeypatch, mode):
    source = ('import pytest\n@pytest.mark.requires_owned_ui_session\ndef test_ok():\n    assert True\n'
              if mode == 'e2e' else 'def test_ok():\n    assert True\n')
    base = fixture_repo(tmp_path / 'base', source)
    ctx = context(base)
    ctx.suite_paths = lambda selected_mode: (base / 'tests/integration',)
    selected = collect_tests(ctx, mode)
    config = {'TUI_MODE': mode, 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'artifacts', 'TEST_PARALLEL_JOBS': 0,
              'E2E_RERUNS': 0}
    storage = Storage()
    failure = OSError('readiness write failed')
    def fail(inspections):
        raise failure
    monkeypatch.setattr('api_agent.testing.inspection_gate.publish_manual_accounting_ready', fail)
    with pytest.raises(OSError) as caught:
        recorded.run_recorded_integration(ctx, selected, config, accepted=True, storage=storage,
            prepare=lambda usage: usage.record_prompt(turn_id='turn', prompt_id='initial', kind='initial'))
    assert caught.value is failure
    result = failure.recorded_result
    assert result.execution is failure.execution and result.persisted
    assert result.execution.tests[0].status == 'passed'
    assert result.usage['prompt_count'] == 1
    assert len(storage.calls) == 1 and result.manifest is not None
    lines = []
    assert finish_terminal_inspection(result, lines.append)
    assert sum('results:' in line for line in lines) == 1
    assert any('1/1 passed' in line for line in lines)
    assert any('Allure publication: PASS' in line for line in lines)


@pytest.mark.parametrize('allow', [False, True])
def test_tui_pre_recording_error_display_and_copy_obey_content_policy(tmp_path, allow):
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    narrative = 'synthetic-private-narrative'
    secret = 'synthetic-registered-credential'
    def runner(*args, **kwargs):
        raise ValueError(f'{narrative} {secret}')
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
            'SYNTHETIC_API_KEY': {'type': 'str', 'default': secret},
        }, argv=['--log-content=True' if allow else '--log-content=False'])
        app.integration_runner = runner
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            from api_agent.testing.selection import SelectionModel
            app.selection = SelectionModel((test,))
            app.show_selection()
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'error'
            body = str(app.screen.query_one('#body', Static).content)
            copied, _ = app.screen.copy_target()
            assert secret not in body and secret not in copied
            assert (narrative in body) is allow
            assert (narrative in copied) is allow
            if not allow:
                assert 'invalid_value' in body and '[withheld: LOG_CONTENT is disabled]' in body
            assert app.operation_status == 3
    asyncio.run(run())


def test_tui_error_before_config_resolution_defaults_to_denied(tmp_path):
    narrative = 'synthetic-early-private-narrative'
    async def run():
        app = fixture(tmp_path, 'integration')
        async def fail(*args):
            raise RuntimeError(narrative)
        app.discover_tests = fail
        app.finish_test_run = lambda: None
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            await app._run_integrations()
            await pilot.pause()
            body = str(app.screen.query_one('#body', Static).content)
            copied, _ = app.screen.copy_target()
            assert narrative not in body and narrative not in copied
            assert '[withheld: LOG_CONTENT is disabled]' in body
    asyncio.run(run())


def test_unrecorded_usage_output_failure_keeps_original_cleanup_receipt(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing import integration
    from api_agent.testing.launched import run_launched_integration
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    execution = SimpleNamespace(exit_code=0, tests=(), cancelled=False, inspections=())
    async def retry():
        pass
    pending = CleanupPendingError((), retry)
    pending.execution = execution
    monkeypatch.setattr(integration, 'run_integration_tests', Mock(side_effect=pending))
    output_failure = OSError('usage display failed')
    with pytest.raises(CleanupPendingError) as caught:
        run_launched_integration(ctx, (test,), {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'},
                                 accepted=True, usage_output=Mock(side_effect=output_failure))
    assert caught.value is pending
    assert pending.recorded_result.execution is execution
    assert pending.diagnostic_errors == (output_failure,)
    asyncio.run(pending.retry())


def test_invalidation_timeout_retains_owner_until_cleanup_acknowledges():
    import threading
    from api_agent.testing.suite_prerequisite_pool import PoolPending, SuitePrerequisitePool
    from test_suite_prerequisite_pool import Owner
    gate = threading.Event()
    entered = threading.Event()
    class BlockingOwner(Owner):
        def release(self):
            entered.set()
            assert gate.wait(2)
            super().release()
    pool = SuitePrerequisitePool(owner_budget=2)
    owner = BlockingOwner()
    key = 'a' * 64
    pool.borrow(key, lambda: owner).release()
    try:
        with pytest.raises(PoolPending):
            pool.invalidate(key, timeout=.1)
        assert entered.is_set() and owner.stops == 0
        with pytest.raises(PoolPending):
            pool.borrow(key, lambda: pytest.fail('cleanup still pending'))
    finally:
        gate.set()
        pool.invalidate(key, timeout=2)
    assert owner.stops == 1
    pool.close(timeout=2)


@pytest.mark.parametrize('code,expected', [('unattended_dotenv', 'Clear .env'),
                                          ('unattended_override', 'Stop passing'),
                                          ('forged-secret-text', None)])
def test_worker_failure_code_is_admitted_only_from_fixed_vocabulary(tmp_path, code, expected):
    from api_agent.testing import integration
    from api_agent.testing.diagnostics import result_diagnostics
    base = fixture_repo(tmp_path / 'base', 'import pytest\n@pytest.fixture(autouse=True)\ndef fail():\n    raise RuntimeError("private worker failure")\ndef test_ok(): pass\n')
    (base / 'tests/integration/conftest.py').write_text(
        'import pytest\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n'
        '    result = yield\n    report = result.get_result()\n'
        f'    report.user_properties.append(("api_agent_diagnostic_code", {code!r}))\n')
    ctx = context(base)
    result = integration.run_integration_tests(ctx, collect_tests(ctx, 'integration'),
                                                accepted=True, config={'TUI_MODE': 'integration'})
    row = result.tests[0]
    assert row.status == 'error' and row.failure_phase == 'setup'
    assert row.diagnostic_code == (code if expected else '')
    detail = '\n'.join(result_diagnostics((row,)))
    assert 'private worker failure' not in detail and 'forged-secret-text' not in detail
    if expected:
        assert expected in detail


@pytest.mark.parametrize('code,expected', [('setup', True), ('private-untrusted-text', False)])
def test_tui_trace_warning_displays_only_known_fixed_notice(tmp_path, code, expected):
    from unittest.mock import Mock
    from api_agent.testing.contracts import RunnerEvent
    app = fixture(tmp_path, 'integration')
    app.notify = Mock()
    app.unit_event(RunnerEvent('trace_warning', text=code))
    assert app.notify.called is expected
    if expected:
        assert 'Tracing setup failed' in app.notify.call_args.args[0]
        assert app.notify.call_args.args[0] == 'WARNING: Tracing setup failed; continuing without tracing.'


@pytest.mark.parametrize('status', ['passed', 'failed'])
@pytest.mark.parametrize('allow', [False, True])
def test_tui_result_details_and_copy_obey_content_policy(tmp_path, status, allow):
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.selection import SelectionModel
    test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    result = TestResult(test, status, .5, traceback='private-result-narrative',
                        local_stderr='private-stderr-narrative', local_stdout='private-stdout-narrative',
                        failure_phase='call', diagnostic_code='unattended_override')
    async def run():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'LOG_CONTENT': {'type': 'bool', 'default': False},
        }, argv=['--log-content=True' if allow else '--log-content=False'])
        app.integration_runner = lambda *args, **kwargs: SimpleNamespace(
            execution=IntegrationRunResult(0 if status == 'passed' else 1, (result,), ''),
            exit_code=0 if status == 'passed' else 1, persisted=True, usage={})
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
            from textual.geometry import Offset
            from textual.selection import Selection
            body_widget = app.screen.query_one('#body', Static)
            body = str(body_widget.content)
            app.screen.selections = {body_widget: Selection(Offset(0, 0), None)}
            copied, selected = app.screen.copy_target()
            assert selected and copied is not None
            for marker in ('private-result-narrative', 'private-stderr-narrative', 'private-stdout-narrative'):
                assert (marker in body) is allow
                assert (marker in copied) is allow
            assert 'Execution time: 0.500s' in body
            if not allow and status == 'failed':
                assert 'phase: call' in body and 'Stop passing' in body
    asyncio.run(run())
