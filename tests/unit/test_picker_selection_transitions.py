"""Collection delivery and drag selection preserve the active user interaction."""
import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import DataTable, Input

from api_agent.tui.app import QuitDialog
from test_test_table import identities
from test_tui import fixture


@pytest.mark.parametrize('refresh', [False, True])
@pytest.mark.parametrize('fails', [False, True])
@pytest.mark.parametrize('accept', [False, True])
def test_collection_completion_preserves_quit_until_answer(tmp_path, monkeypatch, refresh, fails, accept):
    async def scenario():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def collect(*args):
            entered.set()
            assert release.wait(5)
            finished.set()
            if fails:
                raise ValueError('Synthetic collection failure')
            return identities(2)

        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.collector = collect
            if refresh:
                app.run_worker(app.back_to_selection(), group='refresh')
            else:
                await app.collect_tests()
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.pause()
            owner = app.screen
            assert owner.kind == 'collecting'
            # No inference or execution is allowed while Quit is pending.
            choose = AsyncMock()
            monkeypatch.setattr(app, 'choose', choose)
            app.auto_start_pending = True
            await pilot.press('q')
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, QuitDialog)
            answer = dialog.query_one(Input)
            answer.value = 'y' if accept else 'n'
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            await pilot.pause(.1)
            assert app.screen is dialog and app.focused is answer
            assert answer.value == ('y' if accept else 'n')
            assert owner in app.screen_stack
            choose.assert_not_awaited()
            await pilot.press('enter')
            await pilot.pause()
            if accept:
                assert app.collection_cancel.is_set()
                assert app.selection.selected_tests == identities(2)
                choose.assert_not_awaited()
            else:
                await app.workers.wait_for_complete()
                assert app.screen.kind == ('error' if fails else 'tests')
                if not fails and not refresh:
                    choose.assert_awaited_once_with('continue')
                else:
                    choose.assert_not_awaited()
                assert dialog not in app.screen_stack
    asyncio.run(scenario())


def test_pending_collect_only_waits_for_quit_cancellation(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def collect(*args):
            entered.set()
            assert release.wait(5)
            return identities(2)
        app = fixture(tmp_path, 'integration', collector=collect,
                      fields={'COLLECT_ONLY': {'type': 'bool', 'default': True, 'acknowledge_default': False}})
        async with app.run_test(size=(120, 40)) as pilot:
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.press('q')
            dialog = app.screen
            release.set()
            await pilot.pause(.1)
            assert app.screen is dialog and getattr(app, 'collected_output', None) is None
            await pilot.press('escape')
            await pilot.pause()
            assert app.collected_output == '\n'.join(item.id for item in identities(2))
    asyncio.run(scenario())


@pytest.mark.parametrize('obsolescence', ['cancelled', 'replaced', 'menu'])
def test_collection_delivery_rejects_obsolete_visit(tmp_path, obsolescence):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show('collecting', 'Collecting tests')
            await pilot.pause()
            owner, cancel = app.screen, app.collection_cancel
            if obsolescence == 'cancelled':
                cancel.set()
            elif obsolescence == 'replaced':
                app.collection_cancel = threading.Event()
            else:
                app.show_menu()
            await pilot.pause()
            assert not await app.collection_delivery_allowed(owner, cancel)
    asyncio.run(scenario())


def test_mouse_drag_only_refreshes_bindings_on_selection_availability(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(4))
        copied = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            refresh = Mock(wraps=screen.refresh_bindings)
            monkeypatch.setattr(screen, 'refresh_bindings', refresh)
            await pilot.mouse_down('#body', offset=(0, 0))
            await pilot.hover('#body', offset=(8, 0))
            await pilot.pause()
            first = refresh.call_count
            for end in (16, 24, 32, 40):
                await pilot.hover('#body', offset=(end, 0))
            await pilot.mouse_up('#body', offset=(40, 0))
            await pilot.pause()
            assert first > 0 and refresh.call_count == first
            expected = screen.get_selected_text()
            assert expected == str(screen.body)[:41]
            table = screen.query_one(DataTable)
            rows = []
            app.watch(table, 'cursor_coordinate', lambda point: rows.append(point.row), init=False)
            table.focus()
            await pilot.press('down', 'down', 'up', 'up')
            assert rows == [1, 2, 1, 0]
            await pilot.press('ctrl+c')
            copied.assert_awaited_once_with(app, expected)
            assert screen.get_selected_text() is None
            assert not screen._selected_text_available
    asyncio.run(scenario())


@pytest.mark.parametrize('refresh', [False, True])
@pytest.mark.parametrize('fails', [False, True])
@pytest.mark.parametrize('accept', [False, True])
def test_quit_before_queued_collection_starts_blocks_creation_and_delivery(tmp_path, monkeypatch, refresh, fails, accept):
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            collector = Mock(side_effect=ValueError('Synthetic collection failure') if fails else None,
                             return_value=identities(2))
            app.collector = collector
            choose = AsyncMock()
            monkeypatch.setattr(app, 'choose', choose)
            app.auto_start_pending = True
            owner = app.screen
            if refresh:
                app.run_worker(app.back_to_selection(), group='refresh')
            else:
                await app.collect_tests()
            # Open Quit in this turn, before the queued worker gets to run.
            await owner.action_quit_request()
            dialog = app.screen
            await pilot.pause(.1)
            assert isinstance(dialog, QuitDialog) and app.screen is dialog
            assert app.screen_stack[-2] is owner
            collector.assert_not_called()
            choose.assert_not_awaited()
            dialog.query_one(Input).value = 'y' if accept else 'n'
            await pilot.press('enter')
            await pilot.pause()
            if accept:
                collector.assert_not_called()
                choose.assert_not_awaited()
                assert app.collection_cancel.is_set()
            else:
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                collector.assert_called_once()
                assert app.screen.kind == ('error' if fails else 'tests')
                if not fails and not refresh:
                    choose.assert_awaited_once_with('continue')
                else:
                    choose.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('accept', [False, True])
def test_queued_collect_only_does_not_exit_over_quit(tmp_path, accept):
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2), fields={
            'COLLECT_ONLY': {'type': 'bool', 'default': False, 'acknowledge_default': False}})
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.session.set_value('COLLECT_ONLY', 'True')
            await app.collect_tests()
            await app.screen.action_quit_request()
            dialog = app.screen
            await pilot.pause(.1)
            assert app.screen is dialog and getattr(app, 'collected_output', None) is None
            dialog.query_one(Input).value = 'y' if accept else 'n'
            await pilot.press('enter')
            await pilot.pause()
            if accept:
                assert getattr(app, 'collected_output', None) is None
            else:
                assert app.collected_output == '\n'.join(item.id for item in identities(2))
    asyncio.run(scenario())


@pytest.mark.parametrize('accept', [False, True])
def test_refresh_waits_for_quit_opened_during_inspection_cleanup(tmp_path, monkeypatch, accept):
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            entered, release = asyncio.Event(), asyncio.Event()
            async def cleanup(*, force=False):
                entered.set()
                await release.wait()
                return True
            monkeypatch.setattr(app, 'end_inspection', cleanup)
            app.run_worker(app.back_to_selection(), group='refresh')
            await asyncio.wait_for(entered.wait(), 3)
            await app.screen.action_quit_request()
            dialog = app.screen
            release.set()
            await pilot.pause(.1)
            assert app.screen is dialog
            dialog.query_one(Input).value = 'y' if accept else 'n'
            await pilot.press('enter')
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            if accept:
                assert app.collection_cancel.is_set()
            else:
                assert app.screen.kind == 'tests'
    asyncio.run(scenario())
