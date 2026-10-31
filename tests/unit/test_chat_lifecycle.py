"""Controlled event-loop proofs for operation leases and retained cleanup."""
import asyncio
import time

import pytest

from api_agent.api.chat_lifecycle import ChatLifecycle
from api_agent.providers.control import CleanupPendingError, OperationContext, OperationalError


def registry(**options):
    return ChatLifecycle(heartbeat_interval=.01, expiry=.04, cleanup_timeout=.01, **options)


def test_disconnected_invocation_failure_is_reported_without_a_response_consumer():
    async def exercise():
        reported = []
        engine = ChatLifecycle(on_error=reported.append)
        entered = asyncio.Event()
        failure = ValueError('late tool failure')
        async def invoke(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        await engine.disconnect_turn('owner', turn)
        assert reported == [failure]
        engine.report_error(failure, turn=turn)
        assert reported == [failure]
        engine.report_error(asyncio.CancelledError())
        assert reported == [failure]
        await engine.aclose()
    asyncio.run(exercise())


def test_same_exception_instance_is_reported_again_for_a_new_turn_or_catalog():
    async def exercise():
        reported = []
        engine = ChatLifecycle(on_error=reported.append)
        failure = ValueError('reused provider exception')
        async def invoke(context):
            raise failure
        for index in range(2):
            turn = engine.start('owner', str(index), invoke, context=OperationContext(str(index)))
            await engine.wait_turn('owner', turn)
            engine.report_error(failure, turn=turn)
        engine.report_error(failure)
        assert reported == [failure, failure, failure]
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('grouped', [False, True])
def test_failed_context_disposal_is_reported_before_cleanup_retry(grouped):
    async def exercise():
        reported = []
        engine = ChatLifecycle(on_error=reported.append)
        failure = ValueError('resource disposal failed')
        if grouped:
            failure = BaseExceptionGroup('disposal failures', [failure, asyncio.CancelledError()])
        repaired = asyncio.Event()
        class Context(OperationContext):
            async def aclose(self, **options):
                if not repaired.is_set():
                    raise failure
                await super().aclose(**options)
        context = Context('request')
        async def invoke(context):
            return 'completed'
        turn = engine.start('owner', 'session', invoke, context=context)
        with pytest.raises(CleanupPendingError):
            await engine.wait_turn('owner', turn)
        assert reported == [failure] and not turn.done.is_set()
        repaired.set()
        await engine.aclose()
    asyncio.run(exercise())


def test_cancellation_only_group_is_not_reported_as_execution_error():
    reported = []
    engine = ChatLifecycle(on_error=reported.append)
    engine.report_error(BaseExceptionGroup('cancelled', [asyncio.CancelledError()]))
    assert reported == []


def test_derived_base_exception_group_retains_ordinary_failure_diagnostic():
    class Group(BaseExceptionGroup):
        def derive(self, errors):
            return Group(self.message, errors)
    reported = []
    engine = ChatLifecycle(on_error=reported.append)
    failure = Group('failure', [ValueError('ordinary failure'), asyncio.CancelledError()])
    engine.report_error(failure)
    assert reported == [failure]


@pytest.mark.parametrize('enabled', [False, True])
def test_exception_group_classification_failure_cannot_interrupt_execution(enabled):
    class Group(BaseExceptionGroup):
        def subgroup(self, *args):
            raise ValueError('diagnostic classification failed')
    reported = []
    engine = ChatLifecycle(on_error=reported.append if enabled else None)
    engine.report_error(Group('failure', [RuntimeError('original error'), asyncio.CancelledError()]))
    assert reported == []


def test_result_is_retained_and_owner_scoped():
    async def exercise():
        engine = registry()
        result = {'usage': 7}
        async def invoke(context):
            return result
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        assert await engine.wait('owner', 'session') is turn
        assert turn.state == 'completed' and turn.result is result
        assert turn.done.is_set() and not turn.context.retained_tasks
        assert engine.heartbeat('other', 'session') == 'unknown'
        assert await engine.cancel('other', 'session') == 'unknown'
        assert await engine.cancel('owner', 'session') == 'completed'
        assert engine.heartbeat('owner', 'session') == 'completed'
        with pytest.raises(ValueError, match='already exists'):
            engine.start('owner', 'session', invoke, context=OperationContext('again'))
        await engine.aclose()
    asyncio.run(exercise())


def test_heartbeat_renews_only_lease_and_expiry_drains_operation():
    async def exercise():
        engine = registry()
        entered, drained = asyncio.Event(), asyncio.Event()
        context = OperationContext('request', time.monotonic() + 3)
        deadline = context.deadline
        async def invoke(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        turn = engine.start('owner', 'session', invoke, context=context)
        await entered.wait()
        await asyncio.sleep(.025)
        assert engine.heartbeat('owner', 'session') == 'running'
        await asyncio.sleep(.025)
        assert not turn.done.is_set()
        assert context.deadline == deadline
        await asyncio.wait_for(turn.done.wait(), .2)
        assert turn.state == 'cancelled' and drained.is_set()
        assert not context.retained_tasks
        await engine.aclose()
    asyncio.run(exercise())


def test_actual_deadline_remains_effective_after_heartbeat():
    async def exercise():
        engine = registry()
        async def invoke(context):
            await asyncio.Event().wait()
        engine.start('owner', 'session', invoke,
            context=OperationContext('request', time.monotonic() + .01))
        engine.heartbeat('owner', 'session')
        turn = await engine.wait('owner', 'session')
        assert turn.state == 'failed'
        assert isinstance(turn.error, OperationalError) and turn.error.kind == 'deadline'
        await engine.aclose()
    asyncio.run(exercise())


def test_late_heartbeat_cannot_revive_expired_lease_before_watchdog_runs():
    async def exercise():
        engine = registry()
        async def invoke(context):
            await asyncio.Event().wait()
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        turn._heartbeat -= engine.expiry
        assert engine.heartbeat('owner', 'session') == 'cleanup_pending'
        assert (await engine.wait('owner', 'session')).state == 'cancelled'
        assert engine.heartbeat('owner', 'session') == 'cancelled'
        await engine.aclose()
    asyncio.run(exercise())


def test_resistant_operation_retains_receipt_and_blocks_admission():
    async def exercise():
        engine = registry()
        entered, release = asyncio.Event(), asyncio.Event()
        async def invoke(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return 'late result'
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        with pytest.raises(CleanupPendingError) as pending:
            await engine.cancel('owner', 'session')
        assert turn.state == 'cleanup_pending' and not turn.done.is_set()
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'next', invoke, context=OperationContext('next'))
        with pytest.raises(CleanupPendingError):
            await engine.aclose()
        release.set()
        await pending.value.retry()
        assert turn.state == 'cancelled' and turn.result is None
        assert not turn.context.retained_tasks
        await engine.aclose()
    asyncio.run(exercise())


def test_concurrent_cancellation_retries_dispose_once():
    async def exercise():
        engine = registry()
        calls = []
        ready, release = asyncio.Event(), asyncio.Event()
        async def retry():
            calls.append('retry')
            ready.set()
            await release.wait()
        async def invoke(context):
            raise CleanupPendingError(('resource',), retry)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await ready.wait()
        first = asyncio.create_task(engine.cancel('owner', 'session'))
        second = asyncio.create_task(engine.disconnect('owner', 'session'))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == ['cancelled', 'cancelled']
        assert calls == ['retry'] and turn.done.is_set()
        await engine.aclose()
    asyncio.run(exercise())


def test_cancel_before_execution_and_closed_admission():
    async def exercise():
        engine = registry()
        calls = []
        async def invoke(context):
            calls.append(context)
        engine.start('owner', 'session', invoke, context=OperationContext('request'))
        assert await engine.cancel('owner', 'session') == 'cancelled'
        assert calls == []
        assert (await engine.wait('owner', 'session')).state == 'cancelled'
        await engine.aclose()
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'next', invoke, context=OperationContext('request'))
    asyncio.run(exercise())


def test_slow_cleanup_task_remains_owned_across_retry_and_new_receipt():
    async def exercise():
        engine = registry()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def finish():
            calls.append('finish')
        async def retry():
            calls.append('retry')
            entered.set()
            await release.wait()
            raise CleanupPendingError(('next',), finish)
        async def invoke(context):
            raise CleanupPendingError(('first',), retry)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        with pytest.raises(CleanupPendingError) as receipt:
            await engine.wait('owner', 'session')
        assert not turn._cleanup_task.done()
        with pytest.raises(CleanupPendingError):
            await receipt.value.retry()
        assert calls == ['retry']
        release.set()
        with pytest.raises(CleanupPendingError):
            await receipt.value.retry()
        await receipt.value.retry()
        assert calls == ['retry', 'finish'] and turn.state == 'failed'
        await engine.aclose()
    asyncio.run(exercise())


def test_waiter_cancellation_does_not_cancel_shared_operation():
    async def exercise():
        engine = registry()
        entered, release = asyncio.Event(), asyncio.Event()
        async def invoke(context):
            entered.set()
            await release.wait()
            return 'answer'
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        waiter = asyncio.create_task(engine.wait('owner', 'session'))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not turn.task.done()
        release.set()
        assert (await engine.wait('owner', 'session')).result == 'answer'
        await engine.aclose()
    asyncio.run(exercise())


def test_execution_wrapper_itself_resists_cancellation_until_released(monkeypatch):
    async def exercise():
        engine = registry()
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(turn, invoke):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        monkeypatch.setattr(engine, '_execute', execute)
        turn = engine.start('owner', 'session', lambda _: None, context=OperationContext('request'))
        await entered.wait()
        with pytest.raises(CleanupPendingError) as receipt:
            await engine.cancel('owner', 'session')
        assert turn.state == 'cleanup_pending'
        release.set()
        await receipt.value.retry()
        assert turn.state == 'cancelled'
        await engine.aclose()
    asyncio.run(exercise())


def test_wait_observes_cancelled_execution_task_without_cancelling_waiter(monkeypatch):
    async def exercise():
        engine = registry()
        entered = asyncio.Event()
        async def execute(turn, invoke):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(engine, '_execute', execute)
        turn = engine.start('owner', 'session', lambda _: None, context=OperationContext('request'))
        await entered.wait()
        waiter = asyncio.create_task(engine.wait('owner', 'session'))
        await asyncio.sleep(0)
        await engine.cancel('owner', 'session')
        assert await waiter is turn and turn.state == 'cancelled'
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('failure_at', [1, 2, 3])
@pytest.mark.parametrize('progress_enabled', [False, True])
def test_scheduler_failure_keeps_allocated_work_recoverable(monkeypatch, failure_at, progress_enabled):
    import inspect
    from types import SimpleNamespace
    from api_agent.api import chat_lifecycle
    from api_agent.providers.progress import progress_sink
    progress = []
    async def exercise():
        engine = ChatLifecycle()
        native = asyncio.create_task
        target = (engine._execute.__code__, engine._watch.__code__, engine._settle.__code__)[failure_at - 1]
        rejected = []
        callback_finished = asyncio.Event()
        execution_done = engine._execution_done
        def observe_done(turn):
            try:
                execution_done(turn)
            finally:
                callback_finished.set()
        def schedule(coroutine):
            if not rejected and coroutine.cr_code is target:
                rejected.append(coroutine)
                raise RuntimeError('scheduler unavailable')
            return native(coroutine)
        async def invoke(context):
            return 'answer'
        monkeypatch.setattr(engine, '_execution_done', observe_done)
        monkeypatch.setattr(chat_lifecycle, 'asyncio',
            SimpleNamespace(**(vars(asyncio) | {'create_task': schedule})))
        if failure_at == 1:
            with pytest.raises(RuntimeError, match='scheduler'):
                engine.start('owner', 'session', invoke, context=OperationContext('request'))
            assert not engine._turns
        elif failure_at == 2:
            with pytest.raises(CleanupPendingError) as receipt:
                engine.start('owner', 'session', invoke, context=OperationContext('request'))
            assert len(engine._turns) == 1
            turn = next(iter(engine._turns.values()))
            assert turn.state == 'cleanup_pending' and not turn.done.is_set()
            await receipt.value.retry()
            assert turn.state == 'cancelled' and turn.done.is_set()
            assert not turn.context.retained_tasks
        else:
            turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
            await turn.task
            await asyncio.wait_for(callback_finished.wait(), 1)
            assert turn.state == 'cleanup_pending' and not turn.done.is_set()
            assert await engine.wait('owner', 'session') is turn
            assert turn.state == 'completed' and turn.result == 'answer'
            assert turn.done.is_set() and not turn.context.retained_tasks
        assert len(rejected) == 1 and rejected[0].cr_code is target
        assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
        await engine.aclose()
        assert engine.closed
    with progress_sink(progress.append if progress_enabled else None):
        asyncio.run(exercise())
    assert bool(progress) == (progress_enabled and failure_at != 1)


def test_capacities_and_terminal_retention_are_bounded(monkeypatch):
    async def exercise():
        engine = registry(max_active=1, max_terminal=1, terminal_ttl=1)
        gate = asyncio.Event()
        async def invoke(context):
            return await gate.wait()
        engine.start('owner', 'first', invoke, context=OperationContext('first'))
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'second', invoke, context=OperationContext('second'))
        gate.set()
        await engine.wait('owner', 'first')
        engine.start('owner', 'second', invoke, context=OperationContext('second'))
        await engine.wait('owner', 'second')
        assert engine.heartbeat('owner', 'first') == 'unknown'
        monkeypatch.setattr('api_agent.api.chat_lifecycle.time.monotonic', lambda: 1e20)
        assert engine.heartbeat('owner', 'second') == 'unknown'
        assert not engine._turns
        await engine.aclose()
    asyncio.run(exercise())


