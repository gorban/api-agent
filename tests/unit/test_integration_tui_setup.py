"""Integration Start joins recording and shared results without live services."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Static

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from test_tui import fixture


def test_skipped_results_are_orange_and_enter_opens_selected_reason(tmp_path):
    identities = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
                  for name in ('pass', 'skip_first', 'skip_second')]
    results = (TestResult(identities[0], 'passed'),
               TestResult(identities[1], 'skipped', traceback='First unavailable prerequisite'),
               TestResult(identities[2], 'skipped', traceback='Second unavailable prerequisite'))
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities)
        app.integration_runner = lambda *args, **kwargs: SimpleNamespace(
            execution=IntegrationRunResult(0, results, ''), exit_code=0)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.4)
            body = app.screen.query_one('#body', Static).content
            assert 'PASS' in body.plain and '1/3 passed (33.33%), 2/3 skipped (66.67%)' in body.plain
            skip_offset = body.plain.index('2/3 skipped')
            assert any(span.start <= skip_offset < span.end and span.style == 'dark_orange'
                           for span in body.spans)
            table = app.screen.query_one(DataTable)
            assert [str(table.get_row_at(i)[1]) for i in range(3)] == [
                identities[1].id, identities[2].id, identities[0].id]
            table.move_cursor(row=1)
            table.focus()
            await pilot.press('enter')
            await pilot.pause(.2)
            assert app.screen.heading == 'Integration test skipped'
            assert 'Second unavailable prerequisite' in app.screen.body
            assert 'First unavailable prerequisite' not in app.screen.body
            assert app.operation_status == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('status,persisted', [('passed', True), ('failed', True), ('passed', False)])
def test_integration_start_records_and_displays_results(tmp_path, status, persisted):
    test = TestIdentity('base::tests/integration/test_setup.py::test_setup', 'base', 'tests/integration/test_setup.py', 'tests/integration/test_setup.py::test_setup')
    calls = []
    def run(context, selected, config, **kwargs):
        calls.append((selected, config, kwargs))
        execution = IntegrationRunResult(int(status != 'passed'), (TestResult(test, status, .25, 'setup failed' if status == 'failed' else ''),), '')
        return SimpleNamespace(execution=execution, exit_code=execution.exit_code or int(not persisted))
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = run
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            assert app.screen.kind == 'tests' and not calls
            await pilot.press('s')
            await pilot.pause(.4)
            assert len(calls) == 1 and calls[0][2]['accepted'] is True
            assert app.screen.kind == 'test-results'
            text = str(app.screen.query_one('#body', Static).render())
            assert 'Integration results:' in text
            assert ('PASS' if status == 'passed' and persisted else 'FAIL') in text
            assert app.operation_status == int(status != 'passed' or not persisted)
            assert not app.test_run_active()
            await pilot.press('b')
            await pilot.pause(.2)
            assert app.screen.kind == 'tests'
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli'])
def test_unattended_failure_guidance_uses_launch_index_source(tmp_path, monkeypatch, source):
    from api_agent.auth import credentials
    from api_agent.providers.contracts import UnattendedCredentialError
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from test_minimal_subscription_credentials import config as credential_config
    from types import SimpleNamespace
    from traceback import format_exception

    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    def fail(*args, **kwargs):
        raise credentials.CredentialError('private CLI output')
    monkeypatch.setattr(credentials, 'get_username', fail)
    seen = []
    def runner(context, tests, config, **options):
        assert config['UNATTENDED_USER_INDEX'] == 0
        seen.append(options['unattended_index_source'])
        worker = IntegrationLLM(credential_config(), tmp_path,
            unattended_index_source=options['unattended_index_source'])
        try:
            with pytest.raises(UnattendedCredentialError) as caught:
                worker.loop.run_until_complete(worker._credentials(0)(0, 'username'))
            from api_agent.testing.provider_setup import pytest_runtest_makereport
            report = SimpleNamespace(longrepr=''.join(format_exception(caught.value)), when='call', passed=False)
            hook = pytest_runtest_makereport(SimpleNamespace(), SimpleNamespace(excinfo=SimpleNamespace(value=caught.value)))
            next(hook)
            with pytest.raises(StopIteration) as finished:
                hook.send(report)
            assert finished.value.value is report
            trace = report.longrepr
        finally:
            worker.close()
        execution = IntegrationRunResult(1, (TestResult(test, 'failed', .1, trace),), '')
        return SimpleNamespace(execution=execution, persisted=True, exit_code=1)

    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test], fields={
            'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True, 'acknowledge_default': False},
            'LLM_CREDENTIAL_ROLE': {'type': 'str', 'default': 'LLM User', 'acknowledge_default': False},
        }, argv=('--unattended-user-index=0',) if source == 'cli' else ())
        if source == 'dotenv':
            app.session.set_value('UNATTENDED_USER_INDEX', '0', persist=True)
        elif source == 'environment':
            app.session.environ['UNATTENDED_USER_INDEX'] = '0'
        expected_source = app.session.result().fields['UNATTENDED_USER_INDEX'].source
        app.integration_runner = runner
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert seen == [expected_source]
            assert app.screen.kind == 'test-results'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'test-failure'
            body = app.screen.body.plain
            assert body.startswith(test.id + '\n\nFailed to get credentials for unattended access.')
            assert body.index('Failed to get credentials') < body.index('Execution time: 0.100s')
            assert '\n\nFailed to get credentials for unattended access.' in body
            assert 'Failed to get credentials for unattended access.' in body
            assert ('Clear .env configuration' in body) is (source == 'dotenv')
            assert ('stop passing the override' in body) is (source != 'dotenv')
            assert 'private CLI output' not in body
            assert 'Password-manager username retrieval is unavailable.' in body
    asyncio.run(scenario())


def test_integration_runner_failure_has_local_diagnostics(tmp_path):
    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = Mock(side_effect=RuntimeError('synthetic runner failure'))
        async with app.run_test() as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind == 'error' and app.operation_status == 3
            body = str(app.screen.query_one('#body', Static).render())
            assert 'RuntimeError: synthetic runner failure' in body
            assert 'Traceback (most recent call last):' in body
            assert not app.test_run_active()
    asyncio.run(scenario())


def test_subscription_with_no_vault_role_skips_auth_suggestion(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'LLM_CREDENTIAL_ROLE': {'type': 'str', 'default': '', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause(.2)
            assert app.credential_roles() == () and not app.auth_setup_needed()
            assert app.screen.kind != 'suggest'
    asyncio.run(scenario())


@pytest.mark.parametrize('unattended', [False, True])
def test_single_start_uses_effective_account_choice(tmp_path, unattended):
    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    calls = []
    def run(context, selected, config, **kwargs):
        calls.append(config)
        return SimpleNamespace(execution=IntegrationRunResult(0, (TestResult(test, 'passed'),), ''), exit_code=0)
    async def scenario():
        app = fixture(tmp_path, 'integration', fields={'UNATTENDED_USER_INDEX': {
            'type': 'int', 'allow_empty': True}}, collector=lambda *args: [test],
            argv=['--unattended-user-index=' + ('0' if unattended else '')])
        app.integration_runner = run
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            assert app.screen.actions == (('continue', '(s) Start selected tests'),)
            await pilot.press('u')
            assert not calls
            await pilot.press('s')
            await pilot.pause(.3)
            assert calls[0]['UNATTENDED_USER_INDEX'] == (0 if unattended else None)
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == (0 if unattended else None)
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


def test_start_rejects_unavailable_selected_account(tmp_path):
    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    async def scenario():
        app = fixture(tmp_path, 'integration', fields={'UNATTENDED_USER_INDEX': {
            'type': 'int', 'allow_empty': True}}, argv=['--unattended-user-index=3'],
            collector=lambda *args: [test])
        runner = Mock()
        app.integration_runner = runner
        async with app.run_test() as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.2)
            assert 'no eligible configured account' in str(app.screen.query_one('#error', Static).render())
            runner.assert_not_called()
            assert not app.test_run_active()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancelled', [False, True])
def test_recollection_can_cancel_or_remove_selection_before_runner(tmp_path, cancelled):
    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        runner = Mock(side_effect=AssertionError('No remaining selection must not execute'))
        app.integration_runner = runner
        async with app.run_test() as pilot:
            await pilot.pause(.2)
            async def fresh(event):
                if cancelled:
                    event.set()
                return []
            app.discover_tests = fresh
            await pilot.press('s')
            await pilot.pause(.2)
            runner.assert_not_called()
            assert not app.test_run_active()
            if not cancelled:
                assert app.screen.kind == 'error'
    asyncio.run(scenario())


@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_close_integration_waits_for_runner_stop(tmp_path, cleanup_failure):
    import threading
    test = TestIdentity('base::t.py::test_setup', 'base', 't.py', 't.py::test_setup')
    started, stopped = threading.Event(), threading.Event()
    def run(context, selected, config, **kwargs):
        started.set()
        assert kwargs['cancel_event'].wait(10)
        stopped.set()
        if cleanup_failure:
            raise RuntimeError('Synthetic teardown failure')
        execution = IntegrationRunResult(130, (TestResult(test, 'cancelled'),), '', True)
        return SimpleNamespace(execution=execution, exit_code=130)
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        app.integration_runner = run
        async with app.run_test() as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.2)
            assert started.is_set() and app.test_run_active()
            await pilot.press('c')
            await pilot.pause(.1)
            assert not stopped.is_set()
            await pilot.press('y', 'enter')
            await pilot.pause(.2)
            assert stopped.is_set() and not app.test_run_active()
            assert app.screen.kind == 'menu' and app.operation_status == 130
    asyncio.run(scenario())


def test_empty_summary_is_explicit_failure(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(1, (), '')
            app.operation_status = 1
            app.show_unit_results()
            await pilot.pause(.1)
            assert 'FAIL' in str(app.screen.query_one('#body', Static).render())
            assert '(0.00%)' in str(app.screen.query_one('#body', Static).render())
    asyncio.run(scenario())


def test_integration_summary_identifies_skipped_allure_reporting(tmp_path):
    test = TestIdentity('base::t.py::test_ok', 'base', 't.py', 't.py::test_ok')
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [test])
        async with app.run_test() as pilot:
            await pilot.pause(.1)
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (TestResult(test, 'passed'),), '')
            app.integration_reporting_skipped = True
            app.show_unit_results()
            await pilot.pause(.1)
            body = app.screen.query_one('#body', Static).content
            assert 'Allure reporting: SKIPPED by configuration' in body.plain
    asyncio.run(scenario())


def test_selected_ui_intent_reports_missing_configuration_before_execution(tmp_path):
    test=TestIdentity('base::t.py::test_ui','base','t.py','t.py::test_ui',frozenset({'requires_ui_session'}))
    async def scenario():
        app=fixture(tmp_path,'integration',collector=lambda *args:[test],fields={
            'UI_REQUIRED':{'type':'str','required':True,'acknowledge_default':False,
                'applies_when':{'operation':'ui_session'}}})
        app.integration_runner=Mock(side_effect=AssertionError('must not execute without UI configuration'))
        async with app.run_test(size=(110,40)) as pilot:
            await pilot.pause(.2)
            assert app.screen.kind=='tests'
            await pilot.press('s')
            await pilot.pause(.3)
            assert app.screen.kind=='tests'
            assert 'Missing required configuration: UI_REQUIRED' in str(app.screen.query_one('#error', Static).content)
            app.integration_runner.assert_not_called()
    asyncio.run(scenario())
