"""Keyboard ranges use synthetic test identities and mocked clipboard writes."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from textual.widgets import DataTable

from api_agent.testing.collection import TestIdentity
from api_agent.tui.test_table import TestTable
from test_tui import fixture


def identities(count=40):
    return tuple(TestIdentity(f'base::tests/test_example.py::test_{i:02d}[long-parameter-long-parameter-long-parameter]',
                              'base', 'tests/test_example.py', f'test_{i:02d}')
                 for i in range(count))


async def show_table(app, pilot, kind, count=40):
    await pilot.pause()
    if kind == 'test-results':
        app.show(kind, 'Results', 'Synthetic progress', rows=[
            (test.id, ('PASSED', test.id, '')) for test in identities(count)])
        await pilot.pause()
    return app.screen.query_one(TestTable)


@pytest.mark.parametrize('kind', ['tests', 'test-results'])
@pytest.mark.parametrize('platform,key', [('linux', 'ctrl+c'), ('darwin', 'ctrl+c')])
def test_range_copy_and_bulk_toggle(tmp_path, monkeypatch, kind, platform, key):
    async def scenario():
        tests = identities()
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(140, 40)) as pilot:
            table = await show_table(app, pilot, kind)
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            start = table.row_count - 5
            table.move_cursor(row=start)
            await pilot.pause()
            before = app.selection.selected_ids.copy()
            await pilot.press('shift+down', 'shift+down')
            await pilot.pause()
            assert table.selected_row_indices == (start, start + 1, start + 2)
            assert app.selection.selected_ids == before
            expected = '\n'.join(str(table.get_row_at(i)[1]) for i in table.selected_row_indices)
            await pilot.press(key)
            copy.assert_awaited_once_with(app, expected)
            assert table.selected_row_indices == (start, start + 1, start + 2)
            if kind == 'tests':
                keys = set(table.selected_row_keys)
                await pilot.press('enter')
                assert app.selection.selected_ids == before - keys
                await pilot.press('space')
                assert app.selection.selected_ids == before
                app.selection.toggle(next(iter(keys)))
                app.refresh_rows()
                await pilot.press('enter')
                assert app.selection.selected_ids == before
            else:
                await pilot.mouse_down('#body', offset=(0, 0))
                await pilot.hover('#body', offset=(8, 0))
                await pilot.mouse_up('#body', offset=(8, 0))
                await pilot.press(key)
                copy.assert_awaited_with(app, 'Synthetic')
                assert table.selected_row_indices == (start, start + 1, start + 2)
                await pilot.press(key)
                copy.assert_awaited_with(app, expected)
                table.focus()
            await pilot.press('shift+up')
            assert table.selected_row_indices == (start, start + 1)
            await pilot.press('shift+up', 'shift+up')
            assert table.selected_row_indices == (start - 1, start)
            await pilot.press('escape')
            assert table.selected_row_indices == (start - 1,)
            await pilot.press('shift+down', 'j')
            await pilot.pause()
            assert table.range_anchor is None
            assert len(table.selected_row_indices) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('keys,end', [
    (('ctrl+shift+up',), 'top'), (('super+shift+up',), 'top'),
    (('shift+home',), 'top'), (('ctrl+shift+down',), 'bottom'),
    (('super+shift+down',), 'bottom'), (('shift+end',), 'bottom'),
    (('ctrl+a',), 'all'), (('super+a',), 'all'),
])
@pytest.mark.parametrize('kind', ['tests', 'test-results'])
def test_range_edge_and_all_keys(tmp_path, kind, keys, end):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities())
        async with app.run_test(size=(100, 35)) as pilot:
            table = await show_table(app, pilot, kind)
            table.move_cursor(row=10)
            await pilot.pause()
            before = app.selection.selected_ids.copy()
            await pilot.press(*keys)
            await pilot.pause()
            expected = range(0, 11) if end == 'top' else range(10, table.row_count)
            if end == 'all':
                expected = range(table.row_count)
            assert table.selected_row_indices == tuple(expected)
            assert app.selection.selected_ids == before
            await pilot.press('shift+up' if end == 'top' else 'shift+down')
            assert table.selected_row_indices == tuple(expected)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['tests', 'test-results'])
@pytest.mark.parametrize('header', [False, True])
def test_range_pages_follow_actual_viewport_and_resize(tmp_path, kind, header):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities(100))
        async with app.run_test(size=(90, 30)) as pilot:
            table = await show_table(app, pilot, kind, 100)
            table.show_header = header
            await pilot.pause()
            assert table.show_horizontal_scrollbar
            table.move_cursor(row=40)
            await pilot.pause()
            page = table.scrollable_content_region.height - (table.header_height if header else 0)
            await pilot.press('shift+pagedown')
            assert table.cursor_row == 40 + page
            assert table.selected_row_indices == tuple(range(40, 41 + page))
            await pilot.press('shift+pageup')
            assert table.selected_row_indices == (40,)
            await pilot.resize_terminal(90, 40)
            await pilot.pause()
            larger_page = table.scrollable_content_region.height - (table.header_height if header else 0)
            assert larger_page > page
            await pilot.press('shift+pageup')
            assert table.cursor_row == 40 - larger_page
            assert table.selected_row_indices == tuple(range(40 - larger_page, 41))
    asyncio.run(scenario())


def test_range_rendering_clear_and_empty_table(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities())
        async with app.run_test(size=(140, 40)) as pilot:
            table = await show_table(app, pilot, 'test-results')
            table.move_cursor(row=1)
            await pilot.pause()
            normal = list(table.render_line(1))
            await pilot.press('shift+up')
            await pilot.pause()
            highlighted = list(table.render_line(1))
            assert normal != highlighted
            # Both range endpoints render with the same selected background.
            style0 = next(segment.style for segment in table.render_line(1) if 'PASSED' in segment.text)
            style1 = next(segment.style for segment in table.render_line(2) if 'PASSED' in segment.text)
            assert style0.bgcolor == style1.bgcolor
            # Mouse/programmatic cursor changes also clear a stale range.
            table.move_cursor(row=7)
            await pilot.pause()
            assert table.range_anchor is None and table.selected_row_indices == (7,)
            table.clear()
            assert table.range_anchor is None and table.selected_row_indices == ()
            assert table.selected_row_keys == ()
            table.action_extend_range(1)
            table.action_select_all_rows()
            table.styles.height = 1
            await pilot.pause()
            table.action_extend_page(1)
            assert table.selected_row_indices == ()
    asyncio.run(scenario())


def test_plain_navigation_keeps_caches_until_a_range_needs_clearing(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities())
        async with app.run_test(size=(140, 40)) as pilot:
            table = await show_table(app, pilot, 'test-results')
            table.move_cursor(row=3)
            await pilot.pause()
            original = table._clear_caches
            calls = []
            def clear():
                calls.append(True)
                original()
            monkeypatch.setattr(table, '_clear_caches', clear)
            table.action_clear_range()
            assert not calls
            await pilot.press('shift+down')
            await pilot.pause()
            highlighted = list(table.render_line(4))
            await pilot.press('down')
            await pilot.pause()
            assert table.range_anchor is None
            assert calls
            assert list(table.render_line(4)) != highlighted
    asyncio.run(scenario())


@pytest.mark.parametrize('key,edge', [('up', 0), ('k', 0), ('pageup', 0), ('ctrl+home', 0),
                                    ('home', 0), ('down', -1), ('j', -1), ('pagedown', -1),
                                    ('ctrl+end', -1), ('end', -1)])
def test_plain_navigation_clears_range_even_when_clamped(tmp_path, key, edge):
    async def scenario():
        tests = identities(5)
        app = fixture(tmp_path, 'unit', collector=lambda *a: tests)
        async with app.run_test(size=(140, 40)) as pilot:
            table = await show_table(app, pilot, 'tests', 5)
            await pilot.press('ctrl+a')
            if edge == 0:
                await pilot.press('escape', 'ctrl+shift+up')
            await pilot.pause()
            assert len(table.selected_row_indices) == table.row_count
            await pilot.press(key)
            await pilot.pause()
            assert table.range_anchor is None
            expected = tests[0].id if edge == 0 else tests[-1].id
            if edge == 0:
                # All tests is the first picker row; verify the single-row range.
                assert table.selected_row_keys == ('all',)
            else:
                assert table.selected_row_keys == (expected,)
                await pilot.press('enter')
                assert app.selection.selected_ids == {test.id for test in tests[:-1]}
    asyncio.run(scenario())
