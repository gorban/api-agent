"""Multi-test DEBUG confirmation uses distinct selected identities."""

from io import StringIO
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from api_agent.testing.debug_confirmation import confirm_terminal_debug, debug_warning
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.runtime.bootstrap import main
from textual.widgets import Input
from test_tui import fixture

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('mode, debug, ids', [
    ('unit', True, ('a', 'b')),
    ('integration', False, ('a', 'b')),
    ('integration', True, ('a', 'a')),
    ('e2e', True, ('a',)),
])
def test_warning_is_limited_to_multiple_distinct_debug_tests(mode, debug, ids):
    tests = [SimpleNamespace(id=identity) for identity in ids]

    warning = debug_warning(mode, debug, tests)

    assert warning is None


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_warning_counts_distinct_tests_and_explains_avoidance(mode):
    tests = [SimpleNamespace(id=identity) for identity in ('a', 'a', 'b', 'c')]

    warning = debug_warning(mode, True, tests)

    assert '3 selected tests' in warning
    assert 'up to 3' in warning
    assert 'DEBUG=False' in warning
    assert 'do not pass --debug' in warning


@pytest.mark.parametrize('answer, expected', [('y\n', True), ('Y\n', False),
                                              ('yes\n', False), ('y', False), ('\n', False), ('', False)])
def test_terminal_requires_literal_y_and_newline(answer, expected):
    output = StringIO()

    accepted = confirm_terminal_debug('warning', stdin=StringIO(answer), stdout=output)

    assert accepted is expected
    assert output.getvalue() == 'warning\nContinue? [y/N] (default No): '


def test_no_warning_never_reads_stdin():
    assert confirm_terminal_debug(None, stdin=None, stdout=StringIO()) is True


def test_terminal_unavailable_input_cancels():
    source = Mock()
    source.readline.side_effect = OSError('unavailable')

    assert confirm_terminal_debug('warning', stdin=source, stdout=StringIO()) is False


@pytest.mark.parametrize('answer,expected_status', [('y\n', 0), ('n\n', 3), ('', 3)])
def test_terminal_dispatch_confirms_before_runner(answer, expected_status):
    selected = [TestIdentity(f'base::test.py::{name}', 'base', 'test.py', f'test.py::{name}')
                for name in ('one', 'two')]
    output, error = StringIO(), StringIO()
    with patch.dict('os.environ', {}, clear=True), patch('sys.stdin', StringIO(answer)), \
         patch('sys.stdout', output), patch('sys.stderr', error), \
         patch('api_agent.config.read_dotenv', return_value={}), \
         patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
         patch('api_agent.runtime.worktrees.verify_worktrees'), \
         patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
         patch('api_agent.testing.collection.collect_tests', return_value=selected), \
         patch('api_agent.testing.requirements.plan_integration_requirements',
               return_value=SimpleNamespace(requires_ui_session=False)) as requirements, \
         patch('api_agent.testing.launched.run_launched_integration',
               return_value=SimpleNamespace(exit_code=0, persisted=True,
                                            execution=IntegrationRunResult(0, (), ''))) as runner:
        status = main(['--tui-mode=integration', '--no-std-in', '--debug',
                       '--allure-s3-bucket=synthetic'], base_root=ROOT)

    assert status == expected_status, error.getvalue()
    assert 'up to 2' in output.getvalue()
    assert requirements.called  # Inert selection planning precedes confirmation.
    assert runner.called is (expected_status == 0)
    if expected_status == 3:
        assert 'confirmation declined' in error.getvalue()


def test_attended_integration_requires_literal_y_before_runner(tmp_path):
    selected = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
                for name in ('one', 'two')]
    runner = Mock(return_value=SimpleNamespace(
        execution=IntegrationRunResult(0, tuple(TestResult(test, 'passed') for test in selected), ''),
        exit_code=0))
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: selected,
                      fields={'DEBUG': {'type': 'bool', 'default': True, 'acknowledge_default': False}})
        app.integration_runner = runner
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.click('#continue')
            await pilot.pause(.1)
            assert 'up to 2' in str(app.screen.query_one('#quit-message').content)
            assert not app.test_run_active()
            assert not runner.called
            app.screen.query_one(Input).value = 'yes'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert app.screen.kind == 'tests'
            assert not runner.called
            await pilot.press('s')
            await pilot.pause(.1)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#quit-submit')
            await pilot.pause(.4)
            assert runner.call_count == 1
            assert app.screen.kind == 'test-results'
            await pilot.press('b')
            await pilot.pause(.1)
            await pilot.press('s')
            await pilot.pause(.1)
            assert 'up to 2' in str(app.screen.query_one('#quit-message').content)
            assert runner.call_count == 1
    asyncio.run(scenario())


def test_recollection_change_requires_fresh_consent_before_runner(tmp_path):
    first = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
             for name in ('one', 'two')]
    changed = [*first, TestIdentity('base::t.py::three', 'base', 't.py', 't.py::three')]
    calls = 0
    def collect(*args):
        nonlocal calls
        calls += 1
        return first if calls == 1 else changed
    runner = Mock()
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=collect,
                      fields={'DEBUG': {'type': 'bool', 'default': True, 'acknowledge_default': False}})
        app.integration_runner = runner
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.1)
            assert 'up to 2' in str(app.screen.query_one('#quit-message').content)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert 'up to 3' in str(app.screen.query_one('#quit-message').content)
            assert not runner.called
            app.screen.query_one(Input).value = 'n'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert app.screen.kind == 'tests'
            assert not runner.called
            assert not app.test_run_active()
    asyncio.run(scenario())


