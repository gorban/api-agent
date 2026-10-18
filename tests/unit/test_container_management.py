"""Mounted container management and log pages use synthetic readers only."""
import asyncio
from types import SimpleNamespace
import threading

import pytest
from textual.widgets import Button, DataTable, Static, TextArea
from textual.document._document import Selection

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.containers import Container
from api_agent.tui.logs import LogScreen
from test_tui import fixture



def app_for(tmp_path, items, *, mode='cleanup-containers', loader=lambda **kwargs:'sanitized log output'):
    app = fixture(tmp_path, mode)
    app.container_runtime = SimpleNamespace(inventory=lambda: tuple(items), logs=lambda identifier, *, cancelled, scrubber:loader(cancelled=cancelled))
    return app


@pytest.mark.parametrize('items', [[], [Container('a','stopped','image','exited',False)]])
def test_zero_running_menu_remains_accessible_and_stopped_rows_are_not_stop_selected(tmp_path, items):
    async def scenario():
        app = app_for(tmp_path, items, mode=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            button = app.screen.query_one('#mode-cleanup-containers', Button)
            assert not button.disabled and str(button.label)=='Container management'
            assert '0 running' in str(app.screen.query_one('#body',Static).content)
            await app.choose('mode-cleanup-containers')
            await pilot.pause()
            assert app.screen.kind=='containers'
            assert not app.container_marked
            if items:
                await pilot.press('enter')
                assert not app.container_marked
            else:
                with pytest.raises(ValueError,match='Highlight'): app.request_container_logs()
    asyncio.run(scenario())


def test_running_selection_logs_scrolling_and_sanitized_copy(tmp_path, monkeypatch):
    copied=[]
    async def copy(app,text): copied.append(text);return True
    monkeypatch.setattr('api_agent.tui.logs.copy_text',copy)
    async def scenario():
        items=[Container('a','running','image','running'),Container('b','stopped','image','exited',False)]
        app=app_for(tmp_path,items,loader=lambda **kwargs:'first line\n'+'visible line\n'*100)
        async with app.run_test(size=(100,35)) as pilot:
            await pilot.pause()
            app.toggle_managed_container('container-all')
            assert app.container_marked=={'a'}
            app.toggle_managed_container('a');assert not app.container_marked
            app.toggle_managed_container('a');assert app.container_marked=={'a'}
            app.toggle_managed_container('container-all');assert not app.container_marked
            with pytest.raises(ValueError,match='Highlight'):app.request_container_logs()
            table=app.screen.query_one(DataTable);table.move_cursor(row=2)
            app.request_container_logs();await pilot.pause()
            screen=app.screen;assert isinstance(screen,LogScreen)
            editor=screen.query_one(TextArea)
            assert editor.read_only and editor.text.startswith('first line')
            editor.selection=Selection((0,0),(0,5))
            await pilot.press('ctrl+c');await pilot.pause()
            assert copied==['first']
            await pilot.press('pagedown');await pilot.pause()
            assert editor.scroll_y>0
            await pilot.click('#log-copy');await pilot.pause()
            assert copied[-1]==editor.text
            await pilot.press('escape');await pilot.pause()
            assert app.screen.kind=='containers' and screen.reader.done()
            await app.close_container_logs();assert app.container_log_screen is None
            await app.close_container_logs()
    asyncio.run(scenario())


def test_closing_and_unmounting_settle_reader_before_page_disappears(tmp_path):
    async def scenario():
        entered,stop,cancel_seen,permit=(threading.Event() for _ in range(4))
        def loader(*,cancelled):
            entered.set()
            while not cancelled():stop.wait(.01)
            cancel_seen.set();assert permit.wait(10)
            stop.set()
            return 'late output'
        app=app_for(tmp_path,[Container('a','stopped','image','exited',False)],loader=loader)
        async with app.run_test() as pilot:
            await pilot.pause();app.request_container_logs();await pilot.pause()
            screen=app.screen
            assert await asyncio.to_thread(entered.wait,5)
            closing=asyncio.create_task(screen.action_close())
            try:
                assert await asyncio.to_thread(cancel_seen.wait,5)
                await asyncio.sleep(0)
                assert not closing.done() and not screen.reader.done() and app.screen is screen
            finally:
                permit.set();await closing;await screen.reader
            await pilot.pause()
            assert stop.is_set() and screen.reader.done() and app.screen.kind=='containers'
            assert app.screen.query_one('#body',Static).content!='late output'
    asyncio.run(scenario())


def test_late_inventory_does_not_replace_covered_screen(tmp_path):
    async def scenario():
        entered,release=threading.Event(),threading.Event()
        app=app_for(tmp_path,[])
        async with app.run_test() as pilot:
            await pilot.pause()
            def inventory():
                entered.set();assert release.wait(10);return ()
            app.container_runtime.inventory=inventory
            task=asyncio.create_task(app._refresh_managed_containers())
            await asyncio.to_thread(entered.wait,5)
            popup=LogScreen('Other log page',lambda **kwargs:'other text')
            app.push_screen(popup);await pilot.pause()
            try:
                release.set();await task;await pilot.pause()
                assert app.screen is popup and popup.query_one(TextArea).text=='other text'
                await app._refresh_managed_containers()
                assert app.screen is popup
            finally:
                release.set();await task;await popup.action_close()
    asyncio.run(scenario())


def test_inventory_failure_and_pending_reader_cleanup_are_recoverable(tmp_path):
    async def scenario():
        attempts=[];allow=False
        async def retry():
            attempts.append('retry')
            if not allow:raise receipt
        receipt=CleanupPendingError((),retry)
        def loader(**kwargs):raise receipt
        app=app_for(tmp_path,[Container('a','stopped','image','exited',False)],loader=loader)
        async with app.run_test() as pilot:
            await pilot.pause();app.request_container_logs();await pilot.pause()
            screen=app.screen;assert screen.pending_cleanup is receipt
            await screen.action_close();await pilot.pause()
            assert app.screen is screen and screen.pending_cleanup is receipt
            assert 'pending' in str(screen.query_one('#log-error',Static).content)
            with pytest.raises(CleanupPendingError):await app.close_container_logs()
            assert app.container_log_screen is screen
            allow=True
            await app.close_container_logs();assert app.container_log_screen is None
            await screen.action_close();await pilot.pause()
            def fail():raise RuntimeError('never display this raw error')
            app.container_runtime.inventory=fail
            await app._refresh_managed_containers();await pilot.pause()
            assert app.container_summary=='Container inventory unavailable.'
            assert app.screen.kind=='containers'
            app.update_managed_container_menu()  # A non-menu screen is untouched.
    asyncio.run(scenario())


def test_log_failure_copy_failure_and_empty_selection(tmp_path,monkeypatch):
    from api_agent.tui.clipboard import ClipboardError
    async def fail_copy(app,text):raise ClipboardError('private diagnostic')
    monkeypatch.setattr('api_agent.tui.logs.copy_text',fail_copy)
    async def scenario():
        def fail(**kwargs):raise RuntimeError('raw secret diagnostic')
        app=app_for(tmp_path,[Container('a','stopped','image','exited',False)],loader=fail)
        async with app.run_test() as pilot:
            await pilot.pause();app.request_container_logs();await pilot.pause()
            screen=app.screen
            assert screen.query_one(TextArea).text=='Container logs unavailable or cancelled.'
            await screen.action_copy_selected()  # No selection performs no clipboard write.
            await pilot.click('#log-copy');await pilot.pause()
            assert str(screen.query_one('#log-error',Static).content)=='Could not copy log text.'
            assert await pilot.click('#log-close');await pilot.pause()

            assert app.screen.kind=='containers'
    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_close_coalesces_disposal_and_preserves_cancelled_observer(fails):
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[];allow=False
        screen=LogScreen('Detached page',lambda **kwargs:'unused')
        async def retry():
            calls.append('close');entered.set();await release.wait()
            if fails and not allow:raise receipt
        receipt=CleanupPendingError((),retry);screen.pending_cleanup=receipt
        first=asyncio.create_task(screen.aclose())
        await entered.wait()
        second=asyncio.create_task(screen.aclose())
        await asyncio.sleep(0)
        first.cancel('stop observing')
        await asyncio.sleep(0)
        assert calls==['close'] and not first.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:await first
        assert caught.value.args==('stop observing',)
        if fails:
            with pytest.raises(CleanupPendingError):await second
            assert screen.pending_cleanup is receipt
            allow=True
        else:await second
        await screen.aclose();await screen.aclose()
        assert screen.pending_cleanup is None and calls==(['close','close'] if fails else ['close'])
    asyncio.run(scenario())


def test_replacement_cleanup_and_changed_app_owner_are_retained(tmp_path):
    async def scenario():
        screen=LogScreen('Detached page',lambda **kwargs:'unused');closed=[]
        async def replacement_retry():closed.append(True)
        replacement=CleanupPendingError((),replacement_retry)
        async def original_retry():screen.pending_cleanup=replacement
        screen.pending_cleanup=CleanupPendingError((),original_retry)
        with pytest.raises(CleanupPendingError) as pending:await screen.aclose()
        assert pending.value is replacement and not closed
        await screen.aclose();assert closed==[True]
        app=app_for(tmp_path,[])
        entered,release=asyncio.Event(),asyncio.Event()
        async def wait_close():entered.set();await release.wait()
        old=SimpleNamespace(aclose=wait_close)
        new=LogScreen('Next owner',lambda **kwargs:'unused')
        app.container_log_screen=old
        task=asyncio.create_task(app.close_container_logs());await entered.wait()
        app.container_log_screen=new;release.set();await task
        assert app.container_log_screen is new
        await app.close_container_logs();assert app.container_log_screen is None
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['q', 'ctrl+q'])
def test_quit_from_log_page_uses_normal_confirmation_and_settles_reader(tmp_path,key):
    from textual.widgets import Input
    async def scenario():
        entered,stopped=threading.Event(),threading.Event()
        def reader(*,cancelled):
            entered.set()
            while not cancelled():stopped.wait(.01)
            stopped.set();return 'late'
        app=app_for(tmp_path,[Container('a','stopped','image','exited',False)],loader=reader)
        async with app.run_test() as pilot:
            await pilot.pause();app.request_container_logs();await pilot.pause()
            screen=app.screen
            assert await asyncio.to_thread(entered.wait,5)
            await pilot.press(key);await pilot.pause()
            assert app.screen.__class__.__name__=='QuitDialog'
            app.screen.query_one(Input).value='y'
            await pilot.press('enter');await pilot.pause()
            assert stopped.is_set() and screen.reader.done()
            assert app.container_log_screen is None
    asyncio.run(scenario())


def test_interrupted_cleanup_retains_pending_reader_for_another_close(tmp_path):
    async def scenario():
        allow=False
        async def retry():
            if not allow:raise asyncio.CancelledError('native cleanup interrupted')
        receipt=CleanupPendingError((),retry)
        failure=RuntimeError('raw diagnostic');failure.cleanup_pending=receipt
        def loader(**kwargs):raise failure
        app=app_for(tmp_path,[Container('a','stopped','image','exited',False)],loader=loader)
        async with app.run_test() as pilot:
            await pilot.pause();app.request_container_logs();await pilot.pause()
            screen=app.screen
            with pytest.raises(asyncio.CancelledError):await screen.action_close()
            assert app.container_log_screen is screen and screen.pending_cleanup is receipt
            allow=True
            await screen.action_close();await pilot.pause()
            assert screen.pending_cleanup is None and screen.reader.done()
    asyncio.run(scenario())
