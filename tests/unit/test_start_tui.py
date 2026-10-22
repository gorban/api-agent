"""Automatic interactive starts use temporary settings and simulated runners."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Input

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.runner import UnitRunResult
from test_tui import fixture


START = {'type': 'str', 'default': 'False', 'enum': ['', 'False', 'True', 'E2E'], 'acknowledge_default': False}
TESTS = [TestIdentity('base::one.py::test_a', 'base', 'one.py', 'one.py::test_a'),
         TestIdentity('base::two.py::test_b', 'base', 'two.py', 'two.py::test_b')]


async def wait_for(pilot, condition):
    for _ in range(60):
        if condition():
            return
        await pilot.pause(.05)
    assert condition()


def unit_runner(calls):
    def run(context, tests, **options):
        calls.append(tuple(test.id for test in tests))
        return UnitRunResult(0, tuple(TestResult(test, 'passed') for test in tests), (), '')
    return run


def test_start_preserves_suggestion_review_and_filtered_one_shot(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START, 'COUNT': {'type': 'int', 'default': 8}},
                      collector=lambda *args: TESTS, argv=['--start', TESTS[0].id])
        app.session.environ.pop('UNATTENDED_AUTH')
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest' and not calls
            await pilot.press('s')
            assert app.screen.kind == 'defaults' and not calls
            assert [row.key.value for row in app.screen.query_one(DataTable).ordered_rows] == ['COUNT']
            await pilot.press('a')
            assert app.screen.kind == 'defaults' and not calls
            await pilot.press('c')
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert calls == [(TESTS[0].id,)]
            assert app.screen.kind == 'test-results' and not app.auto_start_pending
            await pilot.press('b')
            await wait_for(pilot, lambda: app.screen.kind == 'tests')
            assert app.selection.selected_ids == {TESTS[0].id} and len(calls) == 1
            await pilot.press('c')
            assert app.screen.kind == 'menu'
            await pilot.click('#mode-unit')
            await wait_for(pilot, lambda: app.screen.kind == 'tests')
            assert app.selection.selected_ids == {test.id for test in TESTS}
            assert len(calls) == 1
    asyncio.run(scenario())


def test_start_waits_for_required_configuration(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START, 'NEEDED': {'type': 'str', 'required': True}},
                      collector=lambda *args: TESTS, argv=['--start'])
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults' and not calls
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults' and not calls
            await pilot.press('e')
            app.screen.query_one(Input).value = 'provided'
            await pilot.press('enter')
            await pilot.press('c')
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert calls == [tuple(test.id for test in TESTS)]
            assert app.session.result().values['NEEDED'] == 'provided'
    asyncio.run(scenario())


@pytest.mark.parametrize('configured', [False, True])
def test_start_false_or_omitted_keeps_picker(tmp_path, configured):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START}, collector=lambda *args: TESTS,
                      argv=['--start=False'] if configured else [])
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.screen.kind == 'tests')
            assert not calls and not app.auto_start_pending
    asyncio.run(scenario())


def test_start_integration_uses_selected_subset(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'integration', fields={'START': START}, collector=lambda *args: TESTS,
                      argv=['--start', TESTS[1].id])
        calls = []
        def run(context, tests, config, **options):
            calls.append((tuple(test.id for test in tests), options['accepted']))
            return SimpleNamespace(execution=IntegrationRunResult(0,
                tuple(TestResult(test, 'passed') for test in tests), ''), exit_code=0, persisted=True)
        app.integration_runner = run
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert calls == [((TESTS[1].id,), True)]
            assert app.screen.kind == 'test-results'
    asyncio.run(scenario())


def test_start_e2e_keeps_human_acceptance(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'e2e', fields={'START': START}, collector=lambda *args: TESTS,
                      argv=['--start', TESTS[0].id])
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.screen.kind == 'acceptance')
            assert '1 tests selected' in app.screen.body
            assert not app.test_run_active() and not app.auto_start_pending
            await pilot.pause()
            assert await pilot.click('#decline')
            await wait_for(pilot, lambda: app.screen.kind == 'tests')
            assert app.screen.kind == 'tests'
            await pilot.pause()
            assert app.screen.kind == 'tests' and not app.test_run_active()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['deployment-logs'])
def test_start_service_modes_retain_current_operation_boundary(tmp_path, mode):
    async def scenario():
        app = fixture(tmp_path, mode, fields={'START': START}, argv=['--start'])
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.screen.kind == 'unavailable')
            assert app.mode == mode and not app.auto_start_pending
            assert 'not implemented' in app.screen.body
    asyncio.run(scenario())


def test_start_configuration_review_empty_transition(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START, 'COUNT': {'type': 'int', 'default': 8}},
                      collector=lambda *args: TESTS, argv=['--start'])
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.set_value('COUNT', '8', persist=True)
            app.review_names.clear()
            app.show_configuration()
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert len(calls) == 1
    asyncio.run(scenario())


def test_start_auth_setup_returns_through_review(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START}, collector=lambda *args: TESTS, argv=['--start'])
        app.session.environ.pop('UNATTENDED_AUTH')
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click('#setup')
            await pilot.pause()
            assert not calls and app.mode is None
            await pilot.press('c')
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert len(calls) == 1
    asyncio.run(scenario())


def test_start_invalid_mode_reports_configuration_error(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'config-cleanup', fields={'START': START}, argv=['--start'])
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Configuration error'
            assert 'START=True or E2E requires TUI_MODE' in app.screen.body
    asyncio.run(scenario())


def test_start_required_auth_editor_resumes_pending_operation(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START,
            'UNATTENDED_AUTH': {'type': 'jsonobject', 'acknowledge_default': False,
                               'required_when': {'TUI_MODE': 'unit'}}},
            collector=lambda *args: TESTS, argv=['--start'])
        profile = app.session.environ.pop('UNATTENDED_AUTH')
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            await pilot.press('s')
            assert app.screen.heading == 'Unattended authentication required'
            assert app.mode == 'unit' and not calls
            await pilot.click('#setup')
            await pilot.pause()
            app.session.set_value('UNATTENDED_AUTH', profile)
            await pilot.press('c')
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert len(calls) == 1
    asyncio.run(scenario())


def test_start_hidden_unless_persisted_for_cleanup(tmp_path):
    app = fixture(tmp_path, fields={'START': START})
    assert 'START' not in app.configuration_pending()
    assert 'START' not in {row.name for row in app.session.cleanup_rows()}
    app.session.set_value('START', 'False', persist=True)
    row = next(row for row in app.session.cleanup_rows() if row.name == 'START')
    assert row.present and row.value == 'False' and row.source.endswith('/.env')
    app.session.delete_keys(['START'])
    assert 'START' not in {row.name for row in app.session.cleanup_rows()}


def test_start_collect_only_does_not_suggest_auth_or_execute(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START, 'COLLECT_ONLY': {'type': 'bool', 'default': False}},
                      collector=lambda *args: TESTS, argv=['--start', '--collect-only', TESTS[0].id])
        app.session.environ.pop('UNATTENDED_AUTH')
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.return_value == 0)
            assert app.collected_output == TESTS[0].id
            assert app.unit_result is None
    asyncio.run(scenario())


def test_start_accepts_defaulted_mode(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'START': START,
            'TUI_MODE': {'type': 'str', 'default': 'unit', 'acknowledge_default': False}},
            collector=lambda *args: TESTS, argv=['--start'])
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert len(calls) == 1 and app.mode == 'unit'
    asyncio.run(scenario())


def test_start_e2e_explicit_acceptance_skips_only_acceptance_screen(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'e2e', fields={'START': START, 'COUNT': {'type': 'int', 'default': 8}},
                      collector=lambda *args: TESTS, argv=['--start=E2E', TESTS[1].id])
        app.session.environ.pop('UNATTENDED_AUTH')
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            await pilot.press('s')
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await wait_for(pilot, lambda: app.screen.kind == 'unavailable')
            assert 'E2E execution is not implemented' in app.screen.body
            assert app.selection.selected_ids == {TESTS[1].id}
            assert not app.auto_start_pending and not app.test_run_active()
            await pilot.press('c')
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


def test_start_e2e_value_starts_other_mode_and_empty_does_not(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'START': START}, collector=lambda *args: TESTS,
                      argv=['--start=E2E'])
        calls = []
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.unit_result is not None)
            assert calls == [tuple(test.id for test in TESTS)]
        app = fixture(tmp_path, 'unit', fields={'START': START}, collector=lambda *args: TESTS,
                      argv=['--start='])
        app.unit_runner = unit_runner(calls)
        async with app.run_test() as pilot:
            await wait_for(pilot, lambda: app.screen.kind == 'tests')
            assert len(calls) == 1 and not app.auto_start_pending
    asyncio.run(scenario())


def test_noninteractive_e2e_explicit_acceptance_reaches_operation_boundary():
    from io import StringIO
    from pathlib import Path
    from unittest.mock import patch
    from api_agent.runtime.bootstrap import main
    root = Path(__file__).resolve().parents[2]
    stderr = StringIO()
    with patch.dict('os.environ', {}, clear=True), patch('sys.stderr', stderr), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
            patch('api_agent.runtime.worktrees.verify_worktrees') as gate, \
            patch('api_agent.runtime.dependencies.ensure_environment') as prepare, \
            patch('api_agent.auth.extensions.validate_operation') as validate:
        status = main(['--no-std-in', '--tui-mode=e2e', '--start=E2E',
                       '--ui-source-dir=/tmp/synthetic-ui', '--allure-s3-bucket=synthetic'], base_root=root)
    gate.assert_called_once()
    validate.assert_called_once()
    prepare.assert_not_called()
    assert status == 3, stderr.getvalue()
    assert 'not implemented yet' in stderr.getvalue()
    assert 'incompatible' not in stderr.getvalue() and 'Configuration error' not in stderr.getvalue()


def test_noninteractive_e2e_acceptance_preserves_operation_validation():
    from io import StringIO
    from pathlib import Path
    from unittest.mock import patch
    from api_agent.runtime.bootstrap import main
    root = Path(__file__).resolve().parents[2]
    stderr = StringIO()
    with patch.dict('os.environ', {}, clear=True), patch('sys.stderr', stderr), patch('api_agent.config.read_dotenv', return_value={}), patch('api_agent.config.dotenv.read_dotenv', return_value={}), patch('api_agent.runtime.worktrees.verify_worktrees'), patch('api_agent.auth.extensions.validate_operation', side_effect=ValueError('synthetic operation requirement')):
        status = main(['--no-std-in', '--tui-mode=e2e', '--start=E2E',
                       '--ui-source-dir=/tmp/synthetic-ui', '--allure-s3-bucket=synthetic'], base_root=root)
    assert status == 3 and stderr.getvalue() == 'synthetic operation requirement\n'


def test_start_allure_enters_managed_history_without_automatic_test_execution(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path,'allure',fields={'START':START},argv=['--start'])
        entered=asyncio.Event()
        async def history():
            entered.set()
        monkeypatch.setattr(app,'show_allure_history',history)
        async with app.run_test() as pilot:
            await wait_for(pilot,entered.is_set)
            assert app.mode=='allure' and not app.auto_start_pending
            assert not app.test_run_active()
    asyncio.run(scenario())
