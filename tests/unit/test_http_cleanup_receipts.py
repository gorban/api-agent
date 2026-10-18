"""Transferred disposal ownership is settled exactly once across retries."""
import asyncio
import pytest

from api_agent.api.http_cleanup import CallbackState, close_http_callbacks
from api_agent.providers.control import CleanupPendingError


def test_sync_creation_failure_does_not_skip_sibling():
    async def run():
        closed=[]
        def bad():
            raise RuntimeError('cannot create cleanup')
        async def sibling():
            closed.append('sibling')
        callbacks=[bad,sibling]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks)
        assert closed == ['sibling'] and error.value.owners == (bad,)
    asyncio.run(run())


def test_transferred_child_retry_replaces_parent_callback():
    async def run():
        calls=[]
        resource=object()
        async def child():
            calls.append('child')
        receipt=CleanupPendingError((resource,),child)
        async def callback():
            calls.append('parent')
            raise receipt
        callbacks=[callback]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks)
        assert error.value.owners == (resource,)
        await error.value.retry()
        assert calls == ['parent','child'] and callbacks == []
    asyncio.run(run())


def test_replacement_receipt_cycle_reactivates_only_unacknowledged_duty():
    async def run():
        calls=[]
        async def first():
            calls.append('first')
            if calls.count('first') == 1:
                raise second_receipt
        async def second():
            calls.append('second')
            raise first_receipt
        first_receipt=CleanupPendingError(('first',),first)
        second_receipt=CleanupPendingError(('second',),second)
        async def callback():
            calls.append('parent')
            raise first_receipt
        callbacks=[callback]
        for _ in range(2):
            with pytest.raises(CleanupPendingError):
                await close_http_callbacks(callbacks)
        await close_http_callbacks(callbacks)
        assert calls == ['parent','first','second','first'] and not callbacks
    asyncio.run(run())


def test_same_current_group_preserves_new_and_unknown_duties_without_duplication():
    async def run():
        calls=[]
        release=False
        async def child():
            calls.append('child')
        child_receipt=CleanupPendingError(('child',),child)
        async def current():
            calls.append('current')
            if not release:
                raise ExceptionGroup('pending',[current_receipt,child_receipt,RuntimeError('unclassified')])
        current_receipt=CleanupPendingError(('current',),current)
        async def callback():
            calls.append('parent')
            raise current_receipt
        callbacks=[callback]
        for _ in range(4):
            with pytest.raises(CleanupPendingError):
                await close_http_callbacks(callbacks)
        assert calls.count('child') == 1 and calls.count('parent') == 1
        release=True
        await close_http_callbacks(callbacks)
        assert calls.count('child') == 1 and not callbacks
    asyncio.run(run())


def test_empty_owner_receipt_still_blocks_completion():
    async def run():
        closed=[]
        async def child():
            closed.append(True)
        async def callback():
            raise CleanupPendingError((),child)
        callbacks=[callback]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks)
        await error.value.retry()
        assert closed == [True] and not callbacks
    asyncio.run(run())


def test_cancellation_inside_group_preserves_interruption_and_sibling():
    async def run():
        closed=[]
        release=False
        async def callback():
            if not release:
                raise BaseExceptionGroup('interrupted',[asyncio.CancelledError()])
        async def sibling():
            closed.append(True)
        callbacks=[callback,sibling]
        with pytest.raises(asyncio.CancelledError) as error:
            await close_http_callbacks(callbacks)
        assert closed == [True]
        release=True
        await error.value.cleanup_pending.retry()
        assert not callbacks
    asyncio.run(run())


def test_interruption_while_waiting_for_existing_state_keeps_its_owner():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def callback():
            entered.set()
            await release.wait()
            calls.append(True)
        state=CallbackState(callback)
        callbacks=[state]
        first=asyncio.create_task(close_http_callbacks(callbacks))
        await entered.wait()
        second=asyncio.create_task(close_http_callbacks(callbacks))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await second
        release.set()
        await first
        await error.value.cleanup_pending.retry()
        assert calls == [True] and not callbacks
    asyncio.run(run())


def test_hung_callback_retains_one_task_and_allows_sibling_disposal():
    async def run():
        release=asyncio.Event()
        calls=[]
        async def blocked():
            calls.append('blocked')
            await release.wait()
            calls.append('closed')
        async def sibling():
            calls.append('sibling')
        callbacks=[blocked,sibling]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks,timeout=.02)
        task=callbacks[0].entries[0].task
        assert calls == ['blocked','sibling'] and not task.done()
        with pytest.raises(CleanupPendingError):
            await error.value.retry()
        assert callbacks[0].entries[0].task is task and calls == ['blocked','sibling']
        release.set()
        await error.value.retry()
        assert calls == ['blocked','sibling','closed'] and not callbacks
    asyncio.run(run())


