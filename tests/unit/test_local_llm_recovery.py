"""Explicit actions recover failed readiness without replaying inference."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderCapabilities, ProviderAccessError
from api_agent.providers.control import CleanupPendingError, OperationalError
from api_agent.providers.llm import AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message, TextContent
from api_agent.runtime.local_llm import LocalLLM


@pytest.fixture
def recovery(tmp_path):
    events, generations = [], []

    class Auth:
        def __init__(self, provider): self.provider = provider
        async def __call__(self):
            events.append(('auth', self.provider.number))
            self.provider.auth_calls += 1
            if self.provider.fail_auth or self.provider.auth_calls in self.provider.fail_auth_at:
                raise RuntimeError('Synthetic readiness failure')
            await self.provider.authority.aregister_secret('synthetic-credential')
            return 'synthetic-credential'
        async def aclose(self): events.append(('auth-close', self.provider.number))

    class Provider:
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        capabilities = ProviderCapabilities(kind='llm')
        def __init__(self, number):
            self.number, self.fail_auth, self.fail_complete = number, False, False
            self.auth_calls, self.fail_auth_at = 0, set()
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.release.set()
        async def asetup(self, **options):
            events.append(('setup', self.number, options['interactive'], options['user_index']))
            return Auth(self)
        async def adescribe(self, *, auth):
            events.append(('catalog', self.number))
            return (AvailableSelection('sample'),)
        async def acomplete(self, value, *, auth):
            from api_agent.providers.credential_guard import guard_json
            from api_agent.providers.inference import request_to_wire, result_to_wire
            guard_json(request_to_wire(value), self.authority._secrets)
            events.append(('complete', self.number))
            self.entered.set()
            await self.release.wait()
            if self.fail_complete:
                raise ProviderAccessError('unavailable', 'Synthetic completion failure')
            result = LLMResult(Message('assistant', (TextContent('answer'),)))
            guard_json(result_to_wire(result), self.authority._secrets)
            return result

    async def load(*args, **kwargs):
        provider = Provider(len(generations))
        async def close(): events.append(('provider-close', provider.number))
        loaded = SimpleNamespace(implementation=provider, astart=AsyncMock(), aclose=AsyncMock(side_effect=close))
        generations.append(loaded)
        events.append(('load', provider.number))
        return loaded

    loader = AsyncMock(side_effect=load)
    owner = LocalLLM({'LLM_PROVIDER': 'synthetic', 'NO_STD_IN': True}, tmp_path,
        loader=loader, cleanup_timeout=.05)
    return owner, generations, events, loader


def value():
    return LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))


async def fail_next_readiness(owner, generations):
    await owner.describe()
    generations[0].implementation.fail_auth = True
    with pytest.raises(OperationalError, match='unknown'):
        await owner.describe()


@pytest.mark.parametrize('next_action', ['describe', 'ensure_ready', 'complete'])
def test_next_action_replaces_failed_generation_once(recovery, next_action):
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        old_shared, old_session = owner.shared, owner.session
        boundary, inspection = owner.emissions, owner.inspection
        assert loader.await_count == 1
        assert not any(event[0] == 'complete' for event in events)
        if next_action == 'complete':
            await owner.complete(value())
        else:
            await getattr(owner, next_action)()
        assert loader.await_count == 2
        assert old_shared._closed and old_session._closed
        assert owner.shared is not old_shared and owner.session is not old_session
        assert owner.emissions is boundary and owner.inspection is inspection
        assert events.index(('auth-close', 0)) < events.index(('provider-close', 0)) < events.index(('load', 1))
        generations[0].aclose.assert_awaited_once()
        assert [event for event in events if event[0] == 'complete'] == ([('complete', 1)] if next_action == 'complete' else [])
        assert [event for event in events if event[0] == 'setup'] == [('setup', 0, False, None), ('setup', 1, False, None)]
        await owner.aclose()
        generations[0].aclose.assert_awaited_once()
        generations[1].aclose.assert_awaited_once()
    asyncio.run(run())


def test_completion_failure_never_replaces_or_replays(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.describe()
        generations[0].implementation.fail_complete = True
        with pytest.raises(ProviderAccessError): await owner.complete(value())
        await owner.describe()
        loader.assert_awaited_once()
        assert [event for event in events if event[0] == 'complete'] == [('complete', 0)]
        assert len(owner.usage) == 1
        await owner.aclose()
    asyncio.run(run())


def test_concurrent_actions_share_recovery_and_retain_usage(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.complete(value())
        await fail_next_readiness(owner, generations)
        await asyncio.gather(owner.describe(), owner.ensure_ready(), owner.describe())
        assert loader.await_count == 2
        assert events.count(('provider-close', 0)) == 1
        assert len(owner.usage) == 1
        await owner.complete(value())
        assert len(owner.usage) == 2
        assert [event for event in events if event[0] == 'complete'] == [('complete', 0), ('complete', 1)]
        await owner.aclose()
    asyncio.run(run())


def test_active_completion_finishes_before_generation_retirement(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.describe()
        old = generations[0].implementation
        old.release.clear()
        completion = asyncio.create_task(owner.complete(value()))
        await old.entered.wait()
        old.fail_auth = True
        with pytest.raises(OperationalError): await owner.describe()
        recovery_action = asyncio.create_task(owner.describe())
        await asyncio.sleep(0)
        assert not completion.done() and loader.await_count == 1
        assert ('provider-close', 0) not in events
        old.release.set()
        assert (await completion).payload.content == (TextContent('answer'),)
        await recovery_action
        assert loader.await_count == 2
        assert len(owner.usage) == 1 and owner.usage[0]['outcome'] == 'completed'
        await owner.aclose()
    asyncio.run(run())


def test_active_generation_timeout_retains_owner_without_cancelling_call(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.describe()
        old = generations[0].implementation
        old.release.clear()
        completion = asyncio.create_task(owner.complete(value()))
        await old.entered.wait()
        old.fail_auth = True
        with pytest.raises(OperationalError): await owner.describe()
        with pytest.raises(CleanupPendingError) as caught: await owner.describe()
        assert not completion.done() and loader.await_count == 1
        assert caught.value.owners == (owner,)
        assert ('provider-close', 0) not in events
        old.release.set()
        await completion
        await caught.value.retry()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_failed_cleanup_is_retried_before_new_setup(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        old = generations[0]
        old.aclose.side_effect = [RuntimeError('Synthetic disposal failure'), None]
        with pytest.raises(CleanupPendingError) as caught: await owner.ensure_ready()
        assert loader.await_count == 1 and owner.shared._closed
        await caught.value.retry()
        assert loader.await_count == 2 and old.aclose.await_count == 2
        assert events.count(('auth-close', 0)) == 1
        await owner.aclose()
    asyncio.run(run())


def test_cancelled_recovery_waiter_does_not_cancel_retirement(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
            events.append(('provider-close', 0))
        generations[0].aclose.side_effect = close
        waiter = asyncio.create_task(owner.describe())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        assert loader.await_count == 1
        release.set()
        await owner.ensure_ready()
        assert loader.await_count == 2
        generations[0].aclose.assert_awaited_once()
        await owner.aclose()
    asyncio.run(run())


def test_one_operation_context_keeps_generation_across_callback_tasks(recovery):
    from api_agent.providers.control import OperationContext
    owner, generations, events, loader = recovery
    async def run():
        context = OperationContext('synthetic-admitted-turn')
        async def catalog(*, auth): return await owner.describe()
        await owner.call(catalog, context=context)
        assert len(context.retained_tasks) == 1
        generations[0].implementation.fail_auth = True
        with pytest.raises(OperationalError): await owner.describe()
        async def complete(*, auth): return await owner.complete(value(), context=context)
        with pytest.raises(OperationalError): await owner.call(complete, context=context)
        loader.assert_awaited_once()
        assert not any(event[0] == 'complete' for event in events)
        await context.aclose()
        assert context.retained_tasks == () and not owner._requests
        await owner.ensure_ready()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_nested_callback_cannot_recover_its_own_failed_generation(recovery):
    owner, generations, events, loader = recovery
    async def run():
        async def operation(*, auth):
            await owner.describe()
            generations[0].implementation.fail_auth = True
            with pytest.raises(OperationalError): await owner.describe()
            with pytest.raises(OperationalError): await owner.describe()
            loader.assert_awaited_once()
        await owner.call(operation)
        await owner.describe()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_cleanup_timeout_retains_receipt_and_blocks_new_generation(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        entered = asyncio.Event()
        async def close():
            entered.set()
            await asyncio.Event().wait()
        generations[0].aclose.side_effect = close
        with pytest.raises(CleanupPendingError) as caught: await owner.describe()
        assert entered.is_set() and loader.await_count == 1
        generations[0].aclose.side_effect = None
        await caught.value.retry()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_pending_credential_helper_blocks_new_generation(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        release = asyncio.Event()
        helper = asyncio.create_task(release.wait())
        owner._helpers.add(helper)
        with pytest.raises(CleanupPendingError) as caught: await owner.ensure_ready()
        assert loader.await_count == 1 and not helper.done()
        release.set()
        await helper
        await caught.value.retry()
        assert loader.await_count == 2
        assert events.count(('provider-close', 0)) == 1
        await owner.aclose()
    asyncio.run(run())


def test_failed_loading_is_retried_on_later_action(recovery):
    owner, generations, events, loader = recovery
    original = loader.side_effect
    loader.side_effect = [RuntimeError('Synthetic loading failure')]
    async def run():
        with pytest.raises(RuntimeError): await owner.describe()
        loader.assert_awaited_once()
        loader.side_effect = original
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_shutdown_cancels_recovery_without_starting_fresh_generation(recovery):
    from api_agent.providers.contracts import SessionClosedError
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        generations[0].aclose.side_effect = close
        waiter = asyncio.create_task(owner.describe())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        release.set()
        await owner.aclose()
        loader.assert_awaited_once()
        with pytest.raises(SessionClosedError): await owner.ensure_ready()
        assert not owner._requests
    asyncio.run(run())


def test_studio_recovery_preserves_stable_owner_and_ready_callback(recovery):
    import json
    from uuid import uuid4
    from django.test import RequestFactory
    from api_agent.api import Runtime, create_application
    from api_agent.api.studio import StudioBackend
    from api_agent.providers.selection import ModelSelectionPolicy
    owner, generations, events, loader = recovery
    bound_ready = owner.ensure_ready
    async def describe(*, auth): return await owner.describe()
    async def complete(request, *, auth, context): return await owner.complete(request, context=context)
    backend = StudioBackend(owner=owner, describe=describe, complete=complete,
        policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'sample'}),
        origin='http://127.0.0.1:9443', nonce='s' * 32, scrub=lambda text: text)
    create_application(Runtime('synthetic'))
    def request(body=None):
        return RequestFactory().generic('GET' if body is None else 'POST', '/',
            data=b'' if body is None else json.dumps(body).encode(), content_type='application/json',
            headers={'Origin': backend.origin, 'X-API-Agent-Session': 's' * 32}, REMOTE_ADDR='127.0.0.1')
    def body():
        return {'session_id': str(uuid4()), 'messages': [{'role': 'user', 'content': 'hello'}]}
    async def run():
        assert (await backend.handle(request(), 'models')).status_code == 200
        generations[0].implementation.fail_auth = True
        failed = await backend.handle(request(body()), 'chat_stream')
        assert failed.status_code == 502 and json.loads(failed.content)['code'] == 'unknown'
        loader.assert_awaited_once()
        assert not any(event[0] == 'complete' for event in events)
        await bound_ready()
        assert backend.owner is owner and loader.await_count == 2
        response = await backend.handle(request(body()), 'chat_stream')
        chunks = b''.join([chunk async for chunk in response.streaming_content])
        assert response.status_code == 200 and b'event: result' in chunks
        assert b'"response_text":"answer"' in chunks
        assert [event for event in events if event[0] == 'complete'] == [('complete', 1)]
        await backend.aclose()
        await owner.aclose()
    asyncio.run(run())


def test_completion_authentication_failure_is_not_replayed_in_same_action(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.describe()
        generations[0].implementation.fail_auth = True
        with pytest.raises(OperationalError): await owner.complete(value())
        loader.assert_awaited_once()
        assert not any(event[0] == 'complete' for event in events)
        assert len(owner.usage) == 1 and owner.usage[0]['outcome'] == 'failed'
        await owner.complete(value())
        assert loader.await_count == 2
        assert [event for event in events if event[0] == 'complete'] == [('complete', 1)]
        assert [row['outcome'] for row in owner.usage] == ['failed', 'completed']
        await owner.aclose()
    asyncio.run(run())


def test_initialization_failure_is_not_retried_inside_same_action(recovery):
    owner, generations, events, loader = recovery
    original = loader.side_effect
    loader.side_effect = RuntimeError('Synthetic loading failure')
    async def run():
        async def operation(*, auth):
            with pytest.raises(RuntimeError): await owner.describe()
            with pytest.raises(RuntimeError): await owner.describe()
            loader.assert_awaited_once()
        await owner.call(operation)
        loader.side_effect = original
        await owner.ensure_ready()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


def test_recovery_preserves_interactive_and_selected_account_options(recovery):
    owner, generations, events, loader = recovery
    owner.config = {**owner.config, 'DEBUG': True, 'UNATTENDED_USER_INDEX': 3}
    async def run():
        await fail_next_readiness(owner, generations)
        await owner.ensure_ready()
        assert [event for event in events if event[0] == 'setup'] == [('setup', 0, True, 3), ('setup', 1, True, 3)]
        assert loader.await_args_list[0] == loader.await_args_list[1]
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('touch_provider', [True, False])
def test_cancellation_resistant_outer_callback_stays_owned_until_acknowledged(recovery, touch_provider):
    owner, generations, events, loader = recovery
    async def run():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        retained = []
        await owner.describe()
        async def operation(*, auth):
            if touch_provider:
                await owner.describe()
            retained.append(asyncio.current_task())
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        action = asyncio.create_task(owner.call(operation))
        await entered.wait()
        try:
            with pytest.raises(CleanupPendingError): await owner.aclose()
            await cancelled.wait()
            with pytest.raises((CleanupPendingError, asyncio.CancelledError)):
                await asyncio.wait_for(asyncio.shield(action), .5)
            assert not retained[0].done()
            with pytest.raises(CleanupPendingError) as retry: await owner.aclose()
            assert retry.value.owners == (owner,)
            assert isinstance(retry.value.__context__, CleanupPendingError)
            assert ('provider-close', 0) not in events
            assert not owner.inspection._closed
        finally:
            release.set()
            await asyncio.gather(action, *retained, return_exceptions=True)
            await owner.aclose()
        generations[0].aclose.assert_awaited_once()
        assert owner.inspection._closed
    asyncio.run(run())


def test_recovery_cannot_replace_generation_of_cancelled_surviving_callback(recovery):
    owner, generations, events, loader = recovery
    async def run():
        entered, cancelled, release, blocked = (asyncio.Event() for _ in range(4))
        retained = []
        async def operation(*, auth):
            await owner.describe()
            retained.append(asyncio.current_task())
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            with pytest.raises(OperationalError): await owner.complete(value())
            blocked.set()
        action = asyncio.create_task(owner.call(operation))
        await entered.wait()
        try:
            action.cancel()
            await cancelled.wait()
            with pytest.raises(CleanupPendingError): await action
            generations[0].implementation.fail_auth = True
            with pytest.raises(OperationalError): await owner.describe()
            with pytest.raises(CleanupPendingError): await owner.describe()
            loader.assert_awaited_once()
            assert ('provider-close', 0) not in events
            assert not retained[0].done()
            release.set()
            await retained[0]
            assert blocked.is_set()
            await owner.describe()
            assert loader.await_count == 2
            assert not any(event[0] == 'complete' for event in events)
        finally:
            release.set()
            await asyncio.gather(action, *retained, return_exceptions=True)
            await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('rejected', ['_recover', 'initialize'])
def test_rejected_recovery_allocation_closes_fresh_coroutine(recovery, monkeypatch, rejected):
    import inspect
    owner, generations, events, loader = recovery
    async def run():
        await fail_next_readiness(owner, generations)
        allocate = asyncio.create_task
        captured = []
        def reject(coroutine, *args, **kwargs):
            if coroutine.cr_code.co_name == rejected:
                captured.append(coroutine)
                raise MemoryError('Synthetic allocation failure')
            return allocate(coroutine, *args, **kwargs)
        monkeypatch.setattr(asyncio, 'create_task', reject)
        with pytest.raises(MemoryError): await owner.ensure_ready()
        assert len(captured) == 1
        assert inspect.getcoroutinestate(captured[0]) == inspect.CORO_CLOSED
        loader.assert_awaited_once()
        monkeypatch.undo()
        await owner.ensure_ready()
        assert loader.await_count == 2
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('next_action', ['describe', 'complete'])
def test_per_call_auth_failure_recovers_on_very_next_action(recovery, next_action):
    owner, generations, events, loader = recovery
    async def run():
        await owner.ensure_ready()
        old = generations[0].implementation
        old.fail_auth_at = {3}
        try:
            with pytest.raises(OperationalError): await owner.complete(value())
            assert old.auth_calls == 3
            assert owner.shared._ready.done() and owner.shared._ready.exception() is None
            loader.assert_awaited_once()
            assert not any(event[0] == 'complete' for event in events)
            if next_action == 'describe':
                assert (await owner.describe())[0].selected_llm == 'sample'
            else:
                assert (await owner.complete(value())).payload.content == (TextContent('answer'),)
            assert loader.await_count == 2
            assert events.index(('auth-close', 0)) < events.index(('provider-close', 0)) < events.index(('load', 1))
            assert [event for event in events if event[0] == 'complete'] == ([('complete', 1)] if next_action == 'complete' else [])
            generations[0].aclose.assert_awaited_once()
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_catalog_transport_failure_keeps_authenticated_generation(recovery):
    owner, generations, events, loader = recovery
    async def run():
        await owner.ensure_ready()
        provider = generations[0].implementation
        provider.adescribe = AsyncMock(side_effect=ProviderAccessError('unavailable', 'Synthetic catalog transport failure'))
        owner.shared._describe = provider.adescribe
        try:
            with pytest.raises(ProviderAccessError): await owner.describe()
            provider.adescribe.side_effect = None
            provider.adescribe.return_value = (AvailableSelection('sample'),)
            assert (await owner.describe())[0].selected_llm == 'sample'
            loader.assert_awaited_once()
            assert ('provider-close', 0) not in events
        finally:
            await owner.aclose()
    asyncio.run(run())
