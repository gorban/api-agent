import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.control import CleanupPendingError, OperationalError
from api_agent.providers.shared_setup import SharedSetupOwner, RevocableCredentials
from api_agent.providers.llm import AvailableSelection


def test_concurrent_waiters_refresh_and_cancellation():
    async def scenario():
        gate = asyncio.Event()
        calls = []
        async def call(callback):
            calls.append('call')
            await gate.wait()
            await callback(auth=object())
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        factory = AsyncMock()  # Sync factory, assert exactly one invocation.
        factory = lambda credentials: child
        owner = SharedSetupOwner('op', 'provider:role:0', factory)
        first = asyncio.create_task(owner.ensure_ready())
        second = asyncio.create_task(owner.ensure_ready())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(calls) == 1
        gate.set()
        assert await second is None
        assert await owner.ensure_ready() is None
        assert calls == ['call', 'call']
        await owner.aclose()
        child.aclose.assert_awaited_once()
        with pytest.raises(SessionClosedError):
            await owner.ensure_ready()
    asyncio.run(scenario())


def test_binding_rejected_before_factory_and_is_read_only():
    async def scenario():
        factory = AsyncMock()
        owner = SharedSetupOwner('op', 'selected', factory)
        with pytest.raises(ContractError, match='outside shared binding'):
            await owner.ensure_ready(selected_identity='other')
        factory.assert_not_called()
        with pytest.raises(AttributeError):
            owner.selected_identity = 'other'
        await owner.aclose()
    asyncio.run(scenario())


def test_failed_setup_is_sticky_and_unknown_factory_is_safe():
    async def scenario():
        calls = []
        def factory(credentials):
            calls.append(1)
            raise ValueError('private detail')
        owner = SharedSetupOwner('op', 'selected', factory)
        for _ in range(2):
            with pytest.raises(OperationalError, match='^unknown$'):
                await owner.ensure_ready()
        assert calls == [1]
        await owner.aclose()
    asyncio.run(scenario())


def test_missing_auth_and_failed_child_disposal_keeps_retry():
    async def scenario():
        async def call(callback):
            await callback(auth=None)
        child = SimpleNamespace(call=call, aclose=AsyncMock(side_effect=[ValueError('private'), None]))
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child)
        with pytest.raises(ContractError, match='did not provide authentication'):
            await owner.ensure_ready()
        with pytest.raises(CleanupPendingError) as caught:
            await owner.aclose()
        assert caught.value.owners == (owner,)
        await caught.value.retry()
        assert child.aclose.await_count == 2
    asyncio.run(scenario())


def test_revocation_rejects_late_reply_and_new_read():
    async def scenario():
        gate = asyncio.Event()
        async def reader(index, kind):
            await gate.wait()
            return 'private'
        credentials = RevocableCredentials('op', reader)
        read = asyncio.create_task(credentials(0, 'password'))
        await asyncio.sleep(0)
        credentials.revoke()
        gate.set()
        with pytest.raises(SessionClosedError):
            await read
        with pytest.raises(SessionClosedError):
            await credentials(0, 'password')
        await credentials.aclose()
    asyncio.run(scenario())


def test_close_cancels_setup_and_revokes_credentials():
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()
        bound = []
        async def call(callback):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        def factory(credentials):
            bound.append(credentials)
            return child
        owner = SharedSetupOwner('op', 'selected', factory, credentials=AsyncMock(return_value='secret'))
        waiter = asyncio.create_task(owner.ensure_ready())
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await owner.aclose()
        assert stopped.is_set()
        child.aclose.assert_awaited_once()
        with pytest.raises(SessionClosedError):
            await bound[0](0, 'password')
    asyncio.run(scenario())


def test_stubborn_credential_task_retained_until_retry():
    async def scenario():
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def reader():
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            return 'private'
        credentials = RevocableCredentials('op', reader, timeout=.001)
        read = asyncio.create_task(credentials())
        await entered.wait()
        with pytest.raises(CleanupPendingError) as caught:
            await credentials.aclose()
        assert caught.value.owners == (credentials,)
        finish.set()
        with pytest.raises(SessionClosedError):
            await read
        await caught.value.retry()
    asyncio.run(scenario())


def test_caller_cancellation_is_preserved_after_credential_revocation():
    async def scenario():
        entered = asyncio.Event()
        async def reader():
            entered.set()
            await asyncio.Event().wait()
        credentials = RevocableCredentials('op', reader)
        waiter = asyncio.create_task(credentials())
        await entered.wait()
        credentials.revoke()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await credentials.aclose()
        assert not credentials._reads
    asyncio.run(scenario())