def test_cancel_does_not_revive_expired_terminal_status():
    async def exercise():
        engine = registry()
        async def invoke(context):
            return 'answer'
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await engine.wait('owner', 'session')
        turn._finished -= engine.terminal_ttl
        assert await engine.cancel('owner', 'session') == 'unknown'
        assert not engine._turns
        await engine.aclose()
    asyncio.run(exercise())


def test_shutdown_signals_cooperative_sibling_before_waiting_for_resistant_resource():
    async def exercise():
        engine = ChatLifecycle(heartbeat_interval=.01, expiry=.3, cleanup_timeout=.15)
        resistant_started, cooperative_started = asyncio.Event(), asyncio.Event()
        resistant_cancelled, cooperative_disposed = asyncio.Event(), asyncio.Event()
        release = asyncio.Event()
        resources = set()
        async def resistant(context):
            resources.add('resistant')
            resistant_started.set()
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    resistant_cancelled.set()
                    await release.wait()
            finally:
                resources.remove('resistant')
        async def cooperative(context):
            resources.add('cooperative')
            cooperative_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                resources.remove('cooperative')
                cooperative_disposed.set()
        first = engine.start('owner', 'first', resistant, context=OperationContext('first'))
        second = engine.start('owner', 'second', cooperative, context=OperationContext('second'))
        await resistant_started.wait()
        await cooperative_started.wait()
        closing = asyncio.create_task(engine.aclose())
        await resistant_cancelled.wait()
        try:
            await asyncio.wait_for(cooperative_disposed.wait(), .04)
            assert resources == {'resistant'} and not closing.done()
        finally:
            release.set()
            await closing
        assert not resources and first.done.is_set() and second.done.is_set()
        assert first.state == second.state == 'cancelled'
    asyncio.run(exercise())


