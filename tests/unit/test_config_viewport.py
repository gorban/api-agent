"""Configuration rebuilds keep the focused row at its rendered position."""
import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input

from test_tui import fixture


class TableApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(cursor_type='row')

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column('Setting', width=12)
        table.add_column('Value', width=45)
        add_rows(table, [f'ROW_{i:02}' for i in range(30)])


def add_rows(table, names):
    for name in names:
        table.add_row(name, 'value ' + name, key=name)


def rendered_line(table, name):
    return next(i for i in range(table.size.height) if name in table.render_line(i).text)


def screen_line(table, name):
    return table.scrollable_content_region.y + rendered_line(table, name)


@pytest.mark.parametrize('replacement,anchor', [
    ([f'ROW_{i:02}' for i in range(30)], 'ROW_15'),
    (['NEW_ROW'] + [f'ROW_{i:02}' for i in range(30)], 'ROW_15'),
    ([f'ROW_{i:02}' for i in range(30) if i != 15], 'ROW_16'),
])
def test_rebuild_retains_rendered_offset_and_horizontal_scroll(replacement, anchor):
    from api_agent.tui.config_viewport import capture_viewport, restore_viewport

    async def exercise():
        app = TableApp()
        async with app.run_test(size=(40, 12)) as pilot:
            table = app.query_one(DataTable)
            table.move_cursor(row=15)
            table.scroll_to(x=8, y=11, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'ROW_15')
            horizontal = table.scroll_x
            state = capture_viewport(table)
            table.clear()
            add_rows(table, replacement)
            restore_viewport(table, state)
            await pilot.pause()
            assert screen_line(table, anchor) == before
            assert table.scroll_x == horizontal
            assert table.ordered_rows[table.cursor_row].key.value == anchor
    asyncio.run(exercise())


def test_layout_shift_retains_absolute_screen_position():
    from api_agent.tui.config_viewport import capture_viewport, restore_viewport

    async def exercise():
        app = TableApp()
        async with app.run_test(size=(40, 14)) as pilot:
            table = app.query_one(DataTable)
            table.move_cursor(row=15)
            table.scroll_to(y=10, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'ROW_15')
            state = capture_viewport(table)
            table.styles.margin_top = 2
            await pilot.pause()
            restore_viewport(table, state)
            await pilot.pause()
            assert screen_line(table, 'ROW_15') == before
    asyncio.run(exercise())


def test_shrinking_viewport_clamps_row_to_last_visible_line():
    from api_agent.tui.config_viewport import capture_viewport, restore_viewport

    async def exercise():
        app = TableApp()
        async with app.run_test(size=(40, 15)) as pilot:
            table = app.query_one(DataTable)
            table.move_cursor(row=15)
            table.scroll_to(y=4, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'ROW_15')
            state = capture_viewport(table)
            await pilot.resize_terminal(40, 8)
            await pilot.pause()
            restore_viewport(table, state)
            await pilot.pause()
            assert before > table.scrollable_content_region.bottom - 1
            assert screen_line(table, 'ROW_15') == table.scrollable_content_region.bottom - 1
    asyncio.run(exercise())