def test_settlement_drains_shielded_reads_without_disposing_authentication():
    async def scenario():
        entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def reader():
            entered.set()
            try:
                await release.wait()
            finally:
                stopped.set()
            return 'synthetic-value'
        async def call(callback):
            return await callback(auth=object())
        child = SimpleNamespace(call=call, settle_execution=AsyncMock(), aclose=AsyncMock())
        shared = SharedSetupOwner('op', 'selected', lambda credentials: child, credentials=reader)
        await shared.ensure_ready()
        read = asyncio.create_task(shared._credentials())
        await entered.wait()
        try:
            await shared.settle_execution()
            assert stopped.is_set() and shared._credentials._closed
            assert not shared._credentials._reads
            with pytest.raises(SessionClosedError):
                await read
            child.aclose.assert_not_awaited()
        finally:
            release.set()
            await shared.aclose()
            await asyncio.gather(read, return_exceptions=True)
        child.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('args', [('', 'id', lambda _: None), ('op', '', lambda _: None), ('op', 'id', None)])
def test_invalid_binding(args):
    with pytest.raises(ContractError):
        SharedSetupOwner(*args)


def test_invalid_credentials():
    with pytest.raises(ContractError):
        RevocableCredentials('', None)


def test_successful_scoped_reader_and_factory_contract_failure():
    async def scenario():
        from api_agent.providers.sessions import ScopedRoleCredentials, ScopedLLMCredentials
        source = AsyncMock(return_value='opaque')
        reader = ScopedLLMCredentials('llm', ScopedRoleCredentials(['llm'], 2, source))
        credentials = RevocableCredentials('op', reader)
        assert await credentials(2, 'password') == 'opaque'
        with pytest.raises(ContractError):
            await credentials(1, 'password')
        with pytest.raises(ContractError):
            await credentials(2, 'other')
        source.assert_awaited_once_with('llm', 2, 'password')
        await credentials.aclose()
        def factory(credentials):
            raise ContractError('binding rejected')
        owner = SharedSetupOwner('op', 'selected', factory)
        assert owner.selected_identity == 'selected'
        with pytest.raises(ContractError, match='binding rejected'):
            await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(scenario())


def test_actual_session_owner_has_one_setup_and_one_concurrent_refresh():
    async def scenario():
        from api_agent.providers.sessions import LLMSessionOwner
        entered = asyncio.Event()
        finish = asyncio.Event()
        class Auth:
            def __init__(self):
                self.reads = 0
                self.aclose = AsyncMock()
            async def __call__(self):
                self.reads += 1
                entered.set()
                await finish.wait()
                return object()
        auth = Auth()
        provider = SimpleNamespace(asetup=AsyncMock(return_value=auth))
        factory_calls = []
        def factory(credentials):
            factory_calls.append(1)
            return LLMSessionOwner(provider, credentials=credentials)
        owner = SharedSetupOwner('op', 'selected', factory)
        waiters = [asyncio.create_task(owner.ensure_ready()) for _ in range(6)]
        await entered.wait()
        assert provider.asetup.await_count == 1
        assert auth.reads == 1
        finish.set()
        assert await asyncio.gather(*waiters) == [None] * 6
        entered.clear()
        finish.clear()
        refresh_waiters = [asyncio.create_task(owner.ensure_ready()) for _ in range(6)]
        await entered.wait()
        assert auth.reads == 2
        refresh_waiters[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh_waiters[0]
        assert auth.reads == 2
        finish.set()
        assert await asyncio.gather(*refresh_waiters[1:]) == [None] * 5
        assert auth.reads == 2
        assert factory_calls == [1]
        assert provider.asetup.await_count == 1
        await owner.aclose()
        auth.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_unknown_reader_failure_is_safe():
    async def scenario():
        reader = RevocableCredentials('op', AsyncMock(side_effect=ValueError('private')))
        with pytest.raises(OperationalError, match='^unknown$'):
            await reader()
        await reader.aclose()
    asyncio.run(scenario())


def test_synchronous_reader_failure_is_sanitized():
    async def run():
        def reader(*args):
            raise ValueError('private credential details')
        owner = RevocableCredentials('operation', reader)
        with pytest.raises(OperationalError) as error:
            await owner(0, 'password')
        assert error.value.kind == 'unknown'
        assert 'private credential' not in str(error.value)
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('invalid', [None, object(), type('Invalid', (), {'call': 1, 'aclose': 1})()])
def test_invalid_factory_result_fails_safely_without_poisoning_cleanup(invalid):
    async def run():
        subject = SharedSetupOwner('operation', 'identity', lambda credentials: invalid)
        with pytest.raises(ContractError, match='Invalid shared session owner'):
            await subject.ensure_ready()
        await subject.aclose()
        assert subject._owner is None
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['setup', 'credentials'])
def test_shared_owner_rejects_another_event_loop_before_side_effects(kind):
    reader = AsyncMock(return_value='opaque')
    async def call(callback):
        await callback(auth=object())
    child = SimpleNamespace(call=AsyncMock(side_effect=call), aclose=AsyncMock())
    factory = lambda credentials: child
    subject = (SharedSetupOwner('op', 'selected', factory, credentials=reader)
               if kind == 'setup' else RevocableCredentials('op', reader))
    original_loop = asyncio.new_event_loop()

    async def bind():
        if kind == 'setup':
            await subject.ensure_ready()
        else:
            await subject()

    async def reject():
        with pytest.raises(ContractError, match='another execution context'):
            await subject.aclose()
        with pytest.raises(ContractError, match='another execution context'):
            if kind == 'setup':
                await subject.ensure_ready()
            else:
                await subject()
        if kind == 'credentials':
            with pytest.raises(ContractError, match='another execution context'):
                subject.revoke()
        assert not subject._closed

    try:
        original_loop.run_until_complete(bind())
        asyncio.run(reject())
        if kind == 'setup':
            child.call.assert_awaited_once()
            child.aclose.assert_not_awaited()
            assert not subject._credentials._closed
        else:
            reader.assert_awaited_once()
        original_loop.run_until_complete(subject.aclose())
    finally:
        original_loop.close()