@pytest.mark.parametrize('options', [
    {'expiry': 0}, {'expiry': float('nan')}, {'expiry': True},
    {'max_active': 0}, {'max_terminal': True}, {'heartbeat_interval': 30},
])
def test_invalid_lifetime_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        ChatLifecycle(**options)


@pytest.mark.parametrize('owner,session,context,invoke', [
    ('', 'session', OperationContext('a'), lambda _: None),
    ('owner', '', OperationContext('a'), lambda _: None),
    ('owner', 'session', None, lambda _: None),
    ('owner', 'session', OperationContext('a'), None),
])
def test_invalid_admission_is_inert(owner, session, context, invoke):
    engine = registry()
    with pytest.raises(ValueError, match='Invalid chat'):
        engine.start(owner, session, invoke, context=context)
    assert not engine._turns


@pytest.mark.parametrize('stop', ['cancel', 'disconnect', 'deadline', 'expiry'])
def test_receipt_created_during_invocation_cancellation_blocks_terminal_publication(stop):
    async def exercise():
        engine = registry()
        invoked, disposing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        disposals = []
        async def dispose():
            disposals.append('resource')
            disposing.set()
            await release.wait()
        async def invoke(context):
            invoked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise CleanupPendingError(('resource',), dispose)
        deadline = time.monotonic() + .02 if stop == 'deadline' else None
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request', deadline))
        await invoked.wait()
        control = (engine.cancel if stop == 'cancel' else engine.disconnect if stop == 'disconnect' else engine.wait)
        waiting = asyncio.create_task(control('owner', 'session'))
        await asyncio.wait_for(disposing.wait(), .3)
        with pytest.raises(CleanupPendingError) as pending:
            await waiting
        assert turn.state == 'cleanup_pending' and not turn.done.is_set()
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'next', invoke, context=OperationContext('next'))
        release.set()
        await pending.value.retry()
        assert disposals == ['resource']
        assert turn.state == ('failed' if stop == 'deadline' else 'cancelled')
        assert turn.done.is_set() and not turn.context.retained_tasks
        await engine.aclose()
    asyncio.run(exercise())