def test_interrupted_hung_callback_returns_receipt_without_cancelling_work():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def blocked():
            entered.set()
            await release.wait()
            calls.append(True)
        callbacks=[blocked]
        operation=asyncio.create_task(close_http_callbacks(callbacks,timeout=.02))
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await operation
        task=callbacks[0].entries[0].task
        assert not task.done() and not task.cancelling()
        release.set()
        await error.value.cleanup_pending.retry()
        assert calls == [True] and not callbacks
    asyncio.run(run())


def test_busy_state_lock_is_bounded_without_restarting_callback():
    async def run():
        calls=[]
        async def callback():
            calls.append(True)
        state=CallbackState(callback)
        await state.lock.acquire()
        callbacks=[state]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks,timeout=.01)
        assert calls == [True]
        state.lock.release()
        await error.value.retry()
        assert calls == [True] and not callbacks
    asyncio.run(run())


def test_concurrent_registry_waiters_acknowledge_one_callback_execution():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def callback():
            entered.set()
            await release.wait()
            calls.append(True)
        callbacks=[callback]
        first=asyncio.create_task(close_http_callbacks(callbacks))
        await entered.wait()
        second=asyncio.create_task(close_http_callbacks(callbacks))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first,second)
        assert calls == [True] and not callbacks
    asyncio.run(run())


@pytest.mark.parametrize('name',['heartbeat','stop_heartbeat'])
def test_actual_progress_allocation_failure_retains_callbacks_and_acknowledgements(monkeypatch,name):
    import inspect
    from api_agent.providers.progress import progress_sink
    async def run():
        original=asyncio.create_task
        rejected=[]
        calls=[]
        def create(coroutine,**kwargs):
            if coroutine.cr_code.co_name == name:
                rejected.append(coroutine)
                raise RuntimeError('controlled progress allocator failure')
            return original(coroutine,**kwargs)
        async def callback():
            calls.append(True)
        callbacks=[callback]
        monkeypatch.setattr(asyncio,'create_task',create)
        with progress_sink(lambda event:None):
            with pytest.raises(CleanupPendingError) as error:
                await close_http_callbacks(callbacks)
        assert len(rejected) == 1 and inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
        assert calls == ([] if name == 'heartbeat' else [True])
        monkeypatch.setattr(asyncio,'create_task',original)
        await error.value.retry()
        assert calls == [True] and not callbacks
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_progress_entry_failure_preserves_already_running_callback_task(monkeypatch):
    from api_agent.providers.progress import progress_sink
    async def run():
        release=asyncio.Event()
        calls=[]
        async def callback():
            calls.append('started')
            await release.wait()
            calls.append('closed')
        callbacks=[callback]
        with pytest.raises(CleanupPendingError):
            await close_http_callbacks(callbacks,timeout=.01)
        task=callbacks[0].entries[0].task
        original=asyncio.create_task
        def create(coroutine,**kwargs):
            if coroutine.cr_code.co_name == 'heartbeat':
                raise RuntimeError('observer unavailable')
            return original(coroutine,**kwargs)
        monkeypatch.setattr(asyncio,'create_task',create)
        with progress_sink(lambda event:None):
            with pytest.raises(CleanupPendingError) as error:
                await close_http_callbacks(callbacks,timeout=.01)
        assert callbacks[0].entries[0].task is task and calls == ['started']
        monkeypatch.setattr(asyncio,'create_task',original)
        release.set()
        await error.value.retry()
        assert calls == ['started','closed'] and not callbacks
    asyncio.run(run())


@pytest.mark.parametrize('locked',[False,True])
@pytest.mark.parametrize('failure',[RuntimeError,asyncio.CancelledError])
def test_timing_entry_failure_retires_finished_state_and_retains_busy_state(monkeypatch,locked,failure):
    from api_agent.providers.progress import progress_sink
    async def run():
        calls=[]
        async def callback():
            calls.append(True)
        state=CallbackState(callback)
        if locked:
            await state.lock.acquire()
        else:
            state.start(set())
            await asyncio.sleep(0)
        callbacks=[state]
        original=asyncio.create_task
        def create(coroutine,**kwargs):
            if coroutine.cr_code.co_name == 'heartbeat':
                raise failure()
            return original(coroutine,**kwargs)
        monkeypatch.setattr(asyncio,'create_task',create)
        expected=asyncio.CancelledError if failure is asyncio.CancelledError else CleanupPendingError
        with progress_sink(lambda event:None):
            with pytest.raises(expected) as error:
                await close_http_callbacks(callbacks)
        assert (state in callbacks) is locked
        if locked:
            state.lock.release()
        monkeypatch.setattr(asyncio,'create_task',original)
        receipt=error.value.cleanup_pending if failure is asyncio.CancelledError else error.value
        await receipt.retry()
        assert calls == [True] and not callbacks
    asyncio.run(run())