@pytest.mark.parametrize('flag', ['DEBUG','E2E_DEBUG'])
def test_e2e_debug_confirmation_precedes_existing_acceptance(tmp_path,flag):
    selected = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
                for name in ('one', 'two')]
    async def scenario():
        app = fixture(tmp_path, 'e2e', collector=lambda *args: selected,
                      fields={flag: {'type': 'bool', 'default': True, 'acknowledge_default': False},
                              'START': {'type': 'str', 'default': 'False', 'acknowledge_default': False}})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.1)
            assert 'up to 2' in str(app.screen.query_one('#quit-message').content)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert app.screen.kind == 'acceptance'
            await pilot.click('#accept')
            await pilot.pause(.2)
            assert app.screen.kind == 'unavailable'
            assert 'not implemented yet' in app.screen.body
    asyncio.run(scenario())


def test_late_debug_confirmation_after_cancel_cannot_start_runner(tmp_path):
    initial = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
               for name in ('one', 'two')]
    changed = [*initial, TestIdentity('base::t.py::three', 'base', 't.py', 't.py::three')]
    collections = 0
    def collect(*args):
        nonlocal collections
        collections += 1
        return initial if collections == 1 else changed
    runner = Mock()
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=collect,
                      fields={'DEBUG': {'type': 'bool', 'default': True, 'acknowledge_default': False}})
        app.integration_runner = runner
        responses = []
        original_push = app.push_screen
        def capture_response(screen, callback=None):
            if callback is not None:
                responses.append(callback)
            return original_push(screen, callback)
        app.push_screen = capture_response
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.1)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert 'up to 3' in str(app.screen.query_one('#quit-message').content)
            await asyncio.wait_for(app.test_run.stop_and_wait(), 5)
            assert app.screen.kind == 'tests'
            assert not any(screen.__class__.__name__ == 'QuitDialog' for screen in app.screen_stack)
            responses[-1](True)
            await pilot.pause(.2)
            assert not runner.called
            assert not app.test_run_active()
    asyncio.run(scenario())


@pytest.mark.parametrize('response', ['callback', 'dismissed_modal'])
def test_same_turn_cancel_and_confirmation_cannot_start_runner(tmp_path, response):
    initial = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
               for name in ('one', 'two')]
    changed = [*initial, TestIdentity('base::t.py::three', 'base', 't.py', 't.py::three')]
    collections = 0
    def collect(*args):
        nonlocal collections
        collections += 1
        return initial if collections == 1 else changed
    runner = Mock()
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=collect,
                      fields={'DEBUG': {'type': 'bool', 'default': True, 'acknowledge_default': False}})
        app.integration_runner = runner
        responses = []
        original_push = app.push_screen
        def capture_response(screen, callback=None):
            if callback is not None:
                responses.append(callback)
            return original_push(screen, callback)
        app.push_screen = capture_response
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.1)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#quit-submit')
            await pilot.pause(.2)
            assert 'up to 3' in str(app.screen.query_one('#quit-message').content)
            app.collection_cancel.set()
            if response == 'callback':
                responses[-1](True)
            else:
                app.screen.dismiss(True)
            await asyncio.wait_for(app.test_run.stopped.wait(), 5)
            assert not runner.called
            assert app.screen.kind == 'tests'
            assert not any(screen.__class__.__name__ == 'QuitDialog' for screen in app.screen_stack)
    asyncio.run(scenario())


@pytest.mark.parametrize('debug', [False,True])
def test_legacy_e2e_debug_requires_the_same_multi_selection_acknowledgement(debug):
    tests = [SimpleNamespace(id='one'),SimpleNamespace(id='two')]
    warning = debug_warning('e2e',debug,tests,e2e_debug=True)
    assert '2 selected tests' in warning
    assert 'DEBUG=False and E2E_DEBUG=False' in warning
    assert debug_warning('integration',False,tests,e2e_debug=True) is None
    assert debug_warning('e2e',False,tests[:1],e2e_debug=True) is None


def test_legacy_e2e_recollection_confirmation_can_cancel_before_execution(tmp_path):
    selected = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}')
                for name in ('one','two')]
    async def scenario():
        app = fixture(tmp_path, 'e2e', collector=lambda *args: selected,
            fields={'E2E_DEBUG': {'type':'bool','default':True,'acknowledge_default':False}})
        async with app.run_test(size=(110,40)) as pilot:
            confirmation = asyncio.create_task(app.confirm_debug_selection('e2e',selected,asyncio.Event()))
            await pilot.pause()
            assert 'up to 2' in str(app.screen.query_one('#quit-message').content)
            app.screen.query_one(Input).value = 'n'
            await pilot.click('#quit-submit')
            assert await confirmation is False
    asyncio.run(scenario())
