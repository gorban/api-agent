"""Parent cleanup receipts hold exclusion until all retained resources settle."""
import asyncio
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import launched, owned, suite_lock as locks
from api_agent.testing.prerequisites import RecordingPrerequisites
from test_operation_lifecycle import rig
from test_recorded_integration import fixture, fake_runner, Storage
from test_suite_cleanup_lease import assert_competing_suite_blocked, assert_competing_suite_allowed


@pytest.mark.parametrize('primary', [None, ValueError('workload failed'), KeyboardInterrupt()])
def test_launched_prerequisite_retry_holds_exclusion_through_failed_release(tmp_path, primary):
    context, test, config = fixture(tmp_path)
    manager = Mock(lease=object())
    manager.release.side_effect = [RuntimeError('release failed'), RuntimeError('retry failed'), None]
    prerequisites = RecordingPrerequisites(prepare_dependencies=lambda: None,
        plan=lambda config: object(), manager=lambda *args: manager)
    runner = fake_runner()
    if primary is not None:
        def runner(*args, **hooks):
            hooks['before_run']()
            raise primary
    with pytest.raises(BaseException) as caught:
        launched.run_launched_integration(context, [test], config, accepted=True,
            prerequisites=prerequisites, runner=runner, storage=Storage())
    receipt = caught.value.cleanup_pending
    assert caught.value is (primary or receipt)
    assert receipt is prerequisites.pending_cleanup
    assert prerequisites.owned_manager is manager
    assert_competing_suite_blocked()
    with pytest.raises(CleanupPendingError) as retry:
        asyncio.run(receipt.retry())
    assert_competing_suite_blocked()
    asyncio.run(retry.value.retry())
    assert prerequisites.pending_cleanup is None and prerequisites.owned_manager is None
    assert manager.release.call_count == 3
    assert_competing_suite_allowed()


@pytest.mark.parametrize('primary', [None, ValueError('workload failed'), asyncio.CancelledError()])
def test_owned_operation_listener_retry_retains_exclusion(rig, tmp_path, primary):
    operation, events, resolved = rig
    context, test, config = fixture(tmp_path)
    resolved.values.update(config)
    compose = operation.compose_runtime
    def recording_runtime(*args):
        runtime = compose(*args)
        runtime.emissions = None
        return runtime
    operation.compose_runtime = recording_runtime
    listener = operation.listener()
    stop = listener.stop
    attempts = 0
    async def failing_stop():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError('stop failed')
        await stop()
    listener.stop = failing_stop
    operation.listener = lambda: listener
    def runner(*args, **kwargs):
        if primary is not None:
            raise primary
        return fake_runner()(*args, **kwargs)
    async def scenario():
        with pytest.raises(BaseException) as caught:
            await owned.run_owned_integration(context, [test], config, accepted=True,
                operation=operation, runner=runner, storage=Storage())
        receipt = caught.value if isinstance(caught.value, CleanupPendingError) else caught.value.cleanup_pending
        assert type(caught.value) is type(primary or receipt)
        assert caught.value.args == (primary or receipt).args
        assert operation.pending_cleanup is receipt
        assert events[-1] != 'release:True'
        assert_competing_suite_blocked()
        with pytest.raises(CleanupPendingError) as retry:
            await receipt.retry()
        assert_competing_suite_blocked()
        await retry.value.retry()
        assert operation.pending_cleanup is None
        assert events[-2:] == ['runtime-close', 'release:True']
        assert_competing_suite_allowed()
    asyncio.run(scenario())
    assert attempts == 3


@pytest.mark.parametrize('error', [ValueError('plain'), CleanupPendingError((), None)])
def test_nonretryable_errors_do_not_capture_a_suite(error):
    locks.retain_cleanup_receipt(error)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(error)
        assert not locks._CURRENT.get().owners
    assert_competing_suite_allowed()


def test_cancelled_receipt_retry_keeps_original_owner_until_success():
    attempts = 0
    async def retry():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError()
    receipt = CleanupPendingError((), retry)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(receipt)
        locks.retain_cleanup_receipt(receipt)
        assert locks._CURRENT.get().owners == {id(receipt)}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(receipt.retry())
    assert_competing_suite_blocked()
    asyncio.run(receipt.retry())
    assert_competing_suite_allowed()


def test_receipt_on_cancellation_can_transfer_to_a_successor():
    async def complete():
        pass
    successor = CleanupPendingError((), complete)
    async def retry():
        error = asyncio.CancelledError()
        error.cleanup_pending = successor
        raise error
    receipt = CleanupPendingError((), retry)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(receipt)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(receipt.retry())
    assert_competing_suite_blocked()
    asyncio.run(successor.retry())
    assert_competing_suite_allowed()


def test_foreign_context_cannot_capture_receipt():
    from contextvars import copy_context
    async def retry():
        pass
    receipt = CleanupPendingError((), retry)
    with locks.suite_lock():
        context = copy_context()
    context.run(locks.retain_cleanup_receipt, receipt)
    assert_competing_suite_allowed()