def test_repeated_timing_failure_does_not_accumulate_replayed_observer_duties(monkeypatch):
    from api_agent.providers.progress import progress_sink
    async def run():
        callbacks=[]
        original=asyncio.create_task
        def create(coroutine,**kwargs):
            if coroutine.cr_code.co_name == 'heartbeat':
                raise RuntimeError('observer unavailable')
            return original(coroutine,**kwargs)
        monkeypatch.setattr(asyncio,'create_task',create)
        with progress_sink(lambda event:None):
            for _ in range(3):
                with pytest.raises(CleanupPendingError):
                    await close_http_callbacks(callbacks)
                assert callbacks == []
    asyncio.run(run())


def test_timing_failure_transfers_child_receipt_without_repeating_current_duty(monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.api import http_cleanup
    async def run():
        calls=[]
        async def child():
            calls.append(True)
        receipt=CleanupPendingError(('observer-resource',),child)
        @asynccontextmanager
        async def failed_scope(*args):
            raise ExceptionGroup('observer failure',[receipt,receipt,RuntimeError('presentation')])
            yield
        original=http_cleanup.report_timeout
        monkeypatch.setattr(http_cleanup,'report_timeout',failed_scope)
        callbacks=[]
        for _ in range(2):
            with pytest.raises(CleanupPendingError) as error:
                await close_http_callbacks(callbacks)
            assert len(callbacks) == 1
            assert error.value.owners == ('observer-resource',)
        monkeypatch.setattr(http_cleanup,'report_timeout',original)
        await error.value.retry()
        assert calls == [True] and not callbacks
    asyncio.run(run())


def test_distinct_callbacks_share_one_transferred_receipt_duty():
    async def run():
        calls=[]
        async def child():
            calls.append('child')
        receipt=CleanupPendingError(('shared-resource',),child)
        async def first():
            calls.append('first')
            raise receipt
        async def second():
            calls.append('second')
            raise receipt
        callbacks=[first,second]
        with pytest.raises(CleanupPendingError) as error:
            await close_http_callbacks(callbacks)
        assert error.value.owners == ('shared-resource',)
        await error.value.retry()
        assert calls == ['first','second','child'] and not callbacks
    asyncio.run(run())


def test_shared_receipt_overlapping_retries_keep_one_hung_child_task():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def child():
            entered.set()
            calls.append('started')
            await release.wait()
            calls.append('finished')
        receipt=CleanupPendingError(('shared-resource',),child)
        async def first():
            raise receipt
        async def second():
            raise receipt
        callbacks=[first,second]
        with pytest.raises(CleanupPendingError):
            await close_http_callbacks(callbacks,timeout=.02)
        first_retry=asyncio.create_task(close_http_callbacks(callbacks,timeout=.02))
        await entered.wait()
        second_retry=asyncio.create_task(close_http_callbacks(callbacks,timeout=.02))
        for operation in (first_retry,second_retry):
            with pytest.raises(CleanupPendingError) as error:
                await operation
            assert error.value.owners == ('shared-resource',)
        tasks={entry.task for state in callbacks for entry in state.entries if entry.receipt is receipt}
        assert len(tasks) == 1 and calls == ['started']
        release.set()
        await asyncio.gather(close_http_callbacks(callbacks),close_http_callbacks(callbacks))
        assert calls == ['started','finished'] and not callbacks
    asyncio.run(run())


def test_shared_unacknowledged_replacement_survives_original_parent_retirement():
    async def run():
        calls=[]
        transfer=False
        async def final_child():
            calls.append('final-child')
        final_receipt=CleanupPendingError(('final-child',),final_child)
        async def replaced_child():
            calls.append('replaced-child')
            if calls.count('replaced-child') == 1:
                raise final_receipt
        replaced_receipt=CleanupPendingError(('replaced-child',),replaced_child)
        async def first():
            calls.append('first')
            raise replaced_receipt
        async def second():
            calls.append('second')
            if transfer:
                raise replaced_receipt
            raise RuntimeError('independent pending work')
        callbacks=[first,second]
        for _ in range(2):
            with pytest.raises(CleanupPendingError):
                await close_http_callbacks(callbacks)
        assert len(callbacks) == 1 and calls.count('final-child') == 1
        transfer=True
        await close_http_callbacks(callbacks)
        assert calls.count('first') == 1 and calls.count('replaced-child') == 2
        assert calls.count('final-child') == 1 and not callbacks
    asyncio.run(run())
