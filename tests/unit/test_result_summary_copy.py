"""Outcome presentation and selected-text copying without live operations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult, format_test_summary
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.runner import UnitRunResult
from api_agent.tui.clipboard import ClipboardError
from api_agent.tui.test_results import progress_summary
from test_tui import fixture


def outcomes(statuses):
    return tuple(TestResult(TestIdentity(f'base::t.py::test_{i}', 'base', 't.py', f't.py::test_{i}'), status)
                 for i, status in enumerate(statuses))


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
@pytest.mark.parametrize('statuses,expected', [
    (('passed',), 'PASS — 1/1 passed (100.00%).'),
    (('passed', 'passed', 'skipped'), 'PASS — 2/3 passed (66.67%), 1/3 skipped (33.33%).'),
    (('failed', 'passed', 'skipped'), 'FAIL — 1/3 passed (33.33%), 1/3 skipped (33.33%).'),
    (('skipped',), 'PASS — 0/1 passed (0.00%), 1/1 skipped (100.00%).'),
    ((), 'FAIL — 0/0 passed (0.00%).'),
])
def test_shared_completed_counts(mode, statuses, expected):
    assert format_test_summary(mode, outcomes(statuses)) == f'{mode.capitalize()} results: {expected}'


@pytest.mark.parametrize('statuses,expected', [
    (('passed',), 'Unit results: PASS — 1/1 passed.'),
    (('passed', 'failed'), 'Unit results: FAIL — 1/2 passed; 1 did not pass.'),
])
def test_unit_summary_omits_zero_nonpassing_count(statuses, expected):
    assert format_test_summary('unit', outcomes(statuses)) == expected


@pytest.mark.parametrize('skip_count', [0, 1])
def test_progress_skip_color_only_when_nonzero(skip_count):
    rows = {str(i): (status, str(i), '') for i, status in enumerate(['PASSED'] + ['SKIPPED'] * skip_count)}
    text = progress_summary('integration', rows)
    start = text.plain.index(f'{skip_count} skipped')
    assert any(span.start <= start < span.end and span.style == 'dark_orange'
               for span in text.spans) is bool(skip_count)


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
@pytest.mark.parametrize('statuses,color', [
    (('passed',), 'green'), (('passed', 'skipped'), 'dark_orange'),
    (('failed', 'skipped'), 'red'), ((), 'red'),
])
def test_completed_header_color_uses_outcomes(mode, statuses, color, tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app.mode = mode
            app.unit_result = IntegrationRunResult(0, outcomes(statuses), '')
            app.operation_status = 0
            app.show_unit_results()
            await pilot.pause()
            body = app.screen.query_one('#body', Static).content
            assert body.spans[0].style == color
            assert body.plain.splitlines()[0] == format_test_summary(mode, outcomes(statuses))
    asyncio.run(scenario())


@pytest.mark.parametrize('persisted', [True, False])
def test_reporting_failure_is_distinct_from_passing_tests(tmp_path, persisted):
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *a: [outcomes(('passed',))[0].identity])
        app.integration_runner = lambda *a, **kw: SimpleNamespace(
            execution=IntegrationRunResult(0, outcomes(('passed',)), ''), exit_code=1, persisted=persisted)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause(.3)
            text = app.screen.query_one('#body', Static).content
            assert text.plain.splitlines()[0] == 'Integration results: PASS — 1/1 passed (100.00%).'
            assert text.spans[0].style == 'green'
            message = 'Test operation: FAIL' if persisted else 'Allure publication: FAIL'
            assert message in text.plain
            assert any(span.style == 'red' and span.start >= text.plain.index(message) for span in text.spans)
            assert app.operation_status == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_cancel_after_last_pass_is_not_a_reporting_failure(tmp_path, mode):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = mode
            app.unit_result = IntegrationRunResult(130, outcomes(('passed',)), '', True)
            app.operation_status = 130
            app.integration_persisted = True
            app.show_unit_results()
            await pilot.pause()
            body = app.screen.query_one('#body', Static).content
            assert 'CANCELLED' in body.plain
            assert 'Test operation: FAIL' not in body.plain
            assert 'Allure publication: FAIL' not in body.plain
            assert body.spans[0].style == 'red'
            assert app.operation_status == 130
    asyncio.run(scenario())


def test_mouse_selected_summary_is_copied_and_visibly_deselected(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, outcomes(('passed',)), '')
            app.operation_status = 0
            app.show_unit_results()
            await pilot.pause()
            screen = app.screen
            await pilot.mouse_down('#body', offset=(0, 0))
            await pilot.hover('#body', offset=(19, 0))
            await pilot.mouse_up('#body', offset=(19, 0))
            expected = screen.get_selected_text()
            assert expected == 'Integration results:'
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, expected)
            assert screen.get_selected_text() is None
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('platform,key', [('linux', 'ctrl+c'), ('darwin', 'super+c')])
@pytest.mark.parametrize('partial', [False, True])
def test_copy_result_selection_then_deselects(tmp_path, monkeypatch, mode, platform, key, partial):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=False)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            app.mode = mode
            tests = outcomes(('passed',))
            app.unit_result = (UnitRunResult(0, tests, (), '', require_full_coverage=False)
                               if mode == 'unit' else IntegrationRunResult(0, tests, ''))
            app.operation_status = 0
            app.show_unit_results()
            await pilot.pause()
            screen = app.screen
            body = screen.query_one('#body', Static)
            screen.selections = {body: Selection.from_offsets(Offset(0, 0), Offset(12, 0))
                                 if partial else Selection(None, None)}
            expected = screen.get_selected_text()
            assert expected and ('results' in expected or partial)
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            await pilot.press(key)
            copy.assert_awaited_once_with(app, expected)
            assert screen.get_selected_text() is None
            assert app.screen is screen
            assert not any(call.kwargs.get('title') == 'Do you want to quit?' for call in notify.call_args_list)
    asyncio.run(scenario())


def test_mac_control_c_keeps_quit_hint_and_copy_failure_keeps_selection(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(side_effect=ClipboardError('synthetic-private'))
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show('test-failure', 'Test failure', 'Selected details')
            await pilot.pause()
            screen = app.screen
            screen.selections = {screen.query_one('#body', Static): Selection(None, None)}
            monkeypatch.setattr('api_agent.tui.app.sys.platform', 'darwin')
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            await pilot.press('ctrl+c')
            copy.assert_not_awaited()
            assert any(call.kwargs.get('title') == 'Do you want to quit?' for call in notify.call_args_list)
            await pilot.press('super+c')
            copy.assert_awaited_once_with(app, 'Selected details')
            assert screen.get_selected_text() == 'Selected details'
            notify.assert_called_with('Could not copy the value.', severity='error', timeout=2)
    asyncio.run(scenario())
