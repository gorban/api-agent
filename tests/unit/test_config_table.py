"""ConfigTable selection behavior using only synthetic row identities."""
import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

from api_agent.tui.config_table import ConfigTable


class TableApp(App):
    def __init__(self, keys=('alpha', 'bravo', 'charlie', 'delta', 'echo')):
        super().__init__()
        self.keys = keys

    def compose(self) -> ComposeResult:
        yield ConfigTable(cursor_type='row', id='config')

    def on_mount(self):
        table = self.query_one(ConfigTable)
        table.add_column('Name')
        for key in self.keys:
            table.add_row(key, key=key)
        table.focus()


def test_shift_ranges_can_shrink_reverse_and_commit_to_disjoint_marks():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            table.move_cursor(row=1)
            await pilot.press('shift+down', 'shift+down')
            assert table.selected_row_keys == ('bravo', 'charlie', 'delta')
            await pilot.press('shift+up', 'shift+up', 'shift+up')
            assert table.selected_row_keys == ('alpha', 'bravo')
            await pilot.press('space')
            assert table.marked_keys == {'alpha', 'bravo'}
            assert table.range_anchor is None
            table.move_cursor(row=4)
            await pilot.press('space')
            assert table.selected_row_keys == ('alpha', 'bravo', 'echo')
            await pilot.press('space')
            assert table.selected_row_keys == ('alpha', 'bravo')
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['ctrl+a', 'super+a'])
def test_select_all_then_clear_with_escape(key):
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            await pilot.press(key)
            assert table.selected_row_keys == app.keys
            assert table.selection_explicit
            await pilot.press('space')
            assert table.marked_keys == set(app.keys)
            await pilot.press('escape')
            assert table.marked_keys == set()
            assert not table.selection_explicit
            assert table.selected_row_keys == (app.keys[table.cursor_row],)
    asyncio.run(scenario())


def test_plain_navigation_discards_range_but_retains_disjoint_marks():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            await pilot.press('space', 'down', 'shift+down')
            assert table.marked_keys == {'alpha', 'bravo', 'charlie'}
            await pilot.press('down')
            assert table.range_anchor is None
            assert table.marked_keys == {'alpha'}
            assert table.selected_row_keys == ('alpha',)
            assert table.cursor_row == 3
    asyncio.run(scenario())


def test_control_and_shift_click_rows_and_plain_click_preserves_marks():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            await pilot.pause()
            # Header occupies the first line; row y coordinates start at one.
            await pilot.click(table, offset=(2, 2), control=True)
            assert table.selected_row_keys == ('bravo',)
            await pilot.click(table, offset=(2, 4), control=True)
            assert table.selected_row_keys == ('bravo', 'delta')
            await pilot.click(table, offset=(2, 2), control=True)
            assert table.selected_row_keys == ('delta',)
            await pilot.click(table, offset=(2, 4), shift=True)
            assert table.selected_row_keys == ('bravo', 'charlie', 'delta')
            await pilot.click(table, offset=(2, 5))
            assert table.range_anchor is None
            assert table.selected_row_keys == ('delta',)
            assert table.cursor_row == 4
    asyncio.run(scenario())


def test_hidden_marks_remain_selected_and_visible_keys_are_ordered_first():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            table.set_marked_keys(('zulu-hidden', 'delta', 'alpha', 'hidden'))
            assert table.selection_explicit
            assert table.selected_row_keys == ('alpha', 'delta', 'hidden', 'zulu-hidden')
            table.remove_row('delta')
            assert table.selected_row_keys == ('alpha', 'delta', 'hidden', 'zulu-hidden')
            await pilot.press('escape')
            assert table.marked_keys == set()
            assert not table.selection_explicit
    asyncio.run(scenario())


def test_empty_table_selection_actions_are_safe_and_clear_resets_marks():
    async def scenario():
        app = TableApp(keys=())
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            await pilot.press('shift+down', 'ctrl+a', 'space', 'escape')
            assert table.selected_row_keys == ()
            assert not table.selection_explicit
            table.set_marked_keys(('hidden',))
            assert table.selected_row_keys == ('hidden',)
            table.clear()
            assert table.selected_row_keys == ()
            assert not table.selection_explicit
    asyncio.run(scenario())


def test_marked_rows_and_range_endpoints_are_highlighted():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            table.set_marked_keys(('alpha',))
            table.move_cursor(row=2)
            await pilot.press('shift+down')
            await pilot.pause()
            cursor = Coordinate(table.cursor_row, 0)
            assert table._should_highlight(cursor, Coordinate(0, 0), 'row')
            assert not table._should_highlight(cursor, Coordinate(1, 0), 'row')
            assert table._should_highlight(cursor, Coordinate(2, 0), 'row')
            assert table._should_highlight(cursor, Coordinate(3, 0), 'row')
            await pilot.press('escape')
            assert not table._should_highlight(cursor, Coordinate(0, 0), 'row')
            assert not table._should_highlight(cursor, Coordinate(2, 0), 'row')
    asyncio.run(scenario())


def test_repeated_shift_click_keeps_original_anchor_and_replaces_range():
    async def scenario():
        app = TableApp()
        async with app.run_test(size=(80, 20)) as pilot:
            table = app.query_one(ConfigTable)
            table.move_cursor(row=1)
            await pilot.pause()
            await pilot.click(table, offset=(2, 4), shift=True)
            assert table.selected_row_keys == ('bravo', 'charlie', 'delta')
            await pilot.click(table, offset=(2, 1), shift=True)
            assert table.range_anchor == 1
            assert table.selected_row_keys == ('alpha', 'bravo')
    asyncio.run(scenario())