def test_grouped_distinct_cleanup_receipts_are_drained_once_each():
    async def exercise():
        engine = registry()
        calls = []
        async def first():
            calls.append('first')
        async def second():
            calls.append('second')
        first_receipt = CleanupPendingError(('first',), first)
        async def invoke(context):
            raise ExceptionGroup('disposal', [first_receipt, first_receipt,
                ExceptionGroup('nested', [CleanupPendingError(('second',), second)])])
        engine.start('owner', 'session', invoke, context=OperationContext('request'))
        turn = await engine.wait('owner', 'session')
        assert turn.state == 'failed' and calls == ['first', 'second']
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('mixed_failure', [False, True])
def test_retry_transfers_grouped_cleanup_duties_before_reusing_parent(mixed_failure):
    async def exercise():
        engine = registry()
        blocked, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def first():
            calls.append('first')
            blocked.set()
            await release.wait()
        async def second():
            calls.append('second')
        children = [CleanupPendingError(('first',), first),
                    CleanupPendingError(('second',), second)]
        async def parent():
            calls.append('parent')
            if calls == ['parent']:
                members = [ExceptionGroup('children', [children[0], children[0], children[1]])]
                if mixed_failure:
                    members.insert(0, RuntimeError('unacknowledged parent disposal'))
                raise ExceptionGroup('transferred disposal', members)
        async def invoke(context):
            raise CleanupPendingError(('parent',), parent)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        with pytest.raises(CleanupPendingError) as pending:
            await engine.wait('owner', 'session')
        retrying = asyncio.create_task(pending.value.retry())
        await asyncio.wait_for(blocked.wait(), .2)
        with pytest.raises(CleanupPendingError):
            await retrying
        assert calls == ['parent', 'first']
        assert turn.state == 'cleanup_pending' and not turn.done.is_set()
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'next', invoke, context=OperationContext('next'))
        release.set()
        await pending.value.retry()
        assert calls == (['parent', 'first', 'second', 'parent'] if mixed_failure
                         else ['parent', 'first', 'second'])
        assert turn.done.is_set() and turn.state == 'failed'
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('include_new', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_grouped_current_duty_remains_pending_without_replaying_retired_children(include_new, nested):
    async def exercise():
        engine = registry()
        calls = []
        ready = False
        entered, release = asyncio.Event(), asyncio.Event()
        async def child():
            calls.append('child')
        child_receipt = CleanupPendingError(('child',), child)
        async def retry():
            calls.append('current')
            if not ready:
                duties = [current_receipt, current_receipt]
                if include_new:
                    duties.extend([child_receipt, child_receipt])
                group = ExceptionGroup('pending', duties)
                raise ExceptionGroup('nested', [group]) if nested else group
            entered.set()
            await release.wait()
        current_receipt = CleanupPendingError(('current',), retry)
        async def invoke(context):
            raise current_receipt
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        for _ in range(3):
            with pytest.raises(CleanupPendingError):
                await engine.wait('owner', 'session')
        assert not turn.done.is_set() and turn.state == 'cleanup_pending'
        assert calls.count('child') == int(include_new)
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'next', invoke, context=OperationContext('next'))
        ready = True
        first = asyncio.create_task(engine.wait('owner', 'session'))
        await entered.wait()
        second = asyncio.create_task(engine.wait('owner', 'session'))
        await asyncio.sleep(0)
        release.set()
        assert await first is turn and await second is turn
        assert calls.count('current') == 4
        assert calls.count('child') == int(include_new)
        assert turn.done.is_set()
        await engine.aclose()
    asyncio.run(exercise())


