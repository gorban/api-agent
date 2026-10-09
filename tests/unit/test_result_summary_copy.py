"""Outcome presentation and selected-text copying without live operations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, DataTable, Static
from textual.widgets._footer import FooterKey

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


@pytest.mark.parametrize('kind', ['test-results', 'test-failure'])
@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('platform,key,label', [('linux', 'ctrl+c', '^c'), ('darwin', 'super+c', '⌘c')])
def test_result_copy_footer_and_test_name_fallback(tmp_path, monkeypatch, kind, mode, platform, key, label):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            app.mode = mode
            name = 'base::tests/' + 'long_nested_path/' * 10 + 'test_example.py::test_full_name[param]'
            if kind == 'test-results':
                tests = (TestResult(TestIdentity(name, 'base', 'test_example.py', name), 'passed'),)
                app.unit_rows = {name: ('PASSED', name, '0.001s')}
                app.unit_result = (UnitRunResult(0, tests, (), '', require_full_coverage=False)
                                   if mode == 'unit' else IntegrationRunResult(0, tests, ''))
                app.operation_status = 0
                app.show_unit_results()
            else:
                app.show(kind, 'Test failure', name + '\n\nSynthetic assertion details')
            await pilot.pause()
            screen = app.screen

            def copy_hint():
                hints = [hint for hint in screen.query(FooterKey)
                         if hint.description == 'Copy value']
                assert len(hints) == 1
                hint = hints[0]
                assert hint.key == key and hint.key_display == label
                assert hint.region.width and hint.region.right <= screen.size.width
                assert hint.render().plain.strip() == f'{label} Copy value'
                shortcuts = [hint for hint in screen.query(FooterKey)
                             if not hint.has_class('-command-palette')]
                assert shortcuts[-1] is hint
                return hint

            assert not copy_hint().has_class('-disabled')
            await pilot.press(key)
            copy.assert_awaited_once_with(app, name)
            copy.reset_mock()
            await pilot.mouse_down('#body', offset=(0, 0))
            await pilot.hover('#body', offset=(8, 0))
            await pilot.mouse_up('#body', offset=(8, 0))
            await pilot.pause()
            expected = screen.query_one('#body', Static).content
            expected = (expected.plain if hasattr(expected, 'plain') else expected)[:9]
            assert screen.get_selected_text() == expected
            assert not copy_hint().has_class('-disabled')
            await pilot.press(key)
            await pilot.pause()
            copy.assert_awaited_once_with(app, expected)
            assert screen.get_selected_text() is None
            assert not copy_hint().has_class('-disabled')
            await pilot.press(key)
            copy.assert_awaited_with(app, name)
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('completed', [False, True])
def test_results_tab_only_visits_test_list(tmp_path, monkeypatch, mode, completed):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.mode = mode
            tests = outcomes(('passed', 'failed'))
            app.unit_rows = {test.identity.id: (test.status.upper(), test.identity.id, '')
                             for test in tests}
            if completed:
                app.unit_result = (UnitRunResult(1, tests, (), '', require_full_coverage=False)
                                   if mode == 'unit' else IntegrationRunResult(1, tests, ''))
            app.show_unit_results('Synthetic summary\n' * 40)
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(DataTable)
            assert screen.focus_chain == [table]
            for key in ('tab', 'tab', 'shift+tab', 'shift+tab'):
                await pilot.press(key)
                assert table.has_focus
            table.move_cursor(row=1)
            await pilot.press('tab')
            assert table.cursor_row == 1 and table.has_focus
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, str(table.get_row_at(1)[1]))
            # The scrollable summary is still interactive by mouse, without a Tab stop.
            await pilot.click('#body')
            await pilot.pause()
            summary = screen.query_one('#unit-summary')
            assert summary.has_focus
            await pilot.press('down')
            await pilot.pause()
            assert summary.scroll_y > 0
            await pilot.press('tab')
            assert table.has_focus
    asyncio.run(scenario())


def test_empty_results_copy_only_highlighted_summary(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            app.show('test-results', 'Test results', 'Synthetic summary',
                     rows=[('base::test_a', ('PASSED', 'base::test_a', ''))])
            await pilot.pause()
            screen = app.screen
            screen.query_one(DataTable).clear()
            screen.refresh_bindings()
            await pilot.pause()
            assert screen.selected_test_name() is None
            assert not screen.check_action('copy_value', ())
            assert not [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            await screen.action_copy_value()
            copy.assert_not_awaited()
            await pilot.mouse_down('#body', offset=(0, 0))
            await pilot.hover('#body', offset=(8, 0))
            await pilot.mouse_up('#body', offset=(8, 0))
            await pilot.pause()
            hints = [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            assert len(hints) == 1 and not hints[0].has_class('-disabled')
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, 'Synthetic')
            await pilot.pause()
            assert not [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            app.show('test-failure', 'No selected test', '')
            await pilot.pause()
            assert app.screen.selected_test_name() is None
            assert not app.screen.check_action('copy_value', ())
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('platform,key', [('linux', 'ctrl+c'), ('darwin', 'super+c')])
def test_initial_picker_copy_only_while_table_focused(tmp_path, monkeypatch, mode, platform, key):
    async def scenario():
        tests = tuple(test.identity for test in outcomes(('passed', 'passed')))
        app = fixture(tmp_path, mode, collector=lambda *a: tests)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            screen = app.screen
            assert screen.kind == 'tests'
            table = screen.query_one(DataTable)
            table.move_cursor(row=table.row_count - 1)
            screen.refresh_bindings()
            await pilot.pause()
            checked = app.selection.selected_ids.copy()
            hints = [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            assert len(hints) == 1 and hints[0].key == key
            await pilot.press(key)
            copy.assert_awaited_once_with(app, tests[-1].id)
            assert app.selection.selected_ids == checked
            await pilot.press('tab')
            await pilot.pause()
            assert isinstance(app.focused, Button)
            assert screen.selected_test_name() is None
            assert not [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            copy.reset_mock()
            await screen.action_copy_value()
            copy.assert_not_awaited()
            await pilot.press('shift+tab')
            await pilot.pause()
            assert table.has_focus
            assert [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
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


@pytest.mark.parametrize('platform,key,label', [('linux', 'ctrl+c', '^c'), ('darwin', 'super+c', '⌘c')])
def test_execution_error_screen_shares_copy_and_mouse_selection(tmp_path, monkeypatch, platform, key, label):
    async def scenario():
        app = fixture(tmp_path)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        message = 'Integration setup or recording failed. Check configuration and the selected provider.'
        async with app.run_test(size=(140, 35)) as pilot:
            await pilot.pause()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            app.show('error', 'Integration execution failed', message, (('menu', 'Main menu'),))
            await pilot.pause()
            screen = app.screen
            screen.refresh_bindings()
            await pilot.pause()
            assert app.focused is screen.query_one(Button)
            hints = [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            assert len(hints) == 1 and hints[0].key == key and hints[0].key_display == label
            assert not hints[0].has_class('-disabled')
            await pilot.press(key)
            copy.assert_awaited_once_with(app, message)
            assert app.screen is screen
            copy.reset_mock()
            await pilot.mouse_down('#body', offset=(0, 0))
            await pilot.hover('#body', offset=(37, 0))
            await pilot.mouse_up('#body', offset=(37, 0))
            selected = screen.get_selected_text()
            assert selected == 'Integration setup or recording failed.'
            await pilot.press(key)
            copy.assert_awaited_once_with(app, selected)
            assert screen.get_selected_text() is None
            assert app.screen is screen
    asyncio.run(scenario())


def test_empty_error_message_does_not_offer_copy(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show('error', 'Empty error')
            await pilot.pause()
            assert app.screen.copy_target() == (None, False)
            assert not app.screen.check_action('copy_value', ())
    asyncio.run(scenario())
