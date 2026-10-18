"""Independent picker screens retain their own deferred latest-value redraws."""
import asyncio
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.tui.app import SettingHelpDialog
from api_agent.tui.test_table import TestTable
from test_historical_cost_tui import Snapshot
from test_test_table import identities
from test_tui import fixture


def assert_cost_rows(picker, value):
    table = picker.query_one(TestTable)
    assert table.row_count > 0
    assert [str(table.get_row_at(i)[4]) for i in range(table.row_count)] == [f'{value:.6f}'] * table.row_count


@pytest.fixture
def cached_picker(tmp_path, monkeypatch):
    estimate = Snapshot(7)
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=lambda *args: estimate))
    return fixture(tmp_path, 'integration', collector=lambda *args: identities(2)), estimate


def test_settings_followup_refreshes_both_independently_covered_pickers(tmp_path, monkeypatch):
    started, proceed = Event(), Event()
    def build(context, tests, values):
        estimate = Snapshot(values['TEST_COST_HISTORY_DAYS'])
        if estimate.value == 14:
            def synchronize():
                started.set()
                assert proceed.wait(5)
                return estimate
            estimate.synchronize = synchronize
        return estimate
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history', SimpleNamespace(estimator_for=build))
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2), fields={
            'TEST_COST_HISTORY_DAYS': {'type': 'int', 'default': 7, 'acknowledge_default': False}})
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                original, selected, previous = app.screen, app.selection, app.cost_estimator
                assert_cost_rows(original, 7)
                app.push_screen(SettingHelpDialog('Settings changed', 'Original picker is covered.'))
                await pilot.pause()
                app.session.set_value('TEST_COST_HISTORY_DAYS', '14')
                previous.synchronize = lambda: Snapshot(101)
                await app.synchronize_cost_history(previous, selected)
                assert await asyncio.to_thread(started.wait, 5)
                await app.show('tests', 'Another compatible picker', rows=original.rows)
                replacement = app.screen
                await pilot.pause()
                assert_cost_rows(replacement, 7)
                app.push_screen(SettingHelpDialog('History refresh', 'Replacement picker is covered.'))
                await pilot.pause()
                proceed.set()
                await app.workers.wait_for_complete()

                app.pop_screen()
                await pilot.pause()
                assert app.screen is replacement
                assert_cost_rows(replacement, 14)
                app.pop_screen()
                await pilot.pause()
                app.pop_screen()
                await pilot.pause()
                assert app.screen is original and app.selection is selected
                assert app.cost_estimator.value == 14
                assert_cost_rows(original, 14)
        finally:
            proceed.set()
    asyncio.run(scenario())


def test_repeated_updates_coalesce_to_latest_and_reject_out_of_order_result(cached_picker, monkeypatch):
    app, initial = cached_picker
    entered, release = Event(), Event()
    async def scenario():
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                picker, selected = app.screen, app.selection
                def slow():
                    entered.set()
                    assert release.wait(5)
                    return Snapshot(99)
                initial.synchronize = slow
                stale = asyncio.create_task(app.synchronize_cost_history(initial, selected))
                assert await asyncio.to_thread(entered.wait, 5)
                app.push_screen(SettingHelpDialog('Refreshing history', 'Retain one latest redraw.'))
                await pilot.pause()
                second, third, latest = Snapshot(8), Snapshot(12), Snapshot(15)
                app.cost_estimator = second
                second.synchronize = lambda: third
                await app.synchronize_cost_history(second, selected)
                third.synchronize = lambda: latest
                await app.synchronize_cost_history(third, selected)
                release.set()
                await stale
                assert app.cost_estimator is latest
                render = app.refresh_rows
                redraw = Mock(side_effect=render)
                monkeypatch.setattr(app, 'refresh_rows', redraw)
                app.pop_screen()
                await pilot.pause()
                assert_cost_rows(picker, 15)
                redraw.assert_called_once_with()
                app.push_screen(SettingHelpDialog('No new history', 'Resume has no repeated duty.'))
                await pilot.pause()
                app.pop_screen()
                await pilot.pause()
                redraw.assert_called_once_with()
        finally:
            release.set()
    asyncio.run(scenario())


def test_retired_picker_clears_pending_selection_on_unmount(cached_picker):
    app, _ = cached_picker
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            retired = app.screen
            retired._pending_cost_refresh = app.selection
            await app.show('tests', 'New picker replaces retired screen', rows=retired.rows)
            await pilot.pause()
            assert not retired.is_attached and not retired.is_running
            assert retired._pending_cost_refresh is None
    asyncio.run(scenario())


def test_shutdown_clears_each_owner_and_refuses_late_callback_or_worker(cached_picker, monkeypatch):
    app, estimate = cached_picker
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            original, selected = app.screen, app.selection
            app.push_screen(SettingHelpDialog('Original covered', 'Preserve its owner.'))
            await pilot.pause()
            app.refresh_picker_costs(original, selected)
            await app.show('tests', 'Second picker', rows=original.rows)
            current = app.screen
            await pilot.pause()
            app.push_screen(SettingHelpDialog('Second covered', 'Preserve the second owner.'))
            await pilot.pause()
            app.refresh_picker_costs(current, selected)
            assert original._pending_cost_refresh is current._pending_cost_refresh is selected

            await app.on_unmount()

            assert original._pending_cost_refresh is current._pending_cost_refresh is None
            render = Mock(side_effect=AssertionError('Shutdown queried picker rows'))
            monkeypatch.setattr(app, 'refresh_rows', render)
            app.refresh_picker_costs(original, selected)
            estimate.synchronize = lambda: Snapshot(999)
            await app.synchronize_cost_history(estimate, selected)
            assert original._pending_cost_refresh is current._pending_cost_refresh is None
            assert app.cost_estimator is estimate
            render.assert_not_called()
    asyncio.run(scenario())