def test_disposal_progress_task_allocation_failure_retains_retry_ownership(monkeypatch):
    from api_agent.providers.progress import progress_sink
    async def exercise():
        engine = registry()
        loop = asyncio.get_running_loop()
        schedule = loop.create_task
        disposal_scope = False
        calls = []
        def observe(event):
            nonlocal disposal_scope
            if event.name == 'Chat operation disposal' and event.active:
                disposal_scope = True
        def reject_heartbeat(coroutine, *args, **kwargs):
            if disposal_scope and coroutine.__name__ == 'heartbeat':
                coroutine.close()
                raise RuntimeError('timing task unavailable')
            return schedule(coroutine, *args, **kwargs)
        async def dispose():
            calls.append('disposed')
        async def invoke(context):
            raise CleanupPendingError(('resource',), dispose)
        monkeypatch.setattr(loop, 'create_task', reject_heartbeat)
        with progress_sink(observe):
            turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
            with pytest.raises(CleanupPendingError) as pending:
                await engine.wait('owner', 'session')
            assert turn.state == 'cleanup_pending' and not turn.done.is_set()
            assert calls == []
            with pytest.raises(ValueError, match='unavailable'):
                engine.start('owner', 'next', invoke, context=OperationContext('next'))
            monkeypatch.setattr(loop, 'create_task', schedule)
            await pending.value.retry()
            assert turn.done.is_set() and calls == ['disposed']
            await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['entry', 'exit'])