def test_delayed_review_refresh_retains_latest_row_after_membership_changes(tmp_path, monkeypatch):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            refresher = screen.configuration_refresher
            if refresher.worker:
                await refresher.worker.wait()
            entered, release = asyncio.Event(), asyncio.Event()
            original_run = app.background_reads.run
            original_rows = app.configuration_rows
            changed = False

            async def delayed(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original_run(*args, **kwargs)

            def rows(*args, **kwargs):
                result = original_rows(*args, **kwargs)
                return [row for row in result if not changed or row[0] != 'FIELD_00']

            monkeypatch.setattr(app.background_reads, 'run', delayed)
            monkeypatch.setattr(app, 'configuration_rows', rows)
            table = screen.query_one(DataTable)
            refresher.request()
            try:
                await asyncio.wait_for(entered.wait(), 5)
                index = [row.key.value for row in table.ordered_rows].index('FIELD_15')
                table.move_cursor(row=index)
                table.scroll_to(y=index - 3, animate=False, immediate=True)
                await pilot.pause()
                before = screen_line(table, 'FIELD_15')
                changed = True
            finally:
                release.set()
            await refresher.worker.wait()
            await pilot.pause()
            assert 'FIELD_00' not in [row.key.value for row in table.ordered_rows]
            assert screen_line(table, 'FIELD_15') == before
    asyncio.run(exercise())


def test_accept_default_keeps_actual_review_row_position(tmp_path):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            names = [row.key.value for row in table.ordered_rows]
            index = names.index('FIELD_15')
            table.move_cursor(row=index)
            table.scroll_to(y=index - 3, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'FIELD_15')
            await app.accept_default()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert screen_line(table, 'FIELD_15') == before
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['edit', 'unset'])
def test_review_edit_and_unset_keep_rendered_position(tmp_path, action):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        app.review_names.update(fields)
        if action == 'unset':
            app.session.set_value('FIELD_15', 'saved', persist=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            index = [row.key.value for row in table.ordered_rows].index('FIELD_15')
            table.move_cursor(row=index)
            table.scroll_to(y=index - 3, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'FIELD_15')
            if action == 'edit':
                app.edit_setting()
                await pilot.pause()
                app.screen.query_one(Input).value = 'changed'
                await pilot.click('#submit')
            else:
                app.screen.action_unset_setting()
            await pilot.pause()
            assert screen_line(app.screen.query_one(DataTable), 'FIELD_15') == before
    asyncio.run(exercise())


def test_cleanup_delete_keeps_rendered_position(tmp_path):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        for name in fields:
            app.session.set_value(name, 'saved', persist=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_cleanup()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            index = [row.key.value for row in table.ordered_rows].index('FIELD_15')
            table.move_cursor(row=index)
            table.scroll_to(y=index - 3, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'FIELD_15')
            app.delete_assignment('FIELD_15')
            await pilot.pause()
            assert screen_line(table, 'FIELD_15') == before
    asyncio.run(exercise())


def test_empty_capture_and_restore_are_noops():
    from types import SimpleNamespace
    from api_agent.tui.config_viewport import capture_viewport, restore_viewport

    table = SimpleNamespace(ordered_rows=[], row_count=0)
    assert capture_viewport(table) is None
    restore_viewport(table, None)


def test_fallback_marks_and_stale_callbacks():
    from types import SimpleNamespace
    from api_agent.tui.config_viewport import Viewport, restore_viewport

    def row(key):
        return SimpleNamespace(key=SimpleNamespace(value=key), height=1)

    class Table:
        ordered_rows = [row('new'), row('other')]
        row_count = 2
        cursor_row = 0
        scrollable_content_region = SimpleNamespace(y=2, height=5)
        show_header = True
        header_height = 1
        callbacks = []
        marked = None
        calls = []

        def move_cursor(self, *, row, scroll):
            self.cursor_row = row

        def set_marked_keys(self, keys):
            self.marked = keys

        def call_after_refresh(self, callback):
            self.callbacks.append(callback)

        def scroll_to(self, **kwargs):
            self.calls.append(kwargs)

    table = Table()
    viewport = Viewport(('missing', 'other'), 0, 20, 7, ('new', 'missing'))
    restore_viewport(table, viewport)
    assert table.cursor_row == 1
    assert set(table.marked) == {'new', 'missing'}
    restore_viewport(table, Viewport(('gone',), 0, 2, 3))
    assert table.cursor_row == 0  # No old keys remain: clamp the old index.
    assert not table.marked
    table.callbacks[0]()
    assert table.calls == []
    table.callbacks[1]()
    assert table.calls == [{'x': 3, 'y': 0, 'animate': False, 'immediate': True}]
    table.row_count = 0
    restore_viewport(table, viewport)
    table.callbacks[1]()
    assert len(table.calls) == 1


def test_review_resize_keeps_absolute_row_position(tmp_path):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=15)
            table.scroll_to(y=12, animate=False, immediate=True)
            await pilot.pause()
            before = screen_line(table, 'FIELD_15')
            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            assert screen_line(table, 'FIELD_15') == before
    asyncio.run(exercise())


def test_explicit_review_row_takes_precedence_over_previous_viewport(tmp_path):
    async def exercise():
        fields = {f'FIELD_{i:02}': {'type': 'str', 'default': 'example'}
                  for i in range(30)}
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=15)
            await pilot.pause()
            app.show_configuration(selected_row='FIELD_05')
            await pilot.pause()
            assert app.focused_setting() == 'FIELD_05'
    asyncio.run(exercise())
