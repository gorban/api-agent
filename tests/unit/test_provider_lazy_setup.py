import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.contracts import ContractError, InteractionRequiredError
from api_agent.providers.sessions import LLMSessionOwner


def test_lazy_setup_coalesces_without_credentials_or_account():
    async def scenario():
        auth = AsyncMock(return_value=object())
        auth.aclose = AsyncMock()
        setup = AsyncMock(return_value=auth)
        browser = Mock(side_effect=AssertionError('Unexpected browser'))
        owner = LLMSessionOwner(SimpleNamespace(asetup=setup), browser)
        setup.assert_not_called()
        operation = AsyncMock(return_value='done')
        assert await asyncio.gather(owner.call(operation), owner.call(operation)) == ['done', 'done']
        setup.assert_awaited_once_with(interactive=True, user_index=None, credentials=None)
        assert auth.await_count == 2
        await owner.aclose()
        auth.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_unattended_interaction_error_is_preserved():
    async def scenario():
        message = 'Run attended setup to connect this account.'
        setup = AsyncMock(side_effect=InteractionRequiredError(message))
        owner = LLMSessionOwner(SimpleNamespace(asetup=setup), interactive=False, user_index=2)
        with pytest.raises(InteractionRequiredError, match=message):
            await owner.call(AsyncMock())
        setup.assert_awaited_once_with(interactive=False, user_index=2, credentials=None)
        await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('index', [-1, True, '0'])
def test_setup_rejects_invalid_account(index):
    with pytest.raises(ContractError):
        LLMSessionOwner(SimpleNamespace(), user_index=index)


def test_legacy_auth_requires_dependencies():
    async def scenario():
        owner = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock()), user_index=0)
        with pytest.raises(ContractError, match='requires browser and credentials'):
            await owner.call(AsyncMock())
        await owner.aclose()
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_setup():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        attempts = 0
        auth = AsyncMock(return_value='opaque')
        auth.aclose = AsyncMock()
        async def setup(**kwargs):
            nonlocal attempts
            attempts += 1
            started.set()
            await release.wait()
            return auth
        owner = LLMSessionOwner(SimpleNamespace(asetup=setup))
        task = asyncio.create_task(owner.call(AsyncMock()))
        await started.wait()
        operation = AsyncMock(return_value='done')
        second = asyncio.create_task(owner.call(operation))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await second == 'done'
        assert attempts == 1
        operation.assert_awaited_once_with(auth='opaque')
        await owner.aclose()
        auth.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_disposal_cancels_inflight_setup():
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def setup(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        owner = LLMSessionOwner(SimpleNamespace(asetup=setup))
        task = asyncio.create_task(owner.call(AsyncMock()))
        await started.wait()
        await owner.aclose()
        assert cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


@pytest.mark.parametrize('hook', [
    'def asetup(self, *, interactive, user_index, credentials): pass',
    'async def asetup(self, *, interactive, credentials): pass',
    'async def asetup(self, *, interactive, user_index, credentials, required): pass',
    'async def asetup(self, *, interactive, user_index, credentials): yield None',
    'asetup = 1',
])
def test_loader_rejects_invalid_setup(tmp_path, hook):
    from api_agent.providers.loading import load_provider, ProviderError
    path = tmp_path / 'factory.py'
    path.write_text('class Provider:\n    version = 1\n    ' + hook + '\ndef create(config, services): return Provider()\n')
    with pytest.raises(ProviderError, match='provider_initialization_failed'):
        load_provider(str(path) + ':create', {}, {})


def test_loader_accepts_setup_without_inference_operations(tmp_path):
    from api_agent.providers.loading import load_provider
    path = tmp_path / 'factory.py'
    path.write_text('from api_agent.providers.contracts import ProviderCapabilities\nclass Provider:\n    version = 1\n    capabilities = ProviderCapabilities("llm")\n    async def asetup(self, *, interactive, user_index, credentials): pass\ndef create(config, services): return Provider()\n')
    provider = load_provider(str(path) + ':create', {}, {}, kind='llm')
    asyncio.run(provider.aclose())


def test_setup_protocol_declaration():
    from api_agent.providers.contracts import LLMSetup
    asyncio.run(LLMSetup.asetup(None, interactive=True, user_index=None, credentials=None))


@pytest.mark.parametrize('changes', [dict(kind=1), dict(llm_role=1), dict(features=('invalid',)),
                                      dict(features=['tools']), dict(features=('tools', 'tools')),
                                      dict(kind='api', features=('tools',))])
def test_loader_capability_validation(changes):
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.loading import validate_capabilities, ProviderError
    fields = dict(kind='llm')
    fields.update(changes)
    with pytest.raises(ProviderError, match='provider_capability_invalid'):
        validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities(**fields)))


def test_stream_view_preserves_operational_error():
    from api_agent.providers.contracts import ProviderCapabilities, OperationalError
    from api_agent.providers.loading import LoadedProvider
    from api_agent.providers.llm import LLMRequest,CompletionRequest,Message,TextContent
    request = LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    error = OperationalError('unavailable')
    method = Mock(side_effect=error)
    implementation = SimpleNamespace(capabilities=ProviderCapabilities('llm', ('stream',)),
                                     credential_protection_version=1, astream=method)
    implementation.bind_credential_authority = lambda authority: setattr(implementation, 'authority', authority)
    provider = LoadedProvider(implementation, 'test_module')
    async def scenario():
        iterator = provider.require('llm', 'stream').astream(request)
        try:
            with pytest.raises(OperationalError) as caught:
                await anext(iterator)
            assert caught.value is error
            method.assert_called_once_with(request,auth=None)
        finally:
            await iterator.aclose()
            await provider.aclose()
    asyncio.run(scenario())


def test_setup_deadline_leaves_owner_available_for_cleanup():
    import time
    from api_agent.providers.contracts import OperationalError
    from api_agent.providers.control import OperationContext
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        auth = AsyncMock(return_value='opaque')
        auth.aclose = AsyncMock()
        async def setup(**kwargs):
            started.set()
            await release.wait()
            return auth
        owner = LLMSessionOwner(SimpleNamespace(asetup=setup))
        with pytest.raises(OperationalError) as caught:
            await owner.call(AsyncMock(), context=OperationContext('setup', time.monotonic() + .01))
        assert started.is_set() and caught.value.kind == 'deadline'
        release.set()
        await owner.call(AsyncMock())
        await owner.aclose()
        auth.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_setup_cleanup_failure_can_be_retried():
    from api_agent.providers.contracts import CleanupPendingError
    async def scenario():
        auth = AsyncMock(return_value='opaque')
        auth.aclose = AsyncMock(side_effect=[RuntimeError('hidden'), None])
        owner = LLMSessionOwner(SimpleNamespace(asetup=AsyncMock(return_value=auth)))
        await owner.call(AsyncMock())
        with pytest.raises(CleanupPendingError) as caught:
            await owner.aclose()
        assert caught.value.owners == (owner,)
        await caught.value.retry()
        assert auth.aclose.await_count == 2
    asyncio.run(scenario())


def test_rebind_preserves_unattended_setup_policy():
    async def scenario():
        provider = SimpleNamespace(asetup=AsyncMock(side_effect=InteractionRequiredError('Attend setup.')))
        owner = LLMSessionOwner(provider, interactive=False)
        rebound = await owner.rebind(provider, user_index=4, credentials=None)
        with pytest.raises(InteractionRequiredError):
            await rebound.call(AsyncMock())
        provider.asetup.assert_awaited_once_with(interactive=False, user_index=4, credentials=None)
        await rebound.aclose()
    asyncio.run(scenario())
