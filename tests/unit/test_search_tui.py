"""Mounted search workflows use synthetic identities, never real tests or credentials."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from textual import events
from textual.widgets import Button, Static
from textual.widgets._input import Selection

from api_agent.tui.search import SearchBar, SearchInput
from api_agent.tui.test_table import TestTable
from test_test_table import identities
from test_tui import fixture


def picker_buttons(screen, mode):
    start = screen.query_one('#continue', Button)
    if mode == 'unit':
        assert not screen.query('#review-config')
        return [start]
    review = screen.query_one('#review-config', Button)
    assert str(review.label) == 'Review selected-test configuration'
    return [start, review]


def row_keys(table):
    return [row.key.value for row in table.ordered_rows]


@pytest.mark.parametrize('key,direction', [('slash', 1), ('question_mark', -1)])
@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_search_direction_exit_preserves_identity_and_selection(tmp_path, key, direction, mode):
    async def scenario():
        tests = identities(40)
        app = fixture(tmp_path, mode, collector=lambda *a: tests)
        async with app.run_test(size=(90, 25)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            original = row_keys(table)
            selected = app.selection.selected_ids.copy()
            await pilot.press(key)
            control = app.screen.query_one(SearchInput)
            assert app.focused is control
            assert app.screen.focus_chain == [table, *picker_buttons(app.screen, mode), control]
            assert 'all' not in row_keys(table)
            assert app.selection.selected_ids == selected
            assert app.screen.test_search.direction == direction
            assert app.screen.query_one(SearchBar).display
            assert not app.screen.query_one('ButtonFooter').display
            control.value = 'test_0[01]'
            await pilot.pause()
            assert row_keys(table) == [tests[0].id, tests[1].id, 'search:other']
            assert table.cursor_row == (0 if direction > 0 else 1)
            assert str(table.get_row_at(2)[1]) == '38 other tests'
            await pilot.press('enter')
            await pilot.pause()
            removed = tests[0 if direction > 0 else 1].id
            assert app.selection.selected_ids == selected - {removed}
            current = row_keys(table)[table.cursor_row]
            await pilot.press('escape')
            assert app.screen.test_search is None
            assert row_keys(table) == original
            assert row_keys(table)[table.cursor_row] == current
            assert app.focused is table
            assert not app.screen.query_one(SearchBar).display
            assert app.screen.query_one('ButtonFooter').display
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['slash', 'question_mark'])
@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_search_tab_cycles_list_start_and_query_in_both_directions(tmp_path, key, mode):
    async def scenario():
        app = fixture(tmp_path, mode, collector=lambda *a: identities(3))
        async with app.run_test(size=(90, 25)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            buttons = picker_buttons(app.screen, mode)
            start = buttons[0]
            assert app.screen.focus_chain == [table, *buttons]
            for expected in (*buttons, table):
                await pilot.press('tab')
                assert app.focused is expected
            await pilot.press(key)
            control = app.screen.query_one(SearchInput)
            assert app.focused is control
            for size in ((90, 25), (55, 16)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app.screen.focus_chain == [table, *buttons, control]
                table.focus()
                rows_before = row_keys(table)
                cursor_before = table.cursor_row
                selected_before = app.selection.selected_ids.copy()
                for expected in (*buttons, control, table):
                    await pilot.press('tab')
                    assert app.focused is expected
                for expected in (control, *reversed(buttons), table):
                    await pilot.press('shift+tab')
                    assert app.focused is expected
                assert row_keys(table) == rows_before
                assert table.cursor_row == cursor_before
                assert app.selection.selected_ids == selected_before
            app.choose = AsyncMock()
            await pilot.press('tab', 'enter')
            app.choose.assert_awaited_once_with('continue')
            app.choose.reset_mock()
            await pilot.press('s')
            app.choose.assert_awaited_once_with('continue')
            assert app.focused is start and control.value == ''
            app.choose.reset_mock()
            await pilot.press(*(['tab'] * len(buttons)), 's')
            assert app.focused is control and control.value == 's'
            app.choose.assert_not_awaited()
            await pilot.press('tab', 's')
            assert app.focused is control and control.value == 'ss'
            app.choose.assert_not_awaited()
            await pilot.press('escape')
            assert app.screen.focus_chain == [table, *buttons]
            assert app.focused is table
            await pilot.press('shift+tab')
            assert app.focused is buttons[-1]
    asyncio.run(scenario())


def test_other_removal_empty_results_invalid_regex_and_exit_fallback(tmp_path):
    async def scenario():
        tests = identities(3)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('slash')
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = 'test_00'
            await pilot.pause()
            await pilot.press('down', 'enter')
            await pilot.pause()
            assert row_keys(table) == [tests[0].id]
            assert table.cursor_row == 0
            assert app.selection.selected_ids == {tests[0].id}
            control.value = '['
            await pilot.pause()
            assert row_keys(table) == [tests[0].id]
            assert 'Invalid regular expression' in str(app.screen.query_one('#error', Static).content)
            control.value = '^no-match$'
            await pilot.pause()
            assert row_keys(table) == ['search:other']
            await pilot.press('enter')
            await pilot.pause()
            assert table.row_count == 0
            assert str(app.screen.query_one('#error', Static).content) == 'Select at least one test before continuing'
            assert app.screen.copy_target() == (None, False)
            await pilot.press('down', 'enter', 'escape')
            assert table.cursor_row == 0
            assert row_keys(table)[0] == 'all'
            assert not app.selection.selected_ids
            assert str(app.screen.query_one('#error', Static).content) == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['slash', 'question_mark'])
@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_empty_search_shows_selection_error_and_one_letter_match_clears_it(tmp_path, key, mode):
    async def scenario():
        tests = identities(3)
        app = fixture(tmp_path, mode, collector=lambda *a: tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selection.toggle('all')
            app.refresh_rows()
            assert not app.selection.selected_ids
            await app.choose('continue')
            message = 'Select at least one test before continuing'
            assert str(app.screen.query_one('#error', Static).content) == message
            await pilot.press(key)
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = 'test_00x'
            control.selection = Selection.cursor(len(control.value))
            await pilot.pause()
            assert table.row_count == 0
            assert str(app.screen.query_one('#error', Static).content) == message
            await pilot.press('backspace')
            await pilot.pause()
            assert control.value == 'test_00'
            assert row_keys(table) == [tests[0].id]
            assert str(app.screen.query_one('#error', Static).content) == ''
            assert not app.selection.selected_ids  # Finding a match does not select it.
            control.value = '^no-match$'
            await pilot.pause()
            assert table.row_count == 0
            assert str(app.screen.query_one('#error', Static).content) == message
            control.value = '['
            await pilot.pause()
            assert str(app.screen.query_one('#error', Static).content) == 'Invalid regular expression'
    asyncio.run(scenario())


@pytest.mark.parametrize('platform,key', [('linux', 'ctrl+c'), ('darwin', 'ctrl+c')])
def test_search_text_copy_precedes_names_and_replacement_restores_names(tmp_path, monkeypatch, platform, key):
    async def scenario():
        tests = identities(4)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        copied = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            await pilot.press('slash')
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = 'test_00'
            await pilot.pause()
            control.selection = Selection(5, 7)
            await pilot.press(key)
            copied.assert_awaited_with(app, '00')
            assert control.selection.is_empty
            await pilot.press(key)
            copied.assert_awaited_with(app, tests[0].id)
            control.selection = Selection(5, 7)
            await pilot.press('h')
            assert control.value == 'test_h'
            assert control.selection.is_empty
            assert app.screen.copy_target()[0] == '4 other tests'
            await pilot.press('l', 'left', 'shift+left')
            assert control.value == 'test_hl'
            assert control.selected_text == 'h'
            control.value = 'test_0'
            control.selection = Selection.cursor(len(control.value))
            await pilot.pause()
            await pilot.press('shift+down', key)
            copied.assert_awaited_with(app, '\n'.join(tests[i].id for i in (0, 1)))
            assert table.selected_row_indices == (0, 1)
            await pilot.press('enter')
            assert app.selection.selected_ids == {tests[2].id, tests[3].id}
    asyncio.run(scenario())


def test_search_navigation_ranges_literal_text_paste_and_invalid_cursor(tmp_path):
    async def scenario():
        tests = identities(100)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test(size=(85, 30)) as pilot:
            await pilot.pause()
            await pilot.press('slash')
            table = app.screen.query_one(TestTable)
            control = app.screen.query_one(SearchInput)
            control.value = '::test_'
            await pilot.pause()
            await pilot.press('j', 'down', 'k', 'up')
            assert table.cursor_row == 0
            assert control.value == '::test_'
            await pilot.press('shift+down', 'shift+up', 'shift+down', 'shift+k', 'shift+j')
            assert table.selected_row_indices == (0, 1)
            await pilot.press('ctrl+shift+down')
            assert table.selected_row_indices == tuple(range(100))
            await pilot.press('ctrl+shift+up')
            assert table.selected_row_indices == (0,)
            await pilot.press('shift+pagedown')
            assert len(table.selected_row_indices) > 1
            await pilot.press('shift+pageup')
            assert table.cursor_row == 0
            await pilot.press('pagedown')
            assert table.cursor_row > 0 and table.range_anchor is None
            await pilot.press('pageup')
            assert table.cursor_row == 0
            control.selection = Selection(0, len(control.value))
            control.post_message(events.Paste('test_01'))
            await pilot.pause()
            assert control.value == 'test_01'
            assert row_keys(table)[0] == tests[1].id
            await pilot.press('down')
            assert table.cursor_row == 1
            control.value = '['
            await pilot.pause()
            assert table.cursor_row == 1
            assert row_keys(table)[1] == 'search:other'
            control.value = 'test_0[12]'
            await pilot.pause()
            assert table.cursor_row == 0
            await pilot.press('tab', 'shift+tab')
            assert app.focused is control
            await pilot.resize_terminal(55, 16)
            await pilot.pause()
            assert control.region.height == 1
            assert app.screen.query_one(SearchBar).region.bottom <= app.screen.region.bottom
            await pilot.press('escape')
            assert row_keys(table)[table.cursor_row] == tests[1].id
    asyncio.run(scenario())


def test_search_typing_does_not_start_operation_or_cycle_accounts(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.choose = AsyncMock()
            await pilot.press('slash', 's', 'q', 'b', 'c', 'slash')
            assert app.screen.kind == 'tests'
            assert app.screen.query_one(SearchInput).value == 'sqbc/'
            app.choose.assert_not_awaited()
            assert not app.screen.check_action('start_selected', ())
            assert not app.screen.check_action('previous_account', ())
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['left', 'right', 'shift+left', 'shift+right',
                                'ctrl+left', 'ctrl+right', 'ctrl+shift+left', 'ctrl+shift+right'])
@pytest.mark.parametrize('search_key', ['slash', 'question_mark'])
def test_table_focused_horizontal_edits_match_native_query_editing(tmp_path, key, search_key):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities(5))
        async with app.run_test(size=(90, 25)) as pilot:
            await pilot.pause()
            await pilot.press(search_key)
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = '::test_'
            control.selection = Selection.cursor(3)
            await pilot.pause()
            await pilot.press(key)
            expected = control.selection
            assert expected != Selection.cursor(3)
            control.selection = Selection.cursor(3)
            table.focus()
            await pilot.pause()
            cursor, selected = table.cursor_row, app.selection.selected_ids.copy()
            await pilot.press(key)
            assert app.focused is control
            assert control.selection == expected and control.value == '::test_'
            assert table.cursor_row == cursor and table.range_anchor is None
            assert app.selection.selected_ids == selected
            await pilot.press('tab')
            assert app.focused is table
            await pilot.press('down')
            assert table.cursor_row == min(cursor + 1, table.row_count - 1)
            assert control.selection == expected
    asyncio.run(scenario())


def test_search_table_focus_space_ranges_typing_and_exit_message(tmp_path):
    async def scenario():
        tests = identities(5)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('slash')
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = '::test_'
            await pilot.pause()
            await pilot.press('tab')
            assert app.focused is table
            await pilot.press('j', 'k', 'shift+j')
            assert table.selected_row_indices == (0, 1)
            await pilot.press('space')
            assert app.selection.selected_ids == {test.id for test in tests[2:]}
            assert control.value == '::test_'
            assert app.focused is control  # Row toggles return to editing.
            await pilot.press('tab', 'shift+k', 'down')
            assert app.focused is table
            assert table.range_anchor is None
            control.selection = Selection(0, len(control.value))
            await pilot.press('h')
            assert app.focused is control and control.value == 'h'
            assert control.selection.is_empty
            await pilot.press('space', 'l')
            assert control.value == 'h l'
            control.post_message(SearchInput.Exit())
            await pilot.pause()
            assert app.screen.test_search is None
    asyncio.run(scenario())


def test_search_bulk_toggle_and_invalid_query_keep_stable_range(tmp_path):
    async def scenario():
        tests = identities(4)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('slash')
            control = app.screen.query_one(SearchInput)
            table = app.screen.query_one(TestTable)
            control.value = '::test_'
            await pilot.pause()
            await pilot.press('shift+down', 'enter')
            assert table.selected_row_indices == (0, 1)
            assert app.selection.selected_ids == {tests[2].id, tests[3].id}
            control.value = '['
            await pilot.pause()
            assert table.selected_row_indices == (0, 1)
            await pilot.press('enter')
            assert app.selection.selected_ids == {test.id for test in tests}
            assert table.selected_row_indices == (0, 1)
            app.selection.toggle(tests[2].id)
            app.refresh_rows()
            assert str(table.get_row_at(2)[0]) == ''
            assert table.selected_row_indices == (0, 1)
            app.selection.toggle(tests[2].id)
            app.refresh_rows()
            assert str(table.get_row_at(2)[0]) == '✓'
            control.value = 'test_00'
            await pilot.pause()
            assert table.range_anchor is None
            assert row_keys(table) == [tests[0].id, 'search:other']
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['selection', 'value'])
def test_async_search_copy_preserves_newer_selection(tmp_path, monkeypatch, change):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities(2))
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_copy(owner, value):
            assert owner is app and value == 'ab'
            started.set()
            await release.wait()
            return True
        monkeypatch.setattr('api_agent.tui.app.copy_text', delayed_copy)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('slash')
            control = app.screen.query_one(SearchInput)
            control.value = 'abcd'
            control.selection = Selection(0, 2)
            await pilot.pause()
            task = asyncio.create_task(app.screen.action_copy_value())
            await started.wait()
            if change == 'value':
                control.value = 'wxyz'
            control.selection = Selection(2, 4)
            release.set()
            await task
            assert control.selection == Selection(2, 4)
            assert control.selected_text == ('yz' if change == 'value' else 'cd')
    asyncio.run(scenario())