@pytest.mark.parametrize('kind', ['setup', 'credentials'])
def test_shared_owner_rejects_another_process_before_side_effects(kind):
    async def scenario():
        reader = AsyncMock(return_value='opaque')
        factory = AsyncMock()
        subject = (SharedSetupOwner('op', 'selected', factory, credentials=reader)
                   if kind == 'setup' else RevocableCredentials('op', reader))
        original_pid = subject._pid
        subject._pid = -1
        try:
            with pytest.raises(ContractError, match='another execution context'):
                await subject.aclose()
            with pytest.raises(ContractError, match='another execution context'):
                if kind == 'setup':
                    await subject.ensure_ready()
                else:
                    await subject()
            if kind == 'credentials':
                with pytest.raises(ContractError, match='another execution context'):
                    subject.revoke()
            assert not subject._closed
            if kind == 'setup':
                assert not subject._credentials._closed
            factory.assert_not_called()
            reader.assert_not_called()
        finally:
            subject._pid = original_pid
            await subject.aclose()
    asyncio.run(scenario())


def test_actual_session_setup_failure_is_sticky():
    async def scenario():
        from api_agent.providers.sessions import LLMSessionOwner
        provider = SimpleNamespace(asetup=AsyncMock(side_effect=OperationalError('unavailable')))
        owner = SharedSetupOwner('op', 'selected', lambda credentials: LLMSessionOwner(provider))
        for _ in range(3):
            with pytest.raises(OperationalError, match='^unavailable$'):
                await owner.ensure_ready()
        provider.asetup.assert_awaited_once()
        await owner.aclose()
    asyncio.run(scenario())


def test_catalog_ttl_starts_at_validated_completion_and_refresh_failure_does_not_serve_expired_result(monkeypatch):
    from api_agent.providers import shared_setup
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(shared_setup, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    async def scenario():
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            if len(calls) == 1:
                clock.now = 10.0
            if len(calls) == 2:
                raise OperationalError('unavailable')
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child,
                                 describe=describe, catalog_ttl=5)
        assert (await owner.describe())[0].selected_llm == 'sample'
        clock.now = 14.999
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert calls == ['describe']
        clock.now = 15.0
        with pytest.raises(OperationalError, match='unavailable'):
            await owner.describe()
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert len(calls) == 3
        await owner.aclose()
        child.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_valid_empty_catalog_is_cached_until_ttl_expires(monkeypatch):
    from api_agent.providers import shared_setup
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(shared_setup, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    async def scenario():
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            return ()
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child,
                                 describe=describe, catalog_ttl=2)
        assert await owner.describe() == ()
        clock.now = 1.999
        assert await owner.describe() == ()
        assert len(calls) == 1
        clock.now = 2.0
        assert await owner.describe() == ()
        assert len(calls) == 2
        await owner.aclose()
    asyncio.run(scenario())


