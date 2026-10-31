import asyncio
from unittest.mock import AsyncMock
import pytest

from api_agent.testing.collection import CollectionError, TestIdentity, select_tests
from api_agent.tui.app import picker_label
from api_agent.tui.test_table import TestTable
from api_agent.tui.search import SearchInput
from test_tui import fixture


def test_picker_label_bounds_long_unicode_but_preserves_short_values():
    assert picker_label('x' * 256) == 'x' * 256
    assert len(picker_label('\ud800' * 257)) < 256


def test_replaced_parameter_identity_does_not_alias_an_old_selector():
    old = 'base::tests/example.py::test_limit[' + 'x' * 500 + ']'
    new = TestIdentity('base::tests/example.py::test_limit[over-limit]', 'base',
                       'tests/example.py', 'tests/example.py::test_limit[over-limit]')
    assert select_tests([new], [new.id]) == [new]
    with pytest.raises(CollectionError, match='Unknown or empty test selector'):
        select_tests([new], [old])


def test_long_test_labels_keep_full_copy_search_and_selection(tmp_path, monkeypatch):
    prefix = 'base::tests/example.py::test_' + 'x' * 500
    first, second = prefix + 'first', prefix + 'second'
    tests = [TestIdentity(name, 'base', 'tests/example.py', name.removeprefix('base::'))
             for name in (first, second)]

    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *args: tests)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            rows = {row.key.value: i for i, row in enumerate(table.ordered_rows)}
            first_index, second_index = rows[first], rows[second]
            assert len(str(table.get_row_at(first_index)[1])) < 256
            assert table.get_row_at(first_index)[1] != table.get_row_at(second_index)[1]
            table.move_cursor(row=first_index)
            table.focus()
            assert app.screen.selected_test_name() == first
            await app.show('tests', 'Replacement', rows=app.screen.rows)
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            table.move_cursor(row=first_index)
            table.focus()
            assert app.screen.selected_test_name() == first
            app.screen.full_test_labels.clear()
            assert app.screen.selected_test_name() == first
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, first)
            table.action_extend_range(1)
            assert app.screen.selected_test_name() == first + '\n' + second
            await pilot.press('ctrl+c')
            copy.assert_awaited_with(app, first + '\n' + second)
            await pilot.press('escape', 'slash')
            control = app.screen.query_one(SearchInput)
            control.value = 'second$'
            await pilot.pause()
            assert second in [row.key.value for row in table.ordered_rows]
            assert first not in [row.key.value for row in table.ordered_rows]
            table.move_cursor(row=0)
            assert app.screen.selected_test_name() == second
            assert app.selection.selected_ids == {first, second}
    asyncio.run(scenario())


def test_group_and_search_other_copy_the_full_visible_label(tmp_path):
    tests = [TestIdentity(f'base::tests/example.py::test_{name}', 'base', 'tests/example.py',
                          f'tests/example.py::test_{name}') for name in ('first', 'second')]

    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *args: tests)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            index = next(i for i, row in enumerate(table.ordered_rows) if row.key.value.startswith('file:'))
            table.move_cursor(row=index)
            table.focus()
            assert app.screen.selected_test_name() == 'base::tests/example.py'
            await pilot.press('slash')
            app.screen.query_one(SearchInput).value = 'first$'
            await pilot.pause()
            index = next(i for i, row in enumerate(table.ordered_rows) if row.key.value == 'search:other')
            table.move_cursor(row=index)
            assert app.screen.selected_test_name() == '1 other tests'
    asyncio.run(scenario())
