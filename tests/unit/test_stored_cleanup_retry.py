"""Stable cleanup receipts settle prior observations before a new suite starts."""
import asyncio
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.operation import OperationCleanup
from api_agent.testing import launched, suite_lock as locks
from api_agent.testing.prerequisites import RecordingPrerequisites
from test_recorded_integration import fixture, Storage, fake_runner
from test_suite_cleanup_lease import assert_competing_suite_allowed, assert_competing_suite_blocked
from test_tui import fixture as tui_fixture


@pytest.fixture
def failed_prerequisites(tmp_path):
    context, test, config = fixture(tmp_path)
    manager = Mock(lease=object())
    manager.acquire.return_value = manager.lease
    manager.release.side_effect = [RuntimeError('initial release pending'), RuntimeError('retry pending'), None]
    owner = RecordingPrerequisites(prepare_dependencies=lambda: None,
        plan=lambda config: (), manager=lambda *args: manager)
    leases = []
    with pytest.raises(CleanupPendingError) as caught:
        launched.run_launched_integration(context, (test,), config, accepted=True,
            prerequisites=owner, runner=fake_runner(), storage=Storage(),
            prepare=lambda usage: leases.append(locks._CURRENT.get()))
    assert owner.pending_cleanup is caught.value
    try:
        yield owner, caught.value, manager
    finally:
        manager.release.side_effect = None
        if owner.pending_cleanup is not None:
            asyncio.run(owner.pending_cleanup.retry())
        asyncio.run(caught.value.retry())
        # Even a deliberately broken retention control must relinquish its
        # disposable lease after the main assertion has exposed the defect.
        for state in tuple(leases[0].receipts.values()):
            asyncio.run(state.pending.retry())
        assert_competing_suite_allowed()


def test_launched_original_receipt_converges_after_a_failed_retry(failed_prerequisites):
    owner, original, manager = failed_prerequisites
    with pytest.raises(CleanupPendingError) as failure:
        asyncio.run(original.retry())
    assert failure.value is not original and owner.pending_cleanup is original
    assert_competing_suite_blocked()
    asyncio.run(original.retry())
    assert owner.pending_cleanup is None and owner.owned_manager is None
    assert manager.release.call_count == 3
    assert_competing_suite_allowed()
    asyncio.run(failure.value.retry())
    assert manager.release.call_count == 3
    assert_competing_suite_allowed()


def test_real_tui_stored_retry_releases_exclusion_when_it_reports_success(failed_prerequisites, tmp_path):
    owner, original, manager = failed_prerequisites
    app = tui_fixture(tmp_path)
    app.recording_cleanup = owner
    app.integration_exit_status = 0
    assert not asyncio.run(app.end_inspection())
    assert app.recording_cleanup is owner and owner.pending_cleanup is original
    assert app.inspection_cleanup_active() and app.operation_status == 3
    assert_competing_suite_blocked()
    assert asyncio.run(app.end_inspection())
    assert owner.pending_cleanup is None and owner.owned_manager is None
    assert app.recording_cleanup is None and not app.inspection_cleanup_active()
    assert app.operation_status == 0 and manager.release.call_count == 3
    assert_competing_suite_allowed()


async def failed_operation_retry(original, entered, release, cancelled):
    """Expose the receipt from an ordinary failure or a cancelled owned retry."""
    if not cancelled:
        with pytest.raises(CleanupPendingError) as failure:
            await original.retry()
        return failure.value
    attempt = asyncio.create_task(original.retry())
    await asyncio.wait_for(entered.wait(), 2)
    attempt.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as failure:
        await attempt
    return failure.value.cleanup_pending


@pytest.mark.parametrize('cancelled', [False, True])
def test_operation_stored_receipt_settles_failed_and_cancelled_observations(cancelled):
    async def scenario():
        attempts, entered, release = [], asyncio.Event(), asyncio.Event()
        class Runtime:
            live = True
            async def aclose(self):
                attempts.append('runtime')
                if len(attempts) == 2 and cancelled:
                    entered.set()
                    await release.wait()
                if len(attempts) < 3:
                    raise RuntimeError('Runtime disposal pending')
                self.live = False
        runtime, manager = Runtime(), Mock()
        cleanup = OperationCleanup(manager, runtime, None, object(), True, 2)
        with pytest.raises(CleanupPendingError) as initial:
            await cleanup.retry()
        original = initial.value
        with locks.suite_lock():
            locks.retain_cleanup_receipt(original)
        try:
            successor = await failed_operation_retry(original, entered, release, cancelled)
            assert successor is not original and runtime.live
            manager.release.assert_not_called()
            assert_competing_suite_blocked()
            await original.retry()
            assert not runtime.live and len(attempts) == 3
            manager.release.assert_called_once_with(retain=True)
            assert_competing_suite_allowed()
            await successor.retry()
            assert len(attempts) == 3
            manager.release.assert_called_once_with(retain=True)
        finally:
            release.set()
            await original.retry()
            assert_competing_suite_allowed()
    asyncio.run(scenario())


def test_stable_retry_cannot_silently_complete_a_distinct_successor():
    original_calls, successor_calls = [], []
    allow_cleanup = False
    async def successor_retry():
        successor_calls.append('attempt')
        if not allow_cleanup:
            raise successor
    successor = CleanupPendingError((object(),), successor_retry)
    async def original_retry():
        original_calls.append('attempt')
        if len(original_calls) == 1:
            raise successor
    original = CleanupPendingError((object(),), original_retry)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(original)
    try:
        with pytest.raises(CleanupPendingError): asyncio.run(original.retry())
        with pytest.raises(CleanupPendingError) as pending: asyncio.run(original.retry())
        assert pending.value is successor and len(original_calls) == 1
        assert successor_calls == ['attempt']
        assert_competing_suite_blocked()
        allow_cleanup = True
        asyncio.run(original.retry())
        assert len(original_calls) == 1 and len(successor_calls) == 2
        assert_competing_suite_allowed()
        asyncio.run(original.retry())
        asyncio.run(successor.retry())
        assert len(original_calls) == 1 and len(successor_calls) == 2
    finally:
        allow_cleanup = True
        asyncio.run(successor.retry())
        asyncio.run(original.retry())
        assert_competing_suite_allowed()



@pytest.mark.parametrize('failed_retries', [1, 3])
def test_stored_retry_settles_every_previous_observation(failed_retries):
    class Owner:
        def __init__(self):
            self.calls = 0
            self.closed = False
        async def retry(self):
            if self.closed:
                return
            self.calls += 1
            if self.calls <= failed_retries:
                raise CleanupPendingError((self,), self.retry)
            self.closed = True
    owner = Owner()
    original = CleanupPendingError((owner,), owner.retry)
    with locks.suite_lock(): locks.retain_cleanup_receipt(original)
    for _ in range(failed_retries):
        with pytest.raises(CleanupPendingError): asyncio.run(original.retry())
        assert_competing_suite_blocked()
    asyncio.run(original.retry())
    assert owner.calls == failed_retries + 1
    assert_competing_suite_allowed()