def test_catalog_failure_validation_and_cancelled_waiter_allow_later_retrieval():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            if len(calls) == 1:
                entered.set()
                await release.wait()
                raise OperationalError('unavailable')
            if len(calls) == 2:
                return ('invalid',)
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child, describe=describe)
        first = asyncio.create_task(owner.describe())
        second = asyncio.create_task(owner.describe())
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        with pytest.raises(OperationalError, match='unavailable'):
            await second
        assert owner._catalog is None
        with pytest.raises(ContractError):
            await owner.describe()
        assert owner._catalog is None
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert len(calls) == 3
        await owner.aclose()
        child.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_cancelled_catalog_operation_is_evicted_and_can_be_retrieved_again():
    async def scenario():
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            if len(calls) == 1:
                raise asyncio.CancelledError()
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child, describe=describe)
        with pytest.raises(asyncio.CancelledError):
            await owner.describe()
        assert owner._catalog is None
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert calls == ['describe', 'describe']
        await owner.aclose()
    asyncio.run(scenario())


def test_catalog_failure_settles_after_sole_waiter_cancels():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            if len(calls) == 1:
                entered.set()
                await release.wait()
                raise OperationalError('unavailable')
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child, describe=describe)
        waiter = asyncio.create_task(owner.describe())
        await entered.wait()
        retrieval = owner._catalog
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not retrieval.done()
        release.set()
        with pytest.raises(OperationalError, match='unavailable'):
            await retrieval
        await asyncio.sleep(0)
        assert owner._catalog is None
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert calls == ['describe', 'describe']
        await owner.aclose()
        child.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_zero_catalog_ttl_reuses_inflight_but_not_completed_result():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            entered.set()
            await release.wait()
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child,
                                 describe=describe, catalog_ttl=0)
        await owner.ensure_ready()
        first = asyncio.create_task(owner.describe())
        await entered.wait()
        second = asyncio.create_task(owner.describe())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done()
        release.set()
        assert [item[0].selected_llm for item in await asyncio.gather(first, second)] == ['sample', 'sample']
        assert len(calls) == 1
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert len(calls) == 2
        await owner.aclose()
    asyncio.run(scenario())


def test_waiter_cancelled_after_catalog_success_preserves_validated_cache():
    async def scenario():
        calls = []
        waiter = None
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            asyncio.get_running_loop().call_soon(waiter.cancel)
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        owner = SharedSetupOwner('op', 'selected', lambda credentials: child, describe=describe)
        await owner.ensure_ready()
        waiter = asyncio.create_task(owner.describe())
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert owner._catalog.done() and owner._catalog.exception() is None
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert calls == ['describe']
        await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('ttl', [True, False, -1, float('nan'), float('inf'), 10 ** 1000, '3600', None])
def test_invalid_catalog_ttl(ttl):
    with pytest.raises(ContractError):
        SharedSetupOwner('op', 'selected', lambda credentials: None, catalog_ttl=ttl)


def test_actual_auth_cleanup_failure_and_double_cancellation_retain_credentials():
    async def scenario():
        from api_agent.providers.sessions import LLMSessionOwner
        entered = asyncio.Event()
        finish = asyncio.Event()
        events = []

        class Auth:
            def __init__(self):
                self.closes = 0

            async def __call__(self):
                return object()

            async def aclose(self):
                self.closes += 1
                events.append(f'auth-close-{self.closes}')
                if self.closes == 1:
                    entered.set()
                    await finish.wait()
                    raise ValueError('private cleanup detail')

        auth = Auth()
        provider = SimpleNamespace(asetup=AsyncMock(return_value=auth))
        owner = SharedSetupOwner('op', 'selected',
                                 lambda credentials: LLMSessionOwner(provider, credentials=credentials),
                                 credentials=AsyncMock(return_value='opaque'))
        await owner.ensure_ready()
        original_close = owner._credentials.aclose

        async def close_credentials():
            events.append('credentials-close')
            await original_close()

        owner._credentials.aclose = close_credentials
        closing = asyncio.create_task(owner.aclose())
        await entered.wait()
        closing.cancel()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert events == ['auth-close-1']
        pending_close = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(CleanupPendingError) as caught:
            await pending_close
        assert caught.value.owners == (owner,)
        assert events == ['auth-close-1']
        await caught.value.retry()
        assert events == ['auth-close-1', 'auth-close-2', 'credentials-close']
    asyncio.run(scenario())
