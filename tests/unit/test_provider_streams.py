import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api_agent.providers.control import OperationalError, OperationContext, CleanupPendingError
from api_agent.providers.contracts import ContractError, SessionClosedError, StreamEvent
from api_agent.providers.llm import TextContent, LLMResult, Message
from api_agent.providers.sessions import LLMSessionOwner
from test_provider_sessions import browser, credentials, Auth, run


def owner(**kwargs):
    browser_owner, driver, *_ = browser()
    auth = Auth()
    result = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)),
                             lambda: browser_owner, user_index=0, credentials=credentials(), **kwargs)
    return result, auth, driver


def test_read_cancellation_during_close_retains_awaited_generator_disposal():
    async def scenario():
        started, cleaning, release, disposed = [asyncio.Event() for _ in range(4)]
        async def events(**kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
                yield StreamEvent(TextContent('unused'))
            finally:
                cleaning.set()
                await release.wait()
                disposed.set()
        llm = LLMSessionOwner(SimpleNamespace(), timeout=.01)
        cm = llm.stream(events)
        stream = await cm.__aenter__()
        read = asyncio.create_task(anext(stream))
        await started.wait()
        close = asyncio.create_task(stream.aclose())
        try:
            await cleaning.wait()
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
            with pytest.raises(CleanupPendingError) as pending:
                await close
            assert stream._next.cancelling() == 1
            assert not disposed.is_set() and stream in llm._streams
            assert not stream._next.done()
            release.set()
            await pending.value.retry()
            assert disposed.is_set() and stream not in llm._streams
        finally:
            release.set()
            await asyncio.gather(read, close, return_exceptions=True)
            await cm.__aexit__(None, None, None)
            await llm.aclose()
    run(scenario())


def test_stream_exhaustion_early_close_async_factory_and_terminal_once():
    async def scenario():
        llm, auth, driver = owner(finalizer=AsyncMock())
        closed = []
        async def events(**kwargs):
            assert kwargs['auth'] is not None
            try:
                yield StreamEvent(TextContent('text'))
                yield StreamEvent(LLMResult(Message('assistant', (TextContent('done'),))), 'completed')
            finally:
                closed.append(True)
        async with llm.stream(events) as stream:
            assert stream.__aiter__() is stream
            assert len([event async for event in stream]) == 2
            assert not llm._streams
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        async def factory(**kwargs):
            return events(**kwargs)
        async with llm.stream(factory) as stream:
            await anext(stream)
        assert closed == [True, True]
        await llm.aclose()
        await llm.aclose()
        llm._finalizer.assert_awaited_once()
        assert auth.closed
        driver.stop.assert_awaited_once()
    run(scenario())


def test_stream_shapes_errors_and_failed_close_retry():
    async def scenario():
        # This tests an explicit close error, not expiry; a 10 ms budget can
        # accidentally time out ordinary teardown under parallel coverage load.
        llm, *_ = owner()
        with pytest.raises(ContractError):
            async with llm.stream(lambda **kwargs: object()):
                pass
        class Stream:
            def __init__(self):
                self.count = 0
                self.aclose = AsyncMock(side_effect=[RuntimeError('private'), None])
            async def __anext__(self):
                self.count += 1
                if self.count == 1:
                    return StreamEvent(LLMResult(Message('assistant', (TextContent('done'),))), 'completed')
                return StreamEvent(LLMResult(Message('assistant', (TextContent('done'),))), 'completed')
        raw = Stream()
        cm = llm.stream(lambda **kwargs: raw)
        stream = await cm.__aenter__()
        await anext(stream)
        with pytest.raises(ContractError, match='terminal'):
            await anext(stream)
        with pytest.raises(CleanupPendingError):
            await cm.__aexit__(None, None, None)
        assert llm._streams and not llm._finalizing
        await stream.aclose()
        assert not llm._streams
        assert raw.aclose.await_count == 2
        await llm.aclose()
        llm, *_ = owner()
        async def bad(**kwargs):
            yield StreamEvent(TextContent('text'))
            raise RuntimeError('private')
        with pytest.raises(OperationalError) as error:
            async with llm.stream(bad) as stream:
                await anext(stream)
                await anext(stream)
        assert error.value.kind == 'unknown' and error.value.execution == 'possibly_executed'
        await llm.aclose()
    run(scenario())


def test_resistant_iteration_and_close_retain_dependencies():
    async def scenario():
        llm, auth, driver = owner(timeout=.01, finalizer=AsyncMock())
        entered, release, close_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Stream:
            async def __anext__(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                assert not auth.closed
                return StreamEvent(TextContent('text'))
            async def aclose(self):
                await close_release.wait()
                assert not auth.closed
        cm = llm.stream(lambda **kwargs: Stream())
        stream = await cm.__aenter__()
        next_task = asyncio.create_task(anext(stream))
        await entered.wait()
        with pytest.raises(ContractError, match='Concurrent'):
            await anext(stream)
        next_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_task
        with pytest.raises(ContractError, match="Concurrent"):
            await anext(stream)
        for _ in range(2):
            with pytest.raises(CleanupPendingError):
                await llm.aclose()
            assert not auth.closed and not driver.stop.called
        release.set()
        await stream._next
        with pytest.raises(CleanupPendingError):
            await stream.aclose()
        closing = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        close_release.set()
        await stream.aclose()
        await cm.__aexit__(None, None, None)
        await llm.aclose()
        assert auth.closed
        llm._finalizer.assert_awaited_once()
    run(scenario())


def test_deadline_budget_and_approved_failures():
    async def scenario():
        llm, auth, driver = owner(timeout=.01)
        op = AsyncMock()
        with pytest.raises(OperationalError) as error:
            await llm.call(op, context=OperationContext('expired', time.monotonic()-1))
        assert error.value.execution == 'not_started'
        op.assert_not_called()
        entered, release = asyncio.Event(), asyncio.Event()
        async def operation(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            assert not auth.closed
        with pytest.raises(OperationalError, match='deadline'):
            await llm.call(operation, context=OperationContext('work', time.monotonic()+.02))
        with pytest.raises(CleanupPendingError):
            await llm.aclose()
        assert not auth.closed and not driver.stop.called
        release.set()
        await asyncio.sleep(0)
        await llm.aclose()
        llm, *_ = owner()
        safe = OperationalError('rate_limited', retry_after=1)
        with pytest.raises(OperationalError) as error:
            await llm.call(AsyncMock(side_effect=safe))
        assert error.value is safe
        await llm.aclose()
        llm, *_ = owner()
        async def events(**kwargs):
            await asyncio.sleep(.02)
            yield StreamEvent(TextContent('text'))
        with pytest.raises(OperationalError, match='deadline'):
            async with llm.stream(events, context=OperationContext('stream', time.monotonic()+.01)) as stream:
                await anext(stream)
        await llm.aclose()
    run(scenario())


def test_budget_expiry_during_getter_cancellation_and_boundary_errors():
    async def scenario():
        llm, *_ = owner()
        await llm.call(AsyncMock())
        with pytest.raises(OperationalError):
            await llm._budget(asyncio.sleep(0), OperationContext('expired', time.monotonic()-1))
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(OperationalError):
            await llm._budget(future, OperationContext('expired', time.monotonic()-1))
        assert future.cancelled()
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(llm.call(blocked, context=OperationContext('cancel')))
        await entered.wait()
        task.cancel('user cancellation')
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert error.value.args == ('user cancellation',)
        with pytest.raises(OperationalError) as error:
            await llm.call(AsyncMock(side_effect=RuntimeError('private')))
        assert error.value.kind == 'unknown'
        await llm.aclose()
        llm, *_ = owner()
        safe = OperationalError('authorization')
        llm.provider.authenticate.side_effect = safe
        with pytest.raises(OperationalError) as error:
            await llm.call(AsyncMock())
        assert error.value is safe
        await llm.aclose()
        llm, *_ = owner()
        async def no_terminal(**kwargs):
            if False:
                yield
        with pytest.raises(ContractError, match='without terminal'):
            async with llm.stream(no_terminal) as stream:
                await anext(stream)
        await llm.aclose()
        llm, *_ = owner()
        async def immediate(**kwargs):
            yield StreamEvent(TextContent('text'))
        async with llm.stream(immediate, context=OperationContext('ready')) as stream:
            assert not (await anext(stream)).final
        await llm.aclose()
    run(scenario())


def test_browser_and_credential_safe_errors_and_neutral_setup():
    from api_agent.providers.sessions import ScopedRoleCredentials
    async def scenario():
        safe = OperationalError('authentication')
        reader = ScopedRoleCredentials(['role'], 0, AsyncMock(side_effect=safe))
        with pytest.raises(OperationalError) as error:
            await reader('role', 0, 'username')
        assert error.value is safe
        browser_owner, *_ = browser()
        browser_owner._factory.side_effect = safe
        with pytest.raises(OperationalError) as error:
            await browser_owner.start()
        assert error.value is safe
        browser_owner, *_ = browser()
        browser_owner._prepare = AsyncMock(side_effect=RuntimeError('private'))
        with pytest.raises(OperationalError):
            await browser_owner.prepare(None, tui_mode='unit', user_index=None, credentials=None)
    run(scenario())


def test_completed_budget_error_and_unexpected_browser_start_are_safe():
    async def scenario():
        llm, *_ = owner()
        safe = OperationalError('unavailable')
        with pytest.raises(OperationalError) as error:
            await llm.call(AsyncMock(side_effect=safe), context=OperationContext('safe'))
        assert error.value is safe
        await llm.aclose()
        browser_owner, *_ = browser()
        browser_owner._start = AsyncMock(side_effect=RuntimeError('private'))
        with pytest.raises(ContractError, match='Browser setup failed'):
            await browser_owner.start()
    run(scenario())


def test_malformed_expired_and_late_stream_factory_keeps_close_handle():
    async def scenario():
        llm, *_ = owner(timeout=.01)
        async def malformed(**kwargs):
            yield object()
        with pytest.raises(ContractError, match='Invalid stream event'):
            async with llm.stream(malformed) as stream:
                await anext(stream)
        raw = SimpleNamespace(__anext__=AsyncMock(), aclose=AsyncMock())
        async with llm.stream(lambda **kwargs: raw) as stream:
            stream._context = OperationContext('expired', time.monotonic()-1)
            with pytest.raises(OperationalError, match='deadline'):
                await anext(stream)
            raw.__anext__.assert_not_called()
        release = asyncio.Event()
        async def factory(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return raw
        with pytest.raises(OperationalError, match='deadline'):
            async with llm.stream(factory, context=OperationContext('late', time.monotonic()+.01)):
                pass
        with pytest.raises(CleanupPendingError):
            await llm.aclose()
        release.set()
        await asyncio.sleep(0)
        await llm.aclose()
        assert raw.aclose.await_count == 2
    run(scenario())


@pytest.mark.parametrize('auth', [object(), lambda: None])
def test_malformed_auth_is_contract_failure(auth):
    async def scenario():
        llm, *_ = owner()
        llm.provider.authenticate.return_value = auth
        with pytest.raises(ContractError, match='Invalid authentication session'):
            await llm.call(AsyncMock())
        await llm.aclose()
    run(scenario())


def test_expired_factory_returned_task_retains_session_until_stopped():
    async def scenario():
        llm, auth, driver = owner(timeout=.01)
        await llm.call(AsyncMock())
        entered, release = asyncio.Event(), asyncio.Event()
        used_live_auth = []
        async def work():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            used_live_auth.append(not auth.closed)
        task = asyncio.create_task(work())
        await entered.wait()
        def factory(**kwargs):
            time.sleep(.02)
            return task
        try:
            with pytest.raises(OperationalError) as error:
                await llm.call(factory, context=OperationContext('expired-factory', time.monotonic()+.01))
            assert error.value.execution == 'possibly_executed'
            assert task in llm._calls and task.cancelling() == 1
            with pytest.raises(CleanupPendingError) as pending:
                await llm.aclose()
            assert pending.value.owners == (llm,)
            assert not auth.closed and not driver.stop.called
        finally:
            release.set()
            await task
        await pending.value.retry()
        assert used_live_auth == [True] and auth.closed
        driver.stop.assert_awaited_once()
    run(scenario())


def test_ready_future_concurrent_reads_never_replace_unconsumed_event():
    async def scenario():
        llm, *_ = owner()
        class Stream:
            def __init__(self):
                self.count = 0
                self.aclose = AsyncMock()
            def __anext__(self):
                self.count += 1
                future = asyncio.get_running_loop().create_future()
                future.set_result(StreamEvent(TextContent(str(self.count))))
                return future
        raw = Stream()
        async with llm.stream(lambda **kwargs: raw, context=OperationContext('ready-futures')) as stream:
            first = asyncio.create_task(anext(stream))
            second = asyncio.create_task(anext(stream))
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(results[1], ContractError)
            assert results[0].payload.text == '1' and raw.count == 1
            assert (await anext(stream)).payload.text == '2'
        await llm.aclose()
    run(scenario())


def test_child_cancelled_stream_and_owner_close_can_retry():
    from api_agent.providers.sessions import InspectionOwner
    async def scenario():
        llm, *_ = owner()
        raw = SimpleNamespace(__anext__=AsyncMock(), aclose=AsyncMock(side_effect=[asyncio.CancelledError(), None]))
        cm = llm.stream(lambda **kwargs: raw)
        stream = await cm.__aenter__()
        with pytest.raises(asyncio.CancelledError):
            await stream.aclose()
        assert stream in llm._streams
        await stream.aclose()
        await cm.__aexit__(None, None, None)
        assert raw.aclose.await_count == 2 and not llm._streams
        await llm.aclose()
        inspection = InspectionOwner()
        inspection._dispose = AsyncMock(side_effect=[asyncio.CancelledError(), None])
        with pytest.raises(asyncio.CancelledError):
            await inspection.aclose()
        await inspection.aclose()
        assert inspection._dispose.await_count == 2
    run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_rejected_stream_disposes_allocated_resource_before_parent(failure):
    async def scenario():
        llm, auth, driver = owner()
        rejected = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('pending'), None] if failure else None))
        if failure:
            with pytest.raises(CleanupPendingError) as error:
                async with llm.stream(lambda **kwargs: rejected):
                    pass
            assert llm._streams and not auth.closed and not driver.stop.called
            await error.value.retry()
        else:
            with pytest.raises(ContractError):
                async with llm.stream(lambda **kwargs: rejected):
                    pass
        assert rejected.aclose.await_count == (2 if failure else 1)
        assert not llm._streams
        await llm.aclose()
        assert auth.closed
    run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_rejected_auth_disposes_allocated_resource_before_browser(failure):
    async def scenario():
        llm, _, driver = owner()
        rejected = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('pending'), None] if failure else None))
        llm.provider.authenticate.return_value = rejected
        if failure:
            with pytest.raises(CleanupPendingError) as error:
                await llm.call(AsyncMock())
            assert llm._auth is None and llm._resources and not driver.stop.called
            await error.value.retry()
        else:
            with pytest.raises(ContractError):
                await llm.call(AsyncMock())
        assert rejected.aclose.await_count == (2 if failure else 1)
        assert not llm._resources
        await llm.aclose()
        driver.stop.assert_awaited_once()
    run(scenario())


@pytest.mark.parametrize('timeout', [None, float('inf'), float('nan'), True, False, 0, -1, '1'])
def test_cleanup_timeout_must_be_finite_positive_exact_numeric(timeout):
    from api_agent.providers.sessions import InspectionOwner
    with pytest.raises(ContractError, match='cleanup timeout'):
        InspectionOwner(timeout=timeout)
