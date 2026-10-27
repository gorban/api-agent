"""A covered history redraw belongs to one concrete mounted picker."""
import asyncio
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.tui.app import SettingHelpDialog
from api_agent.testing.selection import SelectionModel
from test_historical_cost_tui import Snapshot
from test_test_table import identities
from test_tui import fixture


@pytest.fixture
def cached_picker(tmp_path, monkeypatch):
    estimate = Snapshot(4)
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=lambda *args, **options: estimate))
    return fixture(tmp_path, 'integration', collector=lambda *args: identities(3)), estimate


def defer_redraws(app, picker, monkeypatch):
    callbacks = []
    original = picker.call_after_refresh
    def defer(callback, *args):
        if callback == app.refresh_picker_costs:
            callbacks.append((callback, args))
            return True
        return original(callback, *args)
    monkeypatch.setattr(picker, 'call_after_refresh', defer)
    return callbacks


async def complete_under_modal(app, pilot, estimate, selection):
    entered, finish = Event(), Event()
    def synchronize():
        entered.set()
        assert finish.wait(5)
        return estimate
    estimate.synchronize = synchronize
    task = asyncio.create_task(app.synchronize_cost_history(estimate, selection))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        app.push_screen(SettingHelpDialog('History information', 'Selection remains covered.'))
        await pilot.pause()
        finish.set()
        await task
    finally:
        finish.set()
        await task


@pytest.mark.parametrize('delivery', ['queued-callback', 'covered-worker'])
def test_replacement_cannot_consume_covered_owners_pending_redraw(cached_picker, monkeypatch, delivery):
    app, estimate = cached_picker
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            owner, selected = app.screen, app.selection
            callbacks = defer_redraws(app, owner, monkeypatch)
            if delivery == 'queued-callback':
                await app.synchronize_cost_history(estimate, selected)
                callback, args = callbacks.pop()
                app.push_screen(SettingHelpDialog('History information', 'Covered before deferred redraw.'))
                await pilot.pause()
                callback(*args)
            else:
                await complete_under_modal(app, pilot, estimate, selected)
            assert owner._pending_cost_refresh is selected
            redraw = Mock()
            monkeypatch.setattr(app, 'refresh_rows', redraw)

            await app.show('tests', 'Another picker', rows=owner.rows)
            replacement = app.screen
            await pilot.pause()
            assert replacement is not owner and app.selection is selected
            redraw.assert_not_called()
            assert owner._pending_cost_refresh is selected

            app.pop_screen()
            await pilot.pause()
            app.pop_screen()
            await pilot.pause()
            assert app.screen is owner and owner._pending_cost_refresh is None
            callback, args = callbacks.pop()
            assert args == (owner, selected)
            callback(*args)
            redraw.assert_called_once_with()
    asyncio.run(scenario())


def test_current_picker_can_adopt_worker_result_before_redraw_is_owned(cached_picker, monkeypatch):
    app, estimate = cached_picker
    entered, finish = Event(), Event()
    replacement_estimate = Snapshot(9)
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            previous, selected = app.screen, app.selection
            def synchronize():
                entered.set()
                assert finish.wait(5)
                return replacement_estimate
            estimate.synchronize = synchronize
            task = asyncio.create_task(app.synchronize_cost_history(estimate, selected))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                await app.show('tests', 'Current compatible picker', rows=previous.rows)
                current = app.screen
                await pilot.pause()
                callbacks = defer_redraws(app, current, monkeypatch)
                finish.set()
                await task
                assert app.cost_estimator is replacement_estimate
                callback, args = callbacks.pop()
                assert args == (current, selected) and current is not previous
                redraw = Mock()
                monkeypatch.setattr(app, 'refresh_rows', redraw)
                callback(*args)
                redraw.assert_called_once_with()
            finally:
                finish.set()
                await task
    asyncio.run(scenario())


def test_original_resume_rejects_pending_redraw_after_selection_changes(cached_picker, monkeypatch):
    app, estimate = cached_picker
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            picker = app.screen
            callbacks = defer_redraws(app, picker, monkeypatch)
            await complete_under_modal(app, pilot, estimate, app.selection)
            app.selection = SelectionModel(identities(3))
            app.pop_screen()
            await pilot.pause()
            assert app.screen is picker and picker._pending_cost_refresh is None
            assert callbacks == []
    asyncio.run(scenario())


def test_resumed_redraw_cannot_touch_picker_after_teardown(cached_picker, monkeypatch):
    app, estimate = cached_picker
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            picker = app.screen
            callbacks = defer_redraws(app, picker, monkeypatch)
            await complete_under_modal(app, pilot, estimate, app.selection)
            app.pop_screen()
            await pilot.pause()
            callback, args = callbacks.pop()
            redraw = Mock()
            monkeypatch.setattr(app, 'refresh_rows', redraw)
        callback(*args)
        redraw.assert_not_called()
    asyncio.run(scenario())


def test_worker_finishing_beneath_nested_modal_binds_nearest_current_picker(cached_picker, monkeypatch):
    app, estimate = cached_picker
    entered, finish = Event(), Event()
    def synchronize():
        entered.set()
        assert finish.wait(5)
        return estimate
    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            original, selected = app.screen, app.selection
            estimate.synchronize = synchronize
            task = asyncio.create_task(app.synchronize_cost_history(estimate, selected))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                app.push_screen(SettingHelpDialog('First overlay', 'Original picker remains below.'))
                await pilot.pause()
                await app.show('tests', 'Current compatible picker', rows=original.rows)
                current = app.screen
                callbacks = defer_redraws(app, current, monkeypatch)
                app.push_screen(SettingHelpDialog('Second overlay', 'Current picker remains below.'))
                await pilot.pause()
                finish.set()
                await task
                assert current._pending_cost_refresh is selected
                app.pop_screen()
                await pilot.pause()
                callback, args = callbacks.pop()
                assert args == (current, selected)
                redraw = Mock()
                monkeypatch.setattr(app, 'refresh_rows', redraw)
                callback(*args)
                redraw.assert_called_once_with()
            finally:
                finish.set()
                await task
    asyncio.run(scenario())
