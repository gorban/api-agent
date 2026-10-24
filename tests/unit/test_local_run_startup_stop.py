"""Stopping a real controller retains a cancellation-suppressing late startup."""
import asyncio
from contextlib import asynccontextmanager

from api_agent.tui.local_run import LocalRunScreen
from api_agent.runtime import local_run as runtime
from api_agent.providers.progress import component_timeout
from test_local_run import local
from test_local_run_screen import Harness


def test_stop_during_suppressed_startup_keeps_loop_and_automatically_retries(local, monkeypatch):
    local.config.values['RUN_LOG_SOURCE'] = 'agent'
    local.owner.CLEANUP_ATTEMPT_SECONDS = .02
    local.owner.CLEANUP_RETRY_SECONDS = .01
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
        phases = []
        def owner_factory(*args, on_event, **kwargs):
            def observed(event):
                if event.kind == 'status':
                    phases.append(event.snapshot.phase)
                on_event(event)
            local.owner.on_event = observed
            return local.owner
        screen = LocalRunScreen(None, lambda: local.config, owner_factory=owner_factory)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await entered.wait()
            stopping = asyncio.create_task(screen.close_owned())
            try:
                finished, _ = await asyncio.wait({stopping}, timeout=.05)
                assert stopping not in finished
                assert 'cleanup-pending' in phases
                assert local.owner.pending_cleanup is not None
                assert not screen.disposed
                assert not app.menu_shown
                assert 'browser-stop' not in local.calls
            finally:
                release.set()
                await screen.run_task
                await stopping
            assert stopping.result() is True
            assert screen.disposed
            assert screen.pending_cleanup is None
            assert local.owner.pending_cleanup is None
            assert local.calls == ['operation-start', 'late-browser-allocation', 'browser-stop', 'operation-stop']
    asyncio.run(exercise())