@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_disposal_scope_interruption_retains_retry_boundary(monkeypatch, phase, failure):
    from contextlib import asynccontextmanager
    from api_agent.api import chat_lifecycle
    async def exercise():
        engine = registry()
        original = chat_lifecycle.report_timeout
        enabled = True
        calls = []
        @asynccontextmanager
        async def interrupted_scope(name, timeout):
            if name == 'Chat operation disposal' and enabled and phase == 'entry':
                raise failure()
            async with original(name, timeout) as budget:
                yield budget
            if name == 'Chat operation disposal' and enabled and phase == 'exit':
                raise failure()
        async def dispose():
            calls.append('disposed')
        async def invoke(context):
            raise CleanupPendingError(('resource',), dispose)
        monkeypatch.setattr(chat_lifecycle, 'report_timeout', interrupted_scope)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        with pytest.raises(CleanupPendingError) as pending:
            await engine.wait('owner', 'session')
        assert turn.state == 'cleanup_pending' and not turn.done.is_set()
        enabled = False
        await pending.value.retry()
        assert turn.done.is_set() and calls == ['disposed']
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('rejection', ['scope', 'allocation'])
def test_operation_preparation_failure_closes_unstarted_invocation(monkeypatch, recwarn, rejection):
    from contextlib import asynccontextmanager
    from api_agent.api import chat_lifecycle
    async def exercise():
        engine = registry()
        original = chat_lifecycle.report_timeout
        calls = []
        @asynccontextmanager
        async def rejected_scope(name, timeout):
            if name == 'Chat operation' and rejection == 'scope':
                raise RuntimeError('scope unavailable')
            async with original(name, timeout) as budget:
                yield budget
        async def invoke(context):
            calls.append('invoke')
        monkeypatch.setattr(chat_lifecycle, 'report_timeout', rejected_scope)
        loop = asyncio.get_running_loop()
        schedule = loop.create_task
        def reject_invocation(coroutine, *args, **kwargs):
            if coroutine.__name__ == '_invoke':
                raise RuntimeError('invocation task unavailable')
            return schedule(coroutine, *args, **kwargs)
        if rejection == 'allocation':
            monkeypatch.setattr(loop, 'create_task', reject_invocation)
        engine.start('owner', 'session', invoke, context=OperationContext('request'))
        turn = await engine.wait('owner', 'session')
        assert turn.state == 'failed' and calls == []
        await engine.aclose()
    asyncio.run(exercise())
    import gc
    gc.collect()
    assert not [warning for warning in recwarn if 'was never awaited' in str(warning.message)]


