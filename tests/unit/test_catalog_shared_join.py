"""Authentication/catalog results share an owner; tokens stay in that owner."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer, SetupClient
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.testing.provider_setup import IntegrationLLM, RemoteIntegrationLLM

VALUES = (AvailableSelection('gpt-example', display_name='Example', max_output_tokens=2048),)
CAP = 'synthetic-catalog-capability-value-000'


def test_shared_describe_coalesces_uses_owned_auth_and_caches():
    async def scenario():
        session = AsyncMock(return_value='synthetic-secret')
        adapter = SimpleNamespace(asetup=AsyncMock(return_value=session))
        describe = AsyncMock(return_value=VALUES)
        subject = SharedSetupOwner('operation', 'selected', lambda credentials: LLMSessionOwner(adapter), describe=describe)
        try:
            results = await asyncio.gather(*(subject.describe(selected_identity='selected') for _ in range(5)))
            assert results == [VALUES] * 5
            assert await subject.describe() == VALUES
            adapter.asetup.assert_awaited_once()
            describe.assert_awaited_once_with(auth='synthetic-secret')
            with pytest.raises(ContractError, match='outside shared'):
                await subject.describe(selected_identity='other')
        finally:
            await subject.aclose()
        session.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_shared_missing_capability_and_invalid_catalog_fail():
    async def scenario():
        owner = SimpleNamespace(call=AsyncMock(), aclose=AsyncMock())
        for method, expected in [(None, SetupPrerequisiteError), (AsyncMock(return_value='secret'), ContractError)]:
            subject = SharedSetupOwner('op', 'selected', lambda c: owner, describe=method)
            if method is not None:
                owner.call.side_effect = [None, 'secret']
            try:
                with pytest.raises(expected):
                    await subject.describe()
            finally:
                await subject.aclose()
    asyncio.run(scenario())


def test_registry_missing_capability_and_revoked_lease():
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(), aclose=AsyncMock())
        registry = SetupRegistry('op', 'selected', lambda: owner)
        binding = registry.register('worker')
        try:
            with pytest.raises(SetupPrerequisiteError):
                await registry.describe(binding)
            await registry.revoke(binding)
            with pytest.raises(SessionClosedError):
                await registry.describe(binding)
        finally:
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, ProviderAccessError('authorization', 'Catalog permission denied', status=403, code='denied', request_id='req-1')])
def test_real_ipc_catalog_and_safe_error_return_without_auth(failure):
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(), describe=AsyncMock(return_value=VALUES, side_effect=failure), aclose=AsyncMock())
        registry = SetupRegistry('op', 'selected', lambda: owner)
        server = SetupServer(registry, CAP)
        endpoint = await server.start()
        clients = [RemoteIntegrationLLM(endpoint), RemoteIntegrationLLM(endpoint)]
        try:
            if failure is None:
                results = await asyncio.gather(*(asyncio.to_thread(client.describe) for client in clients))
                assert results == [VALUES, VALUES]
                assert await asyncio.to_thread(clients[0].describe) == VALUES
            else:
                for client in clients:
                    with pytest.raises(ProviderAccessError, match='Catalog permission denied') as caught:
                        await asyncio.to_thread(client.describe)
                    assert caught.value.status == 403 and caught.value.code == 'denied' and caught.value.request_id == 'req-1'
            owner.describe.assert_awaited_once()
        finally:
            for client in clients:
                await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_worker_local_join_and_closed_guard(tmp_path, monkeypatch):
    from api_agent.testing import provider_setup
    session = AsyncMock(return_value='synthetic-token')
    implementation = SimpleNamespace(asetup=AsyncMock(return_value=session), adescribe=AsyncMock(return_value=VALUES))
    provider = SimpleNamespace(implementation=implementation, aclose=AsyncMock())
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=provider))
    worker = IntegrationLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path)
    assert worker.describe() == worker.describe() == VALUES
    implementation.adescribe.assert_awaited_once_with(auth='synthetic-token')
    worker.close()
    with pytest.raises(ContractError, match='closed'):
        worker.describe()


def test_local_and_remote_missing_describe_are_prerequisite_skips(tmp_path, monkeypatch):
    from api_agent.testing import provider_setup
    implementation = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='token')))
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=SimpleNamespace(implementation=implementation, aclose=AsyncMock())))
    worker = IntegrationLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path)
    try:
        with pytest.raises(pytest.skip.Exception, match='catalog capability'):
            worker.describe()
    finally:
        worker.close()
    remote = RemoteIntegrationLLM({'version': 1, 'host': '127.0.0.1', 'port': 43123, 'capability': CAP})
    remote.client.describe = lambda: (_ for _ in ()).throw(SetupPrerequisiteError('No catalog capability'))
    try:
        with pytest.raises(pytest.skip.Exception, match='catalog capability'):
            remote.describe()
    finally:
        remote.close()


@pytest.mark.parametrize('phase', ['setup', 'catalog'])
def test_short_waiter_deadline_does_not_limit_shared_work(phase):
    from api_agent.providers.progress import component_timeout

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        session = AsyncMock(return_value='synthetic-secret')

        async def body(result):
            async with component_timeout('Shared catalog test', 1):
                entered.set()
                await release.wait()
                return result

        async def setup(**kwargs):
            return await body(session) if phase == 'setup' else session

        async def describe(*, auth):
            assert auth == 'synthetic-secret'
            return await body(VALUES) if phase == 'catalog' else VALUES

        adapter = SimpleNamespace(asetup=AsyncMock(side_effect=setup))
        subject = SharedSetupOwner('op', 'selected', lambda c: LLMSessionOwner(adapter), describe=describe)

        async def short():
            async with component_timeout('Short consumer', .03):
                return await subject.describe()

        async def long():
            async with component_timeout('Long consumer', 1):
                return await subject.describe()

        try:
            first = asyncio.create_task(short())
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(long())
            with pytest.raises(TimeoutError):
                await first
            release.set()
            assert await second == VALUES
            assert await subject.describe() == VALUES
            adapter.asetup.assert_awaited_once()
            assert not subject._catalog.cancelled() and subject._catalog.exception() is None
        finally:
            release.set()
            await subject.aclose()
        session.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_revoke_catalog_waiter_does_not_revoke_sibling_or_owned_catalog():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def describe():
            entered.set()
            await release.wait()
            return VALUES

        owner = SimpleNamespace(ensure_ready=AsyncMock(), describe=AsyncMock(side_effect=describe), aclose=AsyncMock())
        registry = SetupRegistry('op', 'selected', lambda: owner)
        first_binding, other_binding = registry.register('first'), registry.register('other')
        try:
            first = asyncio.create_task(registry.describe(first_binding))
            await asyncio.wait_for(entered.wait(), 2)
            other = asyncio.create_task(registry.describe(other_binding))
            await registry.revoke(first_binding)
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not registry._catalog.cancelled()
            release.set()
            assert await other == VALUES
            owner.describe.assert_awaited_once()
        finally:
            release.set()
            await registry.aclose()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_catalog_completed_concurrently_with_revocation_cannot_escape(monkeypatch):
    from api_agent.providers import setup_registry as module

    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(), describe=AsyncMock(return_value=VALUES), aclose=AsyncMock())
        registry = SetupRegistry('op', 'selected', lambda: owner)
        binding = registry.register('worker')
        original, revocations = module._spawn, []
        count = 0

        def spawn(awaitable):
            nonlocal count
            count += 1
            task = original(awaitable)
            if count == 4:  # Ready, ready waiter, catalog, catalog waiter.
                task.add_done_callback(lambda _: revocations.append(asyncio.create_task(registry.revoke(binding))))
            return task

        monkeypatch.setattr(module, '_spawn', spawn)
        try:
            with pytest.raises(SessionClosedError, match='revoked'):
                await registry.describe(binding)
            await asyncio.gather(*revocations)
            owner.describe.assert_awaited_once()
        finally:
            await registry.aclose()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('frame', [
    {'version': 1, 'status': 'catalog', 'catalog': [], 'extra': 'untrusted'},
    {'version': 1, 'status': 'catalog'},
    {'version': 1, 'status': 'catalog', 'catalog': 'untrusted'},
])
def test_client_rejects_malformed_catalog_before_returning_metadata(frame):
    from unittest.mock import Mock
    from api_agent.providers.setup_transport import _encode

    async def scenario():
        client = SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 43123, 'capability': CAP})
        reader = asyncio.StreamReader()
        reader.feed_data(_encode(frame))
        reader.feed_eof()
        client._reader = reader
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        with pytest.raises(ContractError, match='catalog'):
            await client._exchange('describe')
    asyncio.run(scenario())
