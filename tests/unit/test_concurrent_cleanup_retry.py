"""Concurrent observers cannot duplicate disposal or release suite exclusion."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing import suite_lock as locks


def competing_process(lease, *, blocked):
    """Use a fresh interpreter and its own kernel descriptor, never a mock."""
    path = locks.suite_lock_path()
    code = '''
from pathlib import Path
import sys
from api_agent.testing import suite_lock as locks
locks.cache_path = lambda: Path(sys.argv[1]).with_name('cost-history.sqlite3')
try:
    with locks.suite_lock(): pass
except locks.SuiteBusyError:
    raise SystemExit(17)
'''
    result = subprocess.run([sys.executable, '-c', code, str(path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == (17 if blocked else 0), result.stderr
    assert bool(lease.owners) is blocked


def retain(*receipts):
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        for receipt in receipts:
            locks.retain_cleanup_receipt(receipt)
    return lease


def observe_waiter(monkeypatch, entered, lease):
    original = asyncio.wrap_future
    def wrap(future, **kwargs):
        with lease.guard:
            if any(future is state.running for state in lease.receipts.values()):
                entered.set()
        return original(future, **kwargs)
    monkeypatch.setattr(asyncio, 'wrap_future', wrap)


@pytest.mark.parametrize('saved_handle', ['parent', 'child'])
def test_overlapping_successor_retry_and_cancelled_waiter_keep_one_disposal(monkeypatch, saved_handle):
    async def scenario():
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls, acknowledgements = [], []
        async def child_retry():
            calls.append('child')
            if calls.count('child') > 1:
                queued.set()
            entered.set()
            await release.wait()
        child = CleanupPendingError((), child_retry)
        async def parent_retry():
            calls.append('parent')
            raise child
        parent = CleanupPendingError((), parent_retry)
        lease = retain(parent)
        original_release = lease.release
        def acknowledge(identifier):
            acknowledgements.append(identifier)
            original_release(identifier)
        lease.release = acknowledge
        with pytest.raises(CleanupPendingError):
            await parent.retry()
        observe_waiter(monkeypatch, queued, lease)
        first = asyncio.create_task(parent.retry())
        await asyncio.wait_for(entered.wait(), 15)
        handle = parent if saved_handle == 'parent' else child
        waiter = asyncio.create_task(handle.retry())
        fresh = None
        try:
            await asyncio.wait_for(queued.wait(), 15)
            ticket = lease.receipts[id(child)].running
            competing_process(lease, blocked=True)
            assert calls == ['parent', 'child']
            waiter.cancel('stop waiting')
            with pytest.raises(asyncio.CancelledError) as interrupted:
                await waiter
            assert interrupted.value.args == ('stop waiting',)
            assert not ticket.cancelled() and not ticket.done() and not first.done()
            queued.clear()
            fresh = asyncio.create_task(handle.retry())
            await asyncio.wait_for(queued.wait(), 15)
            competing_process(lease, blocked=True)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, fresh), 15)
            assert calls == ['parent', 'child']
            assert Counter(acknowledgements) == Counter({id(parent): 1, id(child): 1})
            assert all(state.running is None and state.retry_task is None for state in lease.receipts.values())
            competing_process(lease, blocked=False)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, waiter, fresh) if task is not None), return_exceptions=True)
            await parent.retry()
    asyncio.run(scenario())


@pytest.mark.parametrize('interruption', ['failure', 'cancel'])
def test_interrupted_owner_wakes_waiter_only_after_active_callback_settles(monkeypatch, interruption):
    async def scenario():
        entered, release, cancelled, settled = (asyncio.Event() for _ in range(4))
        second_entered, second_release, queued = (asyncio.Event() for _ in range(3))
        failure, calls = RuntimeError('disposal failed'), []
        async def retry():
            calls.append('attempt')
            if len(calls) == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await settled.wait()
                    raise
                raise failure
            second_entered.set()
            await second_release.wait()
        receipt = CleanupPendingError((), retry)
        lease = retain(receipt)
        observe_waiter(monkeypatch, queued, lease)
        first = asyncio.create_task(receipt.retry())
        await asyncio.wait_for(entered.wait(), 15)
        waiter = asyncio.create_task(receipt.retry())
        try:
            await asyncio.wait_for(queued.wait(), 15)
            if interruption == 'cancel':
                first.cancel('cancel disposal')
                await asyncio.wait_for(cancelled.wait(), 15)
                competing_process(lease, blocked=True)
                assert not waiter.done() and calls == ['attempt']
                settled.set()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await first
                assert caught.value.args == ('cancel disposal',)
            else:
                release.set()
                with pytest.raises(RuntimeError) as caught:
                    await first
                assert caught.value is failure
            await asyncio.wait_for(second_entered.wait(), 15)
            assert calls == ['attempt', 'attempt']
            competing_process(lease, blocked=True)
            second_release.set()
            await asyncio.wait_for(waiter, 15)
            await receipt.retry()
            assert calls == ['attempt', 'attempt']
            competing_process(lease, blocked=False)
        finally:
            release.set(); settled.set(); second_release.set()
            await asyncio.gather(first, waiter, return_exceptions=True)
            await receipt.retry()
    asyncio.run(scenario())


def test_same_task_nested_retry_and_inherited_child_observer_preserve_pending():
    async def scenario():
        calls, delegated = [], []
        async def child_retry():
            calls.append('child')
            with pytest.raises(CleanupPendingError) as cyclic:
                await parent.retry()
            assert cyclic.value is parent
        child = CleanupPendingError((), child_retry)
        async def parent_retry():
            calls.append('parent')
            delegated.append(asyncio.create_task(parent.retry()))
            with pytest.raises(CleanupPendingError) as observer:
                await delegated[0]
            assert observer.value is parent
            await child.retry()
            assert calls == ['parent', 'child']
            competing_process(lease, blocked=True)
        parent = CleanupPendingError((), parent_retry)
        lease = retain(parent, child)
        await parent.retry()
        await parent.retry(); await child.retry()
        assert calls == ['parent', 'child']
        competing_process(lease, blocked=False)
    asyncio.run(scenario())


def test_receipt_observers_on_different_threads_and_loops_share_one_retry(monkeypatch):
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    calls = []
    async def retry():
        calls.append(threading.get_ident())
        entered.set()
        assert await asyncio.to_thread(release.wait, 15)
    receipt = CleanupPendingError((), retry)
    lease = retain(receipt)
    observe_waiter(monkeypatch, queued, lease)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(asyncio.run, receipt.retry())
        assert entered.wait(15)
        second = executor.submit(asyncio.run, receipt.retry())
        try:
            assert queued.wait(15)
            competing_process(lease, blocked=True)
            assert len(calls) == 1 and not first.done() and not second.done()
        finally:
            release.set()
            first.result(timeout=15); second.result(timeout=15)
    asyncio.run(receipt.retry())
    assert len(calls) == 1
    assert lease.receipts[id(receipt)].running is None and not lease.retry_waits
    competing_process(lease, blocked=False)


def test_competing_cycle_traversals_do_not_duplicate_callbacks_or_notifications(monkeypatch):
    async def scenario():
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def parent_retry():
            calls.append('parent')
            raise child
        async def child_retry():
            calls.append('child')
            if calls.count('child') < 3:
                raise parent
            entered.set()
            await release.wait()
        parent = CleanupPendingError((), parent_retry)
        child = CleanupPendingError((), child_retry)
        lease = retain(parent)
        with pytest.raises(CleanupPendingError): await parent.retry()
        with pytest.raises(CleanupPendingError): await child.retry()
        with pytest.raises(CleanupPendingError): await parent.retry()
        assert len(lease.receipts[id(parent)].parents) == 1
        assert len(lease.receipts[id(child)].parents) == 1
        observe_waiter(monkeypatch, queued, lease)
        first = asyncio.create_task(parent.retry())
        await asyncio.wait_for(entered.wait(), 15)
        second = asyncio.create_task(child.retry())
        try:
            await asyncio.wait_for(queued.wait(), 15)
            competing_process(lease, blocked=True)
            assert calls == ['parent', 'child', 'child', 'child']
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 15)
            await parent.retry(); await child.retry()
            assert calls == ['parent', 'child', 'child', 'child']
            assert all(state.acknowledged and not state.parents for state in lease.receipts.values())
            competing_process(lease, blocked=False)
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await parent.retry(); await child.retry()
    asyncio.run(scenario())


def test_cancelled_cross_loop_waiter_can_close_its_loop_without_cancelling_disposal(monkeypatch):
    entered, release, queued, ready = (threading.Event() for _ in range(4))
    calls, observer = [], []
    async def retry():
        calls.append('disposal')
        entered.set()
        assert await asyncio.to_thread(release.wait, 15)
    receipt = CleanupPendingError((), retry)
    lease = retain(receipt)
    observe_waiter(monkeypatch, queued, lease)
    async def wait_elsewhere():
        observer.extend((asyncio.get_running_loop(), asyncio.current_task()))
        ready.set()
        await receipt.retry()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(asyncio.run, receipt.retry())
        assert entered.wait(15)
        waiter = executor.submit(asyncio.run, wait_elsewhere())
        try:
            assert ready.wait(15) and queued.wait(15)
            ticket = lease.receipts[id(receipt)].running
            observer[0].call_soon_threadsafe(observer[1].cancel, 'leave observer loop')
            with pytest.raises(asyncio.CancelledError) as cancelled:
                waiter.result(timeout=15)
            assert cancelled.value.args == ('leave observer loop',)
            assert observer[0].is_closed()
            assert not ticket.done() and not first.done()
            competing_process(lease, blocked=True)
        finally:
            release.set()
            first.result(timeout=15)
    asyncio.run(receipt.retry())
    assert calls == ['disposal']
    competing_process(lease, blocked=False)


@pytest.mark.parametrize('adapter', ['finish_cleanup', 'operation_cleanup'])
def test_original_callback_can_await_task_based_child_cleanup(adapter):
    from api_agent.providers.control import finish_cleanup
    from api_agent.runtime.operation import OperationCleanup
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def child_retry():
            calls.append('child')
            entered.set()
            await release.wait()
        child = CleanupPendingError((), child_retry)
        class Runtime:
            async def aclose(self):
                calls.append('runtime')
                await child.retry()
        operation = OperationCleanup(None, Runtime(), None, None, False, 15)
        async def parent_retry():
            calls.append('parent')
            if adapter == 'finish_cleanup':
                await finish_cleanup(child.retry())
            else:
                await operation.retry()
            calls.append('parent complete')
        parent = CleanupPendingError((), parent_retry)
        lease = retain(parent, child)
        task = asyncio.create_task(parent.retry())
        try:
            await asyncio.wait_for(entered.wait(), 15)
            competing_process(lease, blocked=True)
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 15)
            await parent.retry(); await child.retry()
            expected = ['parent', 'child', 'parent complete'] if adapter == 'finish_cleanup' else ['parent', 'runtime', 'child', 'parent complete']
            assert calls == expected
            competing_process(lease, blocked=False)
        finally:
            release.set()
            await task
    asyncio.run(scenario())


def test_cross_task_callback_cycle_exports_pending_and_retains_late_work(monkeypatch):
    async def scenario():
        entered = [asyncio.Event(), asyncio.Event()]
        proceed, late, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls, cyclic, queued = [], [], asyncio.Event()
        async def first_retry():
            calls.append('first')
            entered[0].set()
            await proceed.wait()
            await second.retry()
            late.set()
            await finish.wait()
        async def second_retry():
            calls.append('second')
            entered[1].set()
            await proceed.wait()
            await queued.wait()
            with pytest.raises(CleanupPendingError) as caught:
                await first.retry()
            cyclic.append(caught.value)
        first = CleanupPendingError((), first_retry)
        second = CleanupPendingError((), second_retry)
        lease = retain(first, second)
        observe_waiter(monkeypatch, queued, lease)
        one = asyncio.create_task(first.retry())
        two = asyncio.create_task(second.retry())
        try:
            await asyncio.wait_for(entered[0].wait(), 15)
            await asyncio.wait_for(entered[1].wait(), 15)
            proceed.set()
            await asyncio.wait_for(late.wait(), 15)
            await two
            assert cyclic == [first] and calls == ['first', 'second']
            assert not one.done() and not lease.receipts[id(first)].acknowledged
            competing_process(lease, blocked=True)
            finish.set()
            await asyncio.wait_for(one, 15)
            await first.retry(); await second.retry()
            assert calls == ['first', 'second'] and not lease.retry_waits
            competing_process(lease, blocked=False)
        finally:
            proceed.set(); finish.set()
            await asyncio.gather(one, two, return_exceptions=True)
    asyncio.run(scenario())


def test_public_cycle_retry_cannot_claim_success_with_historical_parent_still_active(monkeypatch):
    async def scenario():
        late, finish, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def child_retry():
            calls.append('child')
            if calls.count('child') == 1:
                raise parent
        child = CleanupPendingError((), child_retry)
        async def parent_retry():
            calls.append('parent')
            try:
                with pytest.raises(CleanupPendingError) as pending:
                    await child.retry()
                assert pending.value is child
            finally:
                late.set()
            await finish.wait()
        parent = CleanupPendingError((), parent_retry)
        lease = retain(parent, child)
        with pytest.raises(CleanupPendingError):
            await child.retry()
        observe_waiter(monkeypatch, queued, lease)
        owner = asyncio.create_task(parent.retry())
        waiter = None
        try:
            await asyncio.wait_for(late.wait(), 15)
            assert not owner.done()
            assert lease.receipts[id(child)].successors == {lease.receipts[id(parent)]}
            assert not lease.receipts[id(parent)].acknowledged
            waiter = asyncio.create_task(child.retry())
            await asyncio.wait_for(queued.wait(), 15)
            assert not waiter.done() and not owner.done()
            competing_process(lease, blocked=True)
            finish.set()
            await asyncio.wait_for(asyncio.gather(owner, waiter), 15)
            await child.retry(); await parent.retry()
            assert calls == ['child', 'parent', 'child']
            assert all(state.acknowledged and not state.successors for state in lease.receipts.values())
            competing_process(lease, blocked=False)
        finally:
            finish.set()
            await asyncio.gather(*(task for task in (owner, waiter) if task is not None), return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('adapter', ['finish_cleanup', 'operation_cleanup'])
def test_task_delegation_cycle_exports_original_receipt_and_can_be_retried(adapter):
    from api_agent.providers.control import finish_cleanup
    from api_agent.runtime.operation import OperationCleanup
    async def scenario():
        calls, allow_cleanup = [], False
        async def child_retry():
            calls.append('child')
            await parent.retry()
        child = CleanupPendingError((), child_retry)
        class Runtime:
            async def aclose(self):
                if not allow_cleanup:
                    await child.retry()
        operation = OperationCleanup(None, Runtime(), None, None, False, 15)
        async def parent_retry():
            calls.append('parent')
            if calls.count('parent') == 1:
                if adapter == 'finish_cleanup':
                    await finish_cleanup(child.retry())
                else:
                    await operation.retry()
        parent = CleanupPendingError((), parent_retry)
        lease = retain(parent, child)
        with pytest.raises(CleanupPendingError) as pending:
            await asyncio.wait_for(parent.retry(), 15)
        # OperationCleanup owns its outer adapter receipt; both exact retained
        # capabilities remain usable, without a false successful child retry.
        assert calls == ['parent', 'child']
        assert pending.value is parent or adapter == 'operation_cleanup'
        competing_process(lease, blocked=True)
        allow_cleanup = True
        await parent.retry(); await child.retry()
        if adapter == 'operation_cleanup':
            await pending.value.retry()
        expected = ['parent', 'child', 'parent'] if adapter == 'finish_cleanup' else ['parent', 'child']
        assert calls == expected
        assert all(state.running is None and state.acknowledged for state in lease.receipts.values())
        competing_process(lease, blocked=False)
    asyncio.run(scenario())


def test_stale_inherited_attempt_does_not_reject_a_later_independent_retry(monkeypatch):
    async def scenario():
        go, entered, release, queued = (asyncio.Event() for _ in range(4))
        calls, stale, rejected = [], [], []
        failure = RuntimeError('first attempt failed')
        async def observe_later():
            await go.wait()
            try:
                await receipt.retry()
            except CleanupPendingError as error:
                rejected.append(error)
                queued.set()
                raise
        async def retry():
            calls.append('attempt')
            if len(calls) == 1:
                stale.append(asyncio.create_task(observe_later()))
                raise failure
            entered.set()
            await release.wait()
        receipt = CleanupPendingError((), retry)
        lease = retain(receipt)
        observe_waiter(monkeypatch, queued, lease)
        with pytest.raises(RuntimeError) as original:
            await receipt.retry()
        assert original.value is failure
        current = asyncio.create_task(receipt.retry())
        try:
            await asyncio.wait_for(entered.wait(), 15)
            go.set()
            await asyncio.wait_for(queued.wait(), 15)
            assert not rejected and not stale[0].done()
            competing_process(lease, blocked=True)
            release.set()
            await asyncio.wait_for(asyncio.gather(current, stale[0]), 15)
            assert calls == ['attempt', 'attempt']
            competing_process(lease, blocked=False)
        finally:
            go.set(); release.set()
            await asyncio.gather(current, *stale, return_exceptions=True)
            await receipt.retry()
    asyncio.run(scenario())


def test_interrupted_cyclic_notification_replays_without_duplicate_disposal(monkeypatch):
    async def scenario():
        calls, releases = [], []
        interruption = KeyboardInterrupt('notification interrupted')
        async def parent_retry():
            calls.append('parent')
            if calls.count('parent') == 1:
                raise child
        async def child_retry():
            calls.append('child')
            raise parent
        parent = CleanupPendingError((), parent_retry)
        child = CleanupPendingError((), child_retry)
        lease = retain(parent, child)
        with pytest.raises(CleanupPendingError): await parent.retry()
        with pytest.raises(CleanupPendingError): await child.retry()
        child_state = lease.receipts[id(child)]
        notify = child_state._successor_finished
        failed = False
        def interrupted_notification(state):
            nonlocal failed
            notify(state)
            if not failed:
                failed = True
                raise interruption
        monkeypatch.setattr(child_state, '_successor_finished', interrupted_notification)
        release = lease.release
        def record_release(identifier):
            releases.append(identifier)
            release(identifier)
        monkeypatch.setattr(lease, 'release', record_release)
        with pytest.raises(KeyboardInterrupt) as caught:
            await child.retry()
        assert caught.value is interruption and caught.value.args == ('notification interrupted',)
        await parent.retry(); await child.retry()
        assert calls == ['parent', 'child', 'parent']
        assert Counter(releases) == Counter({id(parent): 1, id(child): 1})
        assert all(state.acknowledged and not state.parents for state in lease.receipts.values())
        competing_process(lease, blocked=False)
    asyncio.run(scenario())
