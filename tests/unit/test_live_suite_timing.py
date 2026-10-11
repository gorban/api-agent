"""Suite clocks and right-edge previews preserve scrolling and row identities."""
import asyncio

import pytest
from rich.style import Style
from textual.widgets import Static

from api_agent.testing.collection import TestIdentity
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.runner import UnitRunResult
from api_agent.tui.test_results import elapsed_time
from api_agent.tui.test_table import TestTable
from test_tui import fixture


@pytest.mark.parametrize('seconds,expected', [(0, '0s'), (.02, '0.02s'), (59, '59s'),
                                             (60, '1m'), (61.9, '1m 1s'), (3600, '60m')])
def test_compact_elapsed_units(seconds, expected):
    assert elapsed_time(seconds) == expected


def test_suite_clock_includes_pending_and_freezes_after_teardown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('api_agent.testing.lifecycle.monotonic', lambda: clock[0])
    run = TestRunLifecycle()
    clock[0] = 161.5
    assert run.elapsed == 61.5 and run.active
    run.finish()
    clock[0] = 900
    run.finish()
    assert run.elapsed == 61.5 and not run.active


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_clock_updates_without_test_events_and_hides_at_completion(tmp_path, monkeypatch, mode):
    async def scenario():
        identity = TestIdentity('base::long.py::test_pending', 'base', 'long.py', 'test_pending')
        app = fixture(tmp_path, mode, collector=lambda *a: (identity,))
        clock = [0.0]
        monkeypatch.setattr('api_agent.testing.lifecycle.monotonic', lambda: clock[0])
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.test_run = TestRunLifecycle()
            app.unit_rows = {identity.id: ('PENDING', identity.id, '')}
            app.show_unit_results(app.unit_progress())
            await pilot.pause()
            screen = app.screen
            timer = screen.query_one('#suite-elapsed', Static)
            table = screen.query_one(TestTable)
            assert timer.display and timer.content == '0s'
            assert '1 pending' in screen.query_one('#body', Static).content.plain
            assert timer.region.y == screen.query_one('#body').region.y
            assert timer.region.right == screen.query_one('#live-progress').content_region.right
            clock[0] = 61
            await pilot.pause(.3)
            assert timer.content == '1m 1s' and app.focused is table
            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            assert timer.region.right == screen.query_one('#live-progress').content_region.right
            app.finish_test_run()
            await pilot.pause(.3)
            assert not timer.display
            app.unit_result = UnitRunResult(0, (), (), '', require_full_coverage=False)
            screen.refresh_suite_timer()
            assert not timer.display
    asyncio.run(scenario())


def test_real_unit_start_clock_precedes_recollection(tmp_path, monkeypatch):
    async def scenario():
        identity = TestIdentity('base::t.py::test_pending', 'base', 't.py', 'test_pending')
        app = fixture(tmp_path, 'unit', collector=lambda *a: (identity,))
        held = asyncio.Event()
        entered = asyncio.Event()
        async def discover(cancel):
            entered.set()
            await held.wait()
            return (identity,)
        app.unit_runner = lambda *a, **kw: UnitRunResult(0, (), (), '', require_full_coverage=False)
        clock = [0.0]
        monkeypatch.setattr('api_agent.testing.lifecycle.monotonic', lambda: clock[0])
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.discover_tests = discover
            app.start_unit_run()
            await entered.wait()
            clock[0] = 12
            await pilot.pause(.3)
            assert app.screen.query_one('#suite-elapsed', Static).content == '12s'
            assert app.unit_rows[identity.id][0] == 'PENDING'
            held.set()
            await pilot.pause(.3)
            assert not app.test_run_active()
            assert not app.screen.query_one('#suite-elapsed').display
    asyncio.run(scenario())


def test_timing_preview_scroll_resize_and_mouse_metadata(tmp_path):
    async def scenario():
        name = 'base::long_file_name.py::test_' + 'long_name_' * 25
        app = fixture(tmp_path)
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            app.show('test-results', 'Results', 'Progress', rows=[
                ('one', ('RUNNING', name, '1m 2s elapsed; timeout 3m — ' + 'Long component ' * 15)),
                ('two', ('PASSED', name + 'other', '7.042s'))])
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            width = table.scrollable_content_region.width
            header, row = table.render_line(0), table.render_line(1)
            assert header.text[:width].endswith(' Execution time ')
            assert '…' in header.text[:width]
            assert row.text[:width].endswith(' 1m 2s elapsed; ')
            assert table.render_line(2).text[:width].endswith(' 7.042s         ')
            assert '…' not in table.render_line(5).text
            segments = tuple(row.crop(width - 16, width))
            assert all(segment.style.meta['row'] == 0 and segment.style.meta['column'] == 2
                       for segment in segments)
            table.scroll_to(x=50, animate=False)
            await pilot.pause()
            scrolled = table.render_line(1).text[:width]
            assert scrolled != row.text[:width]
            assert not scrolled.startswith('…') and scrolled.endswith(' 1m 2s elapsed; ')
            table.action_extend_range(1)
            assert table.selected_row_keys == ('one', 'two')
            assert table.get_row_at(0)[1] == name
            assert table.render_line(1).crop(width - 16, width).text == ' 1m 2s elapsed; '
            start = sum(column.get_render_width(table) for column in table.ordered_columns[:-1])
            table.scroll_to(x=start, animate=False)
            await pilot.pause()
            assert 'Long component' in table.render_line(1).text[:width]
            assert '…' not in table.render_line(1).text[:width]
            table.scroll_to(x=table.max_scroll_x, animate=False)
            await pilot.pause()
            assert table.render_line(1).text[:width].rstrip().endswith('Long component')
            await pilot.resize_terminal(800, 30)
            await pilot.pause()
            table.scroll_to(x=0, animate=False)
            await pilot.pause()
            assert '…' not in table.render_line(0).text
            assert 'Execution time' in table.render_line(0).text
            # Extremely small viewports keep native behavior rather than drawing
            # an overlay wider than the viewport.
            await pilot.resize_terminal(18, 30)
            await pilot.pause()
            assert '…' not in table.render_line(0).text
            table.clear(columns=True)
            assert table._render_line(0, 0, table.size.width, Style()).text.strip() == ''
    asyncio.run(scenario())
