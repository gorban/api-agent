"""Stopping a real controller retains a cancellation-suppressing late startup."""
import asyncio
from contextlib import asynccontextmanager

from api_agent.tui.local_run import LocalRunScreen
from api_agent.runtime import local_run as runtime
from api_agent.providers.progress import component_timeout
from test_local_run import local
from test_local_run_screen import Harness


def test_stop_during_suppressed_startup_returns_pending_and_retry_disposes(local, monkeypatch):
    local.config.values['RUN_LOG_SOURCE'] = 'agent'
    @asynccontextmanager
    async def short_startup_settlement(name, timeout):
        async with component_timeout(name, .02 if name == 'Local startup settlement' else timeout) as budget:
            yield budget
    monkeypatch.setattr(runtime, 'component_timeout', short_startup_settlement)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def late_browser_start():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            local.calls.append('late-browser-allocation')
        local.browser.start = late_browser_start
        def owner_factory(*args, on_event, **kwargs):
            local.owner.on_event = on_event
            return local.owner
        screen = LocalRunScreen(None, lambda: local.config, owner_factory=owner_factory)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await entered.wait()
            stopping = asyncio.create_task(screen.close_owned())
            try:
                finished, _ = await asyncio.wait({stopping}, timeout=.3)
                assert stopping in finished, 'Stop waited for startup before running bounded cleanup'
                assert stopping.result() is False
                assert screen.pending_cleanup is not None
                assert local.owner.pending_cleanup is not None
                assert not screen.disposed
                assert not app.menu_shown
                assert 'browser-stop' not in local.calls
            finally:
                release.set()
                await screen.run_task
                await stopping
            await screen.pending_cleanup.retry()
            assert screen.disposed
            assert screen.pending_cleanup is None
            assert local.owner.pending_cleanup is None
            assert local.calls == ['operation-start', 'late-browser-allocation', 'browser-stop', 'operation-stop']
    asyncio.run(exercise())
