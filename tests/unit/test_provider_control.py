import asyncio
import time
from dataclasses import FrozenInstanceError
import pytest
from api_agent.providers.control import CleanupPendingError, OperationalError, OperationContext


def test_safe_errors():
    for kind in ('authentication', 'authorization', 'unavailable', 'rate_limited', 'unsupported', 'invalid_request', 'deadline', 'unknown'):
        error = OperationalError(kind, retry_after=1)
        assert str(error) == kind and error.retry_after == 1
    assert OperationalError('unknown').execution == 'possibly_executed'
    for args in [('secret',), ('deadline', 'bad'), ('deadline', 'completed', -1), ('deadline', 'completed', float('nan'))]:
        with pytest.raises(ValueError):
            OperationalError(*args)
    error = CleanupPendingError((object(),), lambda: None)
    assert len(error.owners) == 1 and callable(error.retry) and str(error) == 'cleanup pending'


def test_context_validation():
    for args in [('',), (None,), ('id', float('inf'))]:
        with pytest.raises(ValueError):
            OperationContext(*args)
    with pytest.raises(FrozenInstanceError):
        OperationContext('id').deadline = 1


def test_budget_and_cancellation():
    async def scenario():
        async def value():
            return 3
        assert OperationContext('id').remaining() is None
        assert await OperationContext('id').run(value()) == 3
        assert await OperationContext('id', time.monotonic() + 1).run(value()) == 3
        expired = value()
        with pytest.raises(OperationalError) as caught:
            await OperationContext('id', 0).run(expired)
        assert caught.value.kind == 'deadline' and caught.value.execution == 'not_started'
        assert expired.cr_frame is None
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(OperationalError):
            await OperationContext('id', 0).run(future)
        future.cancel()
        started = asyncio.Event()
        async def block():
            started.set()
            await asyncio.Future()
        task = asyncio.create_task(block())
        await started.wait()
        with pytest.raises(OperationalError) as caught:
            await OperationContext('id', time.monotonic() + .001).run(task)
        assert caught.value.execution == 'possibly_executed'
        await asyncio.sleep(0)
        assert task.cancelled()
        async def fail():
            raise TimeoutError('upstream')
        with pytest.raises(TimeoutError):
            await OperationContext('id', time.monotonic() + 1).run(fail())
        child = asyncio.create_task(block())
        outer = asyncio.create_task(OperationContext('id', time.monotonic() + 1).run(child))
        await asyncio.sleep(0)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)
        assert child.cancelled()
    asyncio.run(scenario())


def test_resistant_child_does_not_extend_budget():
    async def scenario():
        cancelled = asyncio.Event()
        release = asyncio.Event()
        async def resistant():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        child = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        with pytest.raises(OperationalError):
            await OperationContext('id', time.monotonic() + .001).run(child)
        await cancelled.wait()
        assert not child.done()
        release.set()
        await child
    asyncio.run(scenario())


def test_retained_cleanup_and_safe_numeric_validation():
    for value in (True, '1', object()):
        with pytest.raises(ValueError):
            OperationContext('id', value)
        with pytest.raises(ValueError):
            OperationalError('deadline', retry_after=value)
    async def scenario():
        context = OperationContext('id')
        await context.aclose()
        class Empty:
            def __await__(self):
                return iter(())
        with pytest.raises(OperationalError):
            await OperationContext('expired', 0).run(Empty())
        release = asyncio.Event()
        started = asyncio.Event()
        async def resistant():
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        child = asyncio.create_task(resistant())
        await started.wait()
        context = OperationContext('id', time.monotonic() + .001)
        with pytest.raises(OperationalError):
            await context.run(child)
        assert context.retained_tasks == (child,)
        with pytest.raises(CleanupPendingError) as caught:
            await context.aclose(timeout=0)
        assert caught.value.owners == (context,)
        release.set()
        await caught.value.retry()
        await asyncio.sleep(0)
        assert not context.retained_tasks
        future = asyncio.get_running_loop().create_future()
        context = OperationContext('expired', 0)
        with pytest.raises(OperationalError):
            await context.run(future)
        assert future.cancelled()
        await context.aclose()
    asyncio.run(scenario())


def test_canonical_classifications_and_cleanup_budgets():
    class Pretend:
        def __hash__(self):
            return hash('deadline')
        def __eq__(self, other):
            return True
    for value in ([], Pretend()):
        with pytest.raises(ValueError, match='invalid operational classification'):
            OperationalError(value)
        with pytest.raises(ValueError, match='invalid operational classification'):
            OperationalError('deadline', value)
    async def scenario():
        for value in (None, True, -1, float('inf'), float('nan'), '1'):
            with pytest.raises(ValueError, match='invalid cleanup budget'):
                await OperationContext('id').aclose(value)
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(OperationalError) as caught:
            await OperationContext('id', 0).run(future)
        assert caught.value.execution == 'possibly_executed'
        await asyncio.sleep(0)
        started = asyncio.Event()
        async def child():
            started.set()
            await asyncio.Future()
        task = asyncio.create_task(child())
        await started.wait()
        with pytest.raises(OperationalError) as caught:
            await OperationContext('id', 0).run(task)
        assert caught.value.execution == 'possibly_executed'
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_cleanup_does_not_interrupt_awaited_finally():
    async def scenario():
        release = asyncio.Event()
        cleaning = asyncio.Event()
        closed = asyncio.Event()
        async def child():
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                closed.set()
        context = OperationContext('id', time.monotonic() + .001)
        task = asyncio.create_task(child())
        with pytest.raises(OperationalError):
            await context.run(task)
        await cleaning.wait()
        for _ in range(2):
            with pytest.raises(CleanupPendingError) as caught:
                await context.aclose(timeout=0)
            assert context.retained_tasks == (task,)
            assert task.cancelling() == 1
            assert not closed.is_set()
        release.set()
        await caught.value.retry()
        assert closed.is_set() and not context.retained_tasks
        # Respect a cancellation already requested by an outside owner, too.
        release.clear()
        cleaning.clear()
        closed.clear()
        task = asyncio.create_task(child())
        await asyncio.sleep(0)
        task.cancel()
        await cleaning.wait()
        context = OperationContext('id', 0)
        with pytest.raises(OperationalError):
            await context.run(task)
        with pytest.raises(CleanupPendingError):
            await context.aclose(timeout=0)
        assert task.cancelling() == 1
        release.set()
        await context.aclose()
        assert closed.is_set() and not context.retained_tasks
        # Futures have no cancelling counter: context remembers its request.
        class CountingFuture(asyncio.Future):
            def __init__(self):
                super().__init__()
                self.requests = 0
            def cancel(self, *args, **kwargs):
                self.requests += 1
                return False
        future = CountingFuture()
        context = OperationContext('id', 0)
        with pytest.raises(OperationalError):
            await context.run(future)
        with pytest.raises(CleanupPendingError):
            await context.aclose(timeout=0)
        assert future.requests == 1
        future.set_result(None)
        await context.aclose()
        assert not context.retained_tasks
    asyncio.run(scenario())
