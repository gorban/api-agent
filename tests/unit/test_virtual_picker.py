"""Picker coordinates stay global while presentation is viewport bounded."""
import asyncio

import pytest

from api_agent.tui.virtual_picker import VirtualPicker
from test_test_table import identities
from test_tui import fixture


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_virtual_picker_far_rows_and_viewport_work(tmp_path, monkeypatch, mode):
    async def scenario():
        tests = identities(1200)
        app = fixture(tmp_path, mode, collector=lambda *a: tests)
        calls = 0
        original = app.selection_cells

        def counted(row, estimator):
            nonlocal calls
            calls += 1
            return original(row, estimator)

        monkeypatch.setattr(app, 'selection_cells', counted)
        async with app.run_test(size=(100, 25)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(VirtualPicker)
            assert table.row_count > len(tests)
            assert table.ordered_columns[1].width < 100
            assert not table._data
            assert calls < 500
            calls = 0
            await pilot.resize_terminal(110, 30)
            await pilot.pause()
            assert calls < 500
            calls = 0
            last = table.row_count - 1
            table.move_cursor(row=last)
            await pilot.pause()
            assert calls < 500
            assert table.ordered_rows[last].key.value == tests[-1].id
            assert tests[-1].id in str(table.get_row_at(last)[1])
            await pilot.press('enter')
            assert tests[-1].id not in app.selection.selected_ids
            app.selection.selected_ids.add(tests[-1].id)
            app.refresh_rows()
            assert str(table.get_row_at(last)[0]) == '✓'
            key = table.ordered_rows[last].key
            column = list(table.columns)[1]
            table.update_cell(key, column, 'temporary label')
            assert table.get_cell(key, column) == 'temporary label'
            app.refresh_rows()
            assert tests[-1].id in str(table.get_cell(key, column))
            calls = 0
            await pilot.press('ctrl+a')
            await pilot.pause()
            assert len(table.selected_row_keys) == table.row_count
            assert table.selected_row_keys[0] == 'all'
            assert table.selected_row_keys[-1] == tests[-1].id
            assert calls < 500
            calls = 0
            label = app.screen.selected_test_name()
            assert tests[0].id in label and tests[-1].id in label
            assert calls == 0
    asyncio.run(scenario())


def test_search_other_disappearance_clears_stale_global_range(tmp_path):
    async def scenario():
        from api_agent.tui.search import SearchInput

        tests = identities(10)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test(size=(100, 25)) as pilot:
            await pilot.pause()
            await pilot.press('slash')
            app.screen.query_one(SearchInput).value = 'test_00'
            await pilot.pause()
            table = app.screen.query_one(VirtualPicker)
            assert table.ordered_rows[-1].key.value == 'search:other'
            table.move_cursor(row=table.row_count - 1)
            table.focus()
            await pilot.press('shift+down')
            assert table.range_anchor is not None
            await pilot.press('enter')
            await pilot.pause()
            assert table.row_count == 1
            assert table.range_anchor is None
            assert table.selected_row_keys == (tests[0].id,)
            await pilot.press('enter')
            assert not app.selection.selected_ids
    asyncio.run(scenario())


def test_virtual_picker_table_events_and_invalid_keys(tmp_path, monkeypatch):
    async def scenario():
        from textual.widgets import DataTable
        from textual.widgets._data_table import CellDoesNotExist, RowDoesNotExist

        tests = identities(60)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test(size=(100, 25)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(VirtualPicker)
            table.move_cursor(row=table.row_count - 1)
            await pilot.pause()
            messages = []
            post_message = table.post_message
            def capture(message):
                messages.append(message)
                return post_message(message)
            monkeypatch.setattr(table, 'post_message', capture)
            table.cursor_type = 'cell'
            table._post_selected_message()
            event = messages[-1]
            assert isinstance(event, DataTable.CellSelected)
            assert event.coordinate.row == table.row_count - 1
            assert event.cell_key.row_key.value == tests[-1].id
            table.cursor_type = 'column'
            table._post_selected_message()
            assert isinstance(messages[-1], DataTable.ColumnSelected)
            table.cursor_type = 'none'
            before = len(messages)
            table._post_selected_message()
            assert len(messages) == before
            with pytest.raises(RowDoesNotExist):
                table.get_row('missing')
            with pytest.raises(CellDoesNotExist):
                table.get_cell(tests[-1].id, 'missing')
            with pytest.raises(CellDoesNotExist):
                table.update_cell('missing', 'missing', 'x')
            with pytest.raises(ValueError):
                table.set_picker_rows((app.selection.groups()[0],) * 2)
            table.clear()
            assert table.row_count == 0
            table._post_selected_message()
            with pytest.raises(RowDoesNotExist):
                table.get_row(tests[-1].id)
    asyncio.run(scenario())


def test_bulk_deselection_compacts_nonempty_set_without_replacing_it():
    from api_agent.testing.selection import SelectionModel

    tests = identities(5000)
    model = SelectionModel(tests)
    selected = model.selected_ids
    model.toggle_members(test.id for test in tests[:-3])
    assert model.selected_ids is selected
    assert selected == {test.id for test in tests[-3:]}
    assert selected.__sizeof__() <= set(selected).__sizeof__() * 2
    assert model.groups()[-1].selected


def test_long_unicode_label_tails_remain_renderable(tmp_path):
    async def scenario():
        from rich.style import Style
        from api_agent.testing.collection import TestIdentity
        from api_agent.tui.app import picker_label

        prefix = 'base::tests/example.py::test_' + '界' * 500
        ids = [prefix + suffix for suffix in ('first', 'second')]
        tests = [TestIdentity(name, 'base', 'tests/example.py', name) for name in ids]
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test(size=(100, 25)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(VirtualPicker)
            column = table.ordered_columns[1]
            rendered = []
            for identity in ids:
                index = next(i for i, row in enumerate(table.ordered_rows) if row.key.value == identity)
                segments = table._render_cell(index, 1, Style(), column.get_render_width(table))
                rendered.append(''.join(segment.text for line in segments for segment in line))
            assert picker_label(ids[0])[-64:] in rendered[0]
            assert picker_label(ids[1])[-64:] in rendered[1]
            assert rendered[0] != rendered[1]
    asyncio.run(scenario())
