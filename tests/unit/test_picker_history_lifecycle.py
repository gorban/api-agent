"""History completion respects the real picker DOM lifecycle."""
import asyncio
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import Mock

from textual.widgets import DataTable

from api_agent.tui.app import WorkbenchScreen
from test_historical_cost_tui import Snapshot
from test_test_table import identities
from test_tui import fixture


def test_history_finishes_before_picker_composition(tmp_path, monkeypatch):
    snapshot, replacement = Snapshot(1), Snapshot(2)
    snapshot.synchronize = lambda: replacement
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=lambda *args: snapshot))
    compose = WorkbenchScreen._compose
    observed = []
    async def hold_picker_composition(screen):
        if screen.kind == 'tests':
            for worker in list(screen.app.workers):
                if worker.group == 'cost-history':
                    await worker.wait()
            observed.append(screen.app.cost_estimator is replacement)
        await compose(screen)
    monkeypatch.setattr(WorkbenchScreen, '_compose', hold_picker_composition)
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert observed == [True]
            table = app.screen.query_one(DataTable)
            assert table.row_count
            assert all(str(table.get_row_at(i)[4]) == '2.000000' for i in range(table.row_count))
    asyncio.run(scenario())


@pytest.mark.parametrize('transition', ['replacement', 'selection', 'modal', 'running', 'shutdown'])
def test_deferred_history_redraw_retains_picker_ownership(tmp_path, monkeypatch, transition):
    from api_agent.tui.app import SettingHelpDialog
    snapshot = Snapshot(1)
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=lambda *args: snapshot))
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            picker, selection = app.screen, app.selection
            queued = []
            monkeypatch.setattr(picker, 'call_after_refresh', lambda callback, *args: queued.append((callback, args)))
            await app.synchronize_cost_history(snapshot, selection)
            callback, args = queued.pop()
            assert args == (picker, selection)
            redraw = Mock()
            monkeypatch.setattr(app, 'refresh_rows', redraw)
            if transition == 'replacement':
                app.show('tests', 'Replacement', rows=picker.rows)
                await pilot.pause()
                assert app.selection is selection
            elif transition == 'selection':
                app.selection = object()
            elif transition == 'modal':
                app.push_screen(SettingHelpDialog('Information', 'Controlled modal'))
                await pilot.pause()
            elif transition == 'running':
                monkeypatch.setattr(app, 'test_run_active', lambda: True)
            if transition != 'shutdown':
                callback(*args)
                redraw.assert_not_called()
                if transition == 'running':
                    monkeypatch.setattr(app, 'test_run_active', lambda: False)
                if transition == 'modal':
                    assert app._pending_cost_refresh is selection
                    app.pop_screen()
                    await pilot.pause()
                    resume, resume_args = queued.pop()
                    resume(*resume_args)
                    redraw.assert_called_once_with()
        if transition == 'shutdown':
            callback(*args)
            redraw.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['unmounted', 'stopped', 'retired'])
def test_deferred_history_never_queries_unready_dom(tmp_path, state):
    app = fixture(tmp_path)
    selection = app.selection
    screen = SimpleNamespace(is_attached=True, is_mounted=state != 'unmounted', is_running=state != 'stopped')
    app._screen_stacks[app._current_mode] = [object() if state == 'retired' else screen]
    app.refresh_rows = Mock()
    app.refresh_picker_costs(screen, selection)
    app.refresh_rows.assert_not_called()
