"""TUI retries retain the final suite-release duty after resource disposal."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.operation import retry_owned_cleanup
from api_agent.testing import launched, suite_lock as locks
from test_recorded_integration import fixture as recording_fixture, fake_runner, Storage
from test_stored_cleanup_retry import failed_prerequisites
from test_suite_cleanup_lease import assert_competing_suite_allowed, assert_competing_suite_blocked
from test_tui import fixture as tui_fixture


async def assert_failed_consumer(app, consumer, fault):
    """Ordinary faults remain retryable; interruptions keep their classification."""
    if consumer == 'shutdown':
        receipt = app.inspection_cleanup_receipt()
        expected = CleanupPendingError if fault is RuntimeError else fault
        with pytest.raises(expected):
            await receipt.retry()
    elif fault is RuntimeError:
        assert not await app.end_inspection()
    else:
        with pytest.raises(fault):
            await app.end_inspection()


async def recover_consumer(app, consumer):
    if consumer == 'shutdown':
        await app.inspection_cleanup_receipt().retry()
    else:
        assert await app.end_inspection()


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize('first_consumer', ['tui', 'shutdown'])
@pytest.mark.parametrize('recovery_consumer', ['tui', 'shutdown'])
def test_final_native_release_failure_keeps_a_recoverable_tui_receipt(
        failed_prerequisites, tmp_path, fault, first_consumer, recovery_consumer):
    owner, original, manager = failed_prerequisites
    manager.release.side_effect = None
    app = tui_fixture(tmp_path)
    app.recording_cleanup = owner
    app.integration_exit_status = 0
    lease = original.retry.__self__.lease

    async def scenario():
        with patch.object(locks, 'start_new_thread', side_effect=fault('native startup failed')):
            await assert_failed_consumer(app, first_consumer, fault)
            assert owner.pending_cleanup is original
            assert owner.owned_manager is None and owner.lease is None
            assert app.recording_cleanup is owner and app.inspection_cleanup_active()
            assert lease.fd is not None
            manager.release.assert_called_with(retain=True)
            assert manager.release.call_count == 2
            assert_competing_suite_blocked()
            # A newly exported receipt must not skip the unacknowledged release.
            await assert_failed_consumer(app, 'shutdown', fault)
            assert app.recording_cleanup is owner and owner.pending_cleanup is original
            assert_competing_suite_blocked()
        await recover_consumer(app, recovery_consumer)
        assert app.recording_cleanup is None and not app.inspection_cleanup_active()
        assert owner.pending_cleanup is None and not owner.prepared
        assert app.operation_status == 0 and lease.fd is None
        assert_competing_suite_allowed()
        await original.retry()
        assert manager.release.call_count == 2
        assert_competing_suite_allowed()

    asyncio.run(scenario())
    # Injected prerequisites are reusable after acknowledged cleanup, including
    # the cached disposal path that does not rerun its completion callback.
    context, test, config = recording_fixture(tmp_path)
    second = launched.run_launched_integration(context, (test,), config, accepted=True,
        prerequisites=owner, runner=fake_runner(), storage=Storage())
    assert second.exit_code == 0 and second.persisted
    assert manager.acquire.call_count == 2 and manager.release.call_count == 3
    assert owner.pending_cleanup is None and owner.owned_manager is None
    assert_competing_suite_allowed()


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, asyncio.CancelledError])
def test_failed_retry_preserves_a_distinct_replacement_duty(fault):
    replacement = CleanupPendingError((), AsyncMock())
    owner = SimpleNamespace(pending_cleanup=None)
    async def replace_then_fail():
        owner.pending_cleanup = replacement
        raise fault('interrupted acknowledgement')
    original = CleanupPendingError((), replace_then_fail)

    async def scenario():
        with pytest.raises(fault):
            await retry_owned_cleanup(owner, original)
        assert owner.pending_cleanup is replacement
        replacement.retry.assert_not_awaited()
    asyncio.run(scenario())


def test_retry_restores_the_canonical_receipt_after_cancellation():
    owner = SimpleNamespace(pending_cleanup=None)
    async def dispose_then_cancel():
        owner.pending_cleanup = None
        raise asyncio.CancelledError
    pending = CleanupPendingError((), dispose_then_cancel)
    owner.pending_cleanup = pending
    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await retry_owned_cleanup(owner, pending)
        assert owner.pending_cleanup is pending
    asyncio.run(scenario())


@pytest.mark.parametrize('stored', ['canonical', 'replacement', 'cleared'])
def test_successful_acknowledgement_clears_only_the_receipt_it_retried(stored):
    replacement = CleanupPendingError((), AsyncMock())
    owner = SimpleNamespace(pending_cleanup=None)
    async def acknowledge():
        if stored == 'replacement':
            owner.pending_cleanup = replacement
        elif stored == 'cleared':
            owner.pending_cleanup = None
    pending = CleanupPendingError((), acknowledge)
    owner.pending_cleanup = pending

    asyncio.run(retry_owned_cleanup(owner, pending))

    assert owner.pending_cleanup is (replacement if stored == 'replacement' else None)
    replacement.retry.assert_not_awaited()
