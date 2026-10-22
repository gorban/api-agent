"""A pending local operation keeps the remaining application shutdown duties."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.control import CleanupPendingError
from test_tui import fixture


def test_external_unmount_receipt_keeps_reporting_until_local_retry_finishes(tmp_path):
    async def exercise():
        app = fixture(tmp_path)
        actions = []
        attempts = 0
        async def release_local():
            nonlocal attempts
            attempts += 1
            actions.append('local')
            if attempts == 1:
                raise RuntimeError('retry local')
        pending = CleanupPendingError(('local',), release_local)
        app.local_run_screen = SimpleNamespace(close_owned=AsyncMock(return_value=False),
            owner=SimpleNamespace(pending_cleanup=pending))
        app.reporting_sessions = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: actions.append('reporting')))
        await app.on_unmount()
        receipt = app.pending_shutdown_cleanup
        assert receipt is not pending
        with pytest.raises(RuntimeError, match='retry local'):
            await receipt.retry()
        assert actions == ['local']
        assert app.pending_shutdown_cleanup is receipt
        await receipt.retry()
        assert actions == ['local', 'local', 'reporting']
        assert app.local_run_screen is None
        assert app.pending_shutdown_cleanup is None
    asyncio.run(exercise())


def test_confirm_quit_keeps_app_open_when_local_disposal_is_pending(tmp_path):
    async def exercise():
        app = fixture(tmp_path)
        app.local_run_screen = SimpleNamespace(close_owned=AsyncMock(return_value=False))
        app.exit = Mock()
        await app.confirm_quit()
        app.exit.assert_not_called()
    from unittest.mock import Mock
    asyncio.run(exercise())


def test_shutdown_retry_retries_local_without_a_separate_receipt(tmp_path):
    async def exercise():
        app = fixture(tmp_path)
        attempts = 0
        async def close_local():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return False
            app.local_run_screen = None
            return True
        local_screen = SimpleNamespace(close_owned=close_local,
            owner=SimpleNamespace(pending_cleanup=None))
        app.local_run_screen = local_screen
        app.reporting_sessions = SimpleNamespace(aclose=AsyncMock())
        receipt = app.shutdown_cleanup_receipt()
        with pytest.raises(CleanupPendingError):
            await receipt.retry()
        app.reporting_sessions.aclose.assert_not_awaited()
        await receipt.retry()
        assert app.local_run_screen is None
        app.reporting_sessions.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_shutdown_retry_waits_for_active_test_then_keeps_pending_inspection(tmp_path):
    async def exercise():
        from unittest.mock import Mock
        app = fixture(tmp_path)
        app.test_run = SimpleNamespace(active=True, stop_and_wait=AsyncMock())
        app.end_inspection = AsyncMock(return_value=False)
        pending = CleanupPendingError(('inspection',), AsyncMock())
        app.inspection_cleanup_receipt = Mock(return_value=pending)
        app.reporting_sessions = SimpleNamespace(aclose=AsyncMock())
        receipt = app.shutdown_cleanup_receipt()
        with pytest.raises(CleanupPendingError) as caught:
            await receipt.retry()
        assert caught.value is pending
        app.test_run.stop_and_wait.assert_awaited_once()
        app.reporting_sessions.aclose.assert_not_awaited()
    asyncio.run(exercise())


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_child_unmount_exports_local_and_reporting_retry(tmp_path, monkeypatch, cancelled):
    import sys
    from api_agent.runtime.local_contracts import LocalSnapshot
    async def exercise():
        async def retry_owner(): pass
        pending = CleanupPendingError(('local',), retry_owner)
        failure = asyncio.CancelledError() if cancelled else pending
        class Owner:
            pending_cleanup = pending
            reject = True
            def __init__(self, *args, **kwargs): self.stop = asyncio.Event()
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await self.stop.wait()
                return 0
            def request_stop(self): self.stop.set()
            async def aclose(self):
                if self.reject:
                    raise failure
                self.pending_cleanup = None
            def diagnostic(self, error): return 'cleanup pending'
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        app.reporting_sessions = SimpleNamespace(aclose=AsyncMock())
        with pytest.raises(type(failure)) as caught:
            async with app.run_test() as pilot:
                await pilot.pause()
        receipt = app.pending_shutdown_cleanup
        assert caught.value is (failure if cancelled else receipt)
        if cancelled:
            assert caught.value.cleanup_pending is receipt
        assert app.local_run_screen is not None
        app.reporting_sessions.aclose.assert_not_awaited()
        app.local_run_screen.owner.reject = False
        await receipt.retry()
        assert app.pending_shutdown_cleanup is None
        assert app.local_run_screen is None
        app.reporting_sessions.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_stale_ownerless_screen_receipt_can_be_retried(tmp_path):
    async def exercise():
        app = fixture(tmp_path)
        local_screen = SimpleNamespace(owner=None, pending_cleanup=None, close_owned=AsyncMock(return_value=True))
        app.local_run_screen = local_screen
        app.reporting_sessions = SimpleNamespace(aclose=AsyncMock())
        receipt = app.shutdown_cleanup_receipt()
        await receipt.retry()
        assert app.local_run_screen is None
        await receipt.retry()
        assert app.pending_shutdown_cleanup is None
    asyncio.run(exercise())
