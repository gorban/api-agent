"""Stopping a real controller retains a cancellation-suppressing late startup."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api_agent.tui.local_run import LocalRunScreen
from api_agent.runtime import local_run as runtime
from api_agent.providers.progress import component_timeout
from api_agent.providers.control import CleanupPendingError
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


@pytest.mark.parametrize('state', ['unfinished', 'cancelled', 'failed', 'replaced', 'foreign', 'retained'])
def test_delayed_receipt_cannot_acknowledge_unproven_disposal(local, monkeypatch, state):
    async def exercise():
        closing = asyncio.get_running_loop().create_future()
        local.owner._closing = closing
        observed = []
        failed = RuntimeError('synthetic disposal failure')
        foreign = CleanupPendingError((local.owner,), local.owner.aclose)

        @asynccontextmanager
        async def settle_at_receipt_delivery(name, timeout):
            try:
                yield SimpleNamespace(remaining=0)
            except CleanupPendingError as error:
                observed.append(error)
                if state == 'cancelled':
                    closing.cancel()
                elif state == 'failed':
                    closing.set_exception(failed)
                elif state != 'unfinished':
                    closing.set_result(None)
                if state == 'replaced':
                    replacement = asyncio.get_running_loop().create_future()
                    replacement.set_result(None)
                    local.owner._closing = replacement
                if state != 'retained':
                    local.owner.pending_cleanup = None
                if state == 'foreign':
                    raise foreign
                raise

        monkeypatch.setattr(runtime, 'report_timeout', settle_at_receipt_delivery)
        try:
            expected = RuntimeError if state == 'failed' else CleanupPendingError
            with pytest.raises(expected) as caught:
                await local.owner.aclose()
            produced, = observed
            assert caught.value is (failed if state == 'failed' else foreign if state == 'foreign' else produced)
            if state == 'retained':
                assert local.owner.pending_cleanup is produced
        finally:
            if not closing.done():
                closing.cancel()
            elif not closing.cancelled():
                closing.exception()

    asyncio.run(exercise())


def test_delayed_successful_receipt_preserves_waiter_cancellation(local, monkeypatch):
    async def exercise():
        closing = asyncio.get_running_loop().create_future()
        local.owner._closing = closing

        class CancelAtWait:
            checks = 0

            @property
            def remaining(self):
                self.checks += 1
                if self.checks == 2:
                    asyncio.current_task().cancel()
                return 1 if self.checks <= 2 else 0

        @asynccontextmanager
        async def complete_at_receipt_delivery(name, timeout):
            try:
                yield CancelAtWait()
            except CleanupPendingError:
                closing.set_result(None)
                local.owner.pending_cleanup = None
                raise

        monkeypatch.setattr(runtime, 'report_timeout', complete_at_receipt_delivery)
        with pytest.raises(asyncio.CancelledError):
            await local.owner.aclose()
        assert closing.done() and closing.result() is None
        assert local.owner.pending_cleanup is None

    asyncio.run(exercise())


def test_delayed_pending_receipt_acknowledges_same_successful_close(local, monkeypatch):
    local.config.values['RUN_LOG_SOURCE'] = 'agent'
    local.owner.CLEANUP_ATTEMPT_SECONDS = .01
    original_timeout = runtime.report_timeout

    async def exercise():
        ready, cleanup_entered = asyncio.Event(), asyncio.Event()
        release_cleanup, receipt_delayed = asyncio.Event(), asyncio.Event()
        release_receipt = asyncio.Event()
        receipts = []

        async def held_browser_close():
            cleanup_entered.set()
            await release_cleanup.wait()
            local.calls.append('browser-stop')

        local.browser.aclose = held_browser_close

        def owner_factory(*args, on_event, **kwargs):
            def observed(event):
                on_event(event)
                if event.kind == 'status' and event.snapshot.phase == 'ready':
                    ready.set()
            local.owner.on_event = observed
            return local.owner

        screen = LocalRunScreen(None, lambda: local.config, owner_factory=owner_factory)

        @asynccontextmanager
        async def delay_pending_receipt(name, timeout):
            try:
                async with original_timeout(name, timeout) as budget:
                    yield budget
            except CleanupPendingError as error:
                if name == 'Local cleanup attempt' and asyncio.current_task() is screen._close_task:
                    receipts.append(error)
                    receipt_delayed.set()
                    await release_receipt.wait()
                raise

        monkeypatch.setattr(runtime, 'report_timeout', delay_pending_receipt)
        app = Harness(screen)
        async with app.run_test():
            async with asyncio.timeout(2):
                await ready.wait()
            stopping = asyncio.create_task(screen.close_owned())
            try:
                async with asyncio.timeout(2):
                    await cleanup_entered.wait()
                    await receipt_delayed.wait()
                closing = local.owner._closing
                receipt, = receipts
                assert local.owner.pending_cleanup is not None
                assert local.owner.pending_cleanup.owners == (local.owner,)
                assert receipt.owners == (local.owner,)
                assert not closing.done() and not screen.disposed

                completing = asyncio.create_task(local.owner.finish_cleanup())
                release_cleanup.set()
                async with asyncio.timeout(2):
                    await completing
                    await screen.run_task
                assert local.owner._closing is closing
                assert closing.done() and closing.result() is None
                assert local.owner.pending_cleanup is None
                assert not stopping.done()

                release_receipt.set()
                async with asyncio.timeout(2):
                    assert await stopping is True
                assert screen.disposed and screen.pending_cleanup is None
                assert local.calls == ['operation-start', 'browser-start', 'browser-stop', 'operation-stop']
            finally:
                release_cleanup.set()
                release_receipt.set()
                await stopping

    asyncio.run(exercise())