@pytest.mark.parametrize('phase', ['entry', 'exit', 'cancel'])
@pytest.mark.parametrize('resistant', [False, True])
def test_watchdog_failure_fail_stops_operation_and_retains_cleanup(monkeypatch, phase, resistant):
    from contextlib import asynccontextmanager
    from api_agent.api import chat_lifecycle
    async def exercise():
        engine = registry()
        original = chat_lifecycle.report_timeout
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def failed_scope(name, timeout):
            if name == 'Chat heartbeat lease':
                await entered.wait()
                if phase == 'entry':
                    raise RuntimeError('lease scope entry failed')
                if phase == 'cancel':
                    raise asyncio.CancelledError()
                async with original(name, timeout) as budget:
                    yield budget
                raise RuntimeError('lease scope exit failed')
            async with original(name, timeout) as budget:
                yield budget
        async def invoke(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                if resistant:
                    await release.wait()
        monkeypatch.setattr(chat_lifecycle, 'report_timeout', failed_scope)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        if phase == 'exit':
            engine.heartbeat('owner', 'session')
        await asyncio.wait_for(cancelled.wait(), .2)
        try:
            if resistant:
                with pytest.raises(CleanupPendingError):
                    await engine.wait('owner', 'session')
                assert not turn.done.is_set() and turn.state == 'cleanup_pending'
                with pytest.raises(ValueError, match='unavailable'):
                    engine.start('owner', 'next', invoke, context=OperationContext('next'))
            else:
                await engine.wait('owner', 'session')
                assert turn.state == 'cancelled' and turn.done.is_set()
            assert turn._monitor.done()
            assert isinstance(turn._watch_error, RuntimeError) if phase != 'cancel' else turn._monitor.cancelled()
        finally:
            release.set()
            await engine.aclose()
    asyncio.run(exercise())


def test_real_progress_heartbeat_allocation_fault_cannot_disable_lease(monkeypatch):
    from api_agent.providers.progress import progress_sink
    async def exercise():
        engine = registry()
        loop = asyncio.get_running_loop()
        schedule = loop.create_task
        entered = asyncio.Event()
        lease_scope = False
        def observe(event):
            nonlocal lease_scope
            if event.name == 'Chat heartbeat lease' and event.active:
                lease_scope = True
        def reject_heartbeat(coroutine, *args, **kwargs):
            nonlocal lease_scope
            if lease_scope and coroutine.__name__ == 'heartbeat':
                lease_scope = False
                coroutine.close()
                raise RuntimeError('lease timing allocation failed')
            return schedule(coroutine, *args, **kwargs)
        async def invoke(context):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(loop, 'create_task', reject_heartbeat)
        with progress_sink(observe):
            turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
            await asyncio.wait_for(turn.done.wait(), .2)
            assert turn.state == 'cancelled' and not turn.context.retained_tasks
            assert isinstance(turn._watch_error, RuntimeError)
            await engine.aclose()
    asyncio.run(exercise())


def test_intentional_watchdog_stop_preserves_successful_result():
    async def exercise():
        engine = registry()
        started, release = asyncio.Event(), asyncio.Event()
        async def invoke(context):
            started.set()
            await release.wait()
            return {'usage': 3}
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await started.wait()
        release.set()
        await engine.wait('owner', 'session')
        assert turn.state == 'completed' and turn.result == {'usage': 3}
        assert turn._monitor_stopping and turn._monitor.cancelled()
        assert turn._watch_error is None
        await engine.aclose()
    asyncio.run(exercise())


def test_watchdog_fail_stop_survives_settlement_scheduler_rejection(monkeypatch):
    async def exercise():
        engine = registry()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        schedule = loop.create_task
        rejected = []
        def reject_first_settlement(coroutine, *args, **kwargs):
            if coroutine.__name__ == '_settle' and not rejected:
                rejected.append('settlement')
                raise RuntimeError('settlement allocator unavailable')
            return schedule(coroutine, *args, **kwargs)
        async def watch(turn):
            await started.wait()
            raise RuntimeError('watchdog unavailable')
        async def invoke(context):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(engine, '_watch', watch)
        monkeypatch.setattr(loop, 'create_task', reject_first_settlement)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await asyncio.wait_for(turn.done.wait(), .2)
        assert rejected == ['settlement']
        assert turn.state == 'cancelled' and not turn.context.retained_tasks
        assert isinstance(turn._watch_error, RuntimeError)
        await engine.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('pruning', ['ttl', 'capacity'])
def test_captured_response_cleanup_preserves_reused_session(pruning):
    async def exercise():
        engine = registry(max_terminal=1)
        entered, drained = asyncio.Event(), asyncio.Event()
        async def completed(context):
            return 'original'
        async def blocked(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        original = engine.start('owner', 'session', completed, context=OperationContext('original'))
        await engine.wait('owner', 'session')
        if pruning == 'ttl':
            original._finished -= engine.terminal_ttl
        else:
            engine.start('owner', 'another', completed, context=OperationContext('another'))
            await engine.wait('owner', 'another')
        replacement = engine.start('owner', 'session', blocked, context=OperationContext('replacement'))
        await entered.wait()

        observed = await engine.wait_turn('owner', original)
        state = await engine.disconnect_turn('owner', original)

        assert not drained.is_set()
        assert replacement.state == 'running' and not replacement.done.is_set()
        assert state == 'completed' and observed is original and observed.result == 'original'
        assert engine.heartbeat('owner', 'session') == 'running'
        await engine.aclose()
        assert drained.is_set() and replacement.state == 'cancelled'
    asyncio.run(exercise())


@pytest.mark.parametrize('invalid', ['owner', 'lifecycle', 'handle'])
@pytest.mark.parametrize('operation', ['disconnect_turn', 'wait_turn'])
def test_captured_response_cleanup_enforces_backend_owner(invalid, operation):
    async def exercise():
        engine, other = registry(), registry()
        entered, drained = asyncio.Event(), asyncio.Event()
        async def invoke(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()
        target = other if invalid == 'lifecycle' else engine
        owner = 'other' if invalid == 'owner' else 'owner'
        handle = object() if invalid == 'handle' else turn

        with pytest.raises(ValueError, match='ownership mismatch'):
            await getattr(target, operation)(owner, handle)

        assert turn.state == 'running' and not drained.is_set()
        await engine.aclose()
        await other.aclose()
        assert drained.is_set()
    asyncio.run(exercise())


def test_captured_response_cleanup_pending_receipts_and_overlap_retain_resource():
    async def exercise():
        engine = registry()
        entered, release, disposed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
            disposed.set()
        async def invoke(context):
            raise CleanupPendingError(('resource',), dispose)
        turn = engine.start('owner', 'session', invoke, context=OperationContext('request'))
        await entered.wait()

        with pytest.raises(CleanupPendingError) as pending:
            await engine.disconnect_turn('owner', turn)

        assert turn.state == 'cleanup_pending' and not turn.done.is_set()
        assert not disposed.is_set()
        with pytest.raises(ValueError, match='unavailable'):
            engine.start('owner', 'session', invoke, context=OperationContext('replacement'))
        retry = asyncio.create_task(pending.value.retry())
        overlap = asyncio.create_task(engine.disconnect_turn('owner', turn))
        await asyncio.sleep(0)
        release.set()
        await retry
        assert await overlap == 'cancelled'
        assert disposed.is_set() and turn.done.is_set()
        assert not turn.context.retained_tasks
        await engine.aclose()
    asyncio.run(exercise())
