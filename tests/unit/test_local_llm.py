"""Local inference controls use synthetic providers without browsers or I/O."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderCapabilities, SessionClosedError, ProviderAccessError, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError, OperationContext, OperationalError
from api_agent.providers.llm import AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.runtime.local_llm import LocalLLM


def request(model='sample'):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('hello'),)),)))


@pytest.fixture
def selected(tmp_path):
    events = []
    class Auth:
        async def __call__(self): return 'secret'
        async def aclose(self): events.append('auth-close')
    class Provider:
        capabilities = ProviderCapabilities(kind='llm')
        async def asetup(self, **options):
            events.append(('setup', options))
            return Auth()
        async def adescribe(self, *, auth):
            events.append('describe')
            return (AvailableSelection('sample'), AvailableSelection('other'))
        async def acomplete(self, value, *, auth):
            events.append(('complete', value, auth))
            return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(2, 3))
    implementation = Provider()
    loaded = SimpleNamespace(implementation=implementation, astart=AsyncMock(), aclose=AsyncMock())
    loader = AsyncMock(return_value=loaded)
    owner = LocalLLM({'LLM_PROVIDER': 'synthetic', 'LLM_ALLOWED_MODELS': 'sample'}, tmp_path, loader=loader)
    return owner, implementation, loaded, loader, events


def test_construction_and_studio_callback_are_lazy(selected):
    owner, _, loaded, loader, events = selected
    async def run():
        assert await owner.call(AsyncMock(return_value='status')) == 'status'
        loader.assert_not_called()
        assert events == [] and owner.usage == ()
        await owner.aclose()
        await owner.aclose()
        loaded.aclose.assert_not_called()
        with pytest.raises(SessionClosedError): await owner.describe()
    asyncio.run(run())


def test_shared_auth_catalog_usage_and_fresh_requests(selected):
    owner, _, loaded, loader, events = selected
    async def run():
        await asyncio.gather(owner.ensure_ready(), owner.ensure_ready())
        assert [event[0] for event in events if isinstance(event, tuple)] == ['setup']
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert events.count('describe') == 1
        assert (await owner.complete(request())).usage == Usage(2, 3)
        assert (await owner.complete(request())).payload.content == (TextContent('answer'),)
        assert len(owner.usage) == 2 and owner.usage[0]['usage']['input_tokens'] == 2
        copy = owner.usage
        copy[0]['outcome'] = 'changed'
        assert owner.usage[0]['outcome'] == 'completed'
        assert loader.call_args.args[2]['browser_inspection'] is owner.inspection
        assert owner.emissions.content_export('secret') != 'secret'
        await owner.aclose()
        loaded.aclose.assert_awaited_once()
        assert events.count('auth-close') == 1
    asyncio.run(run())


def test_studio_http_catalog_recovers_after_transient_provider_failure(selected):
    import httpx
    from api_agent.api import Runtime, create_application
    from api_agent.api.studio import StudioBackend
    from api_agent.providers.selection import ModelSelectionPolicy

    owner, implementation, loaded, loader, events = selected
    implementation.adescribe = AsyncMock(side_effect=[
        ProviderAccessError('unavailable', 'Synthetic catalog failure.'),
        (AvailableSelection('sample'),),
    ])

    async def describe(*, auth):
        return await owner.describe()

    backend = StudioBackend(owner=owner, describe=describe,
        complete=lambda *args, **kwargs: None,
        policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'sample'}),
        origin='http://127.0.0.1:9443', nonce='s' * 32, scrub=lambda text: text)
    application = create_application(Runtime('synthetic', services={'studio_backend': backend}))

    async def run():
        transport = httpx.ASGITransport(app=application, client=('127.0.0.1', 41337))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8443',
            headers={'Origin': 'http://127.0.0.1:9443', 'X-API-Agent-Session': 's' * 32},
            trust_env=False) as client:
            first = await client.get('/app/agent/models')
            second = await client.get('/app/agent/models')
            third = await client.get('/app/agent/models')
        assert first.status_code == 502 and first.json()['code'] == 'unavailable'
        assert second.status_code == third.status_code == 200
        assert second.json()['default'] == third.json()['default'] == 'sample'
        assert implementation.adescribe.await_count == 2
        assert [event[0] for event in events if isinstance(event, tuple)] == ['setup']
        loader.assert_awaited_once()
        await backend.aclose()
        loaded.aclose.assert_awaited_once()
        assert events.count('auth-close') == 1
    asyncio.run(run())


@pytest.mark.parametrize('configured, expected', [(None, 3600), (0, 0), (12.5, 12.5)])
def test_local_llm_passes_catalog_ttl_to_shared_owner(selected, configured, expected):
    owner, _, _, _, _ = selected
    if configured is not None:
        owner.config = {**owner.config, 'LLM_CATALOG_CACHE_TTL_SECONDS': configured}
    async def run():
        await owner.ensure_ready()
        assert owner.shared._catalog_ttl == expected
        await owner.aclose()
    asyncio.run(run())


def test_admission_rejects_before_authentication(selected):
    owner, _, _, loader, _ = selected
    async def run():
        with pytest.raises(ProviderAccessError): await owner.complete(request('forbidden'))
        with pytest.raises(OperationalError, match='deadline'):
            await owner.complete(request(), context=OperationContext('expired', 0))
        loader.assert_not_called()
        assert await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())


def test_failed_setup_is_sticky_without_fallback(selected):
    owner, implementation, _, _, events = selected
    implementation.asetup = AsyncMock(side_effect=ProviderAccessError('authentication', 'Selected authentication failed.'))
    async def run():
        for _ in range(2):
            with pytest.raises(ProviderAccessError): await owner.ensure_ready()
        implementation.asetup.assert_awaited_once()
        assert events == []
        await owner.aclose()
    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_shared_setup(selected):
    owner, implementation, _, _, _ = selected
    original = implementation.asetup
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def setup(**options):
            entered.set()
            await release.wait()
            return await original(**options)
        implementation.asetup = setup
        waiter = asyncio.create_task(owner.ensure_ready())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        release.set()
        await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(run())


def test_retryable_cleanup_retains_inspection_until_provider_acknowledges(selected):
    owner, _, loaded, _, _ = selected
    loaded.aclose.side_effect = [RuntimeError('blocked'), None]
    async def run():
        await owner.ensure_ready()
        with pytest.raises(CleanupPendingError) as failure: await owner.aclose()
        assert not owner.inspection._closed
        await failure.value.retry()
        assert owner.inspection._closed
        assert loaded.aclose.await_count == 2
    asyncio.run(run())


def test_missing_selection_and_bad_timeout(tmp_path):
    with pytest.raises(ValueError): LocalLLM({}, tmp_path, timeout=0)
    async def run():
        owner = LocalLLM({}, tmp_path)
        with pytest.raises(SetupPrerequisiteError): await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(run())


def test_load_failure_retains_owned_resources(selected):
    owner, _, _, loader, _ = selected
    retained = SimpleNamespace(aclose=AsyncMock())
    error = RuntimeError('load failed')
    error.retained_owners = (retained,)
    loader.side_effect = error
    async def run():
        with pytest.raises(RuntimeError): await owner.ensure_ready()
        await owner.aclose()
        retained.aclose.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('registry', [None, object()])
def test_selected_credentials_are_scoped_and_lazy(selected, monkeypatch, registry):
    from api_agent.runtime import local_llm
    owner, _, _, _, events = selected
    owner.config = {**owner.config, 'UNATTENDED_USER_INDEX': 2, 'LLM_CREDENTIAL_ROLE': 'personal'}
    owner.registry = registry
    bindings = []
    from api_agent.providers.sessions import ScopedRoleCredentials
    def bind(config, roles, index, **options):
        bindings.append((roles, index, options))
        return ScopedRoleCredentials(('personal',), 2, AsyncMock(return_value='username'))
    monkeypatch.setattr(local_llm, 'bind_role_credentials', bind)
    monkeypatch.setattr(local_llm, 'registry_for_owner', lambda root: 'registry')
    async def run():
        assert bindings == []
        await owner.ensure_ready()
        assert bindings[0][:2] == (('personal',), 2)
        assert bindings[0][2]['registry'] == (registry or 'registry')
        setup = events[0][1]
        assert setup['user_index'] == 2 and setup['credentials'] is not None
        await owner.aclose()
    asyncio.run(run())


def test_provider_without_start_and_non_string_auth(selected):
    owner, implementation, loaded, _, _ = selected
    del loaded.astart
    class Auth:
        async def __call__(self): return object()
        async def aclose(self): pass
    implementation.asetup = AsyncMock(return_value=Auth())
    async def run():
        await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())


def test_failed_completion_records_usage_and_quote_failure_preserves_result(selected):
    owner, implementation, _, _, _ = selected
    implementation.acomplete = AsyncMock(side_effect=ProviderAccessError('unavailable', 'Unavailable.'))
    async def run():
        with pytest.raises(ProviderAccessError): await owner.complete(request())
        assert owner.usage[0]['outcome'] == 'failed' and owner.usage[0]['usage'] is None
        implementation.acomplete.side_effect = None
        implementation.acomplete.return_value = LLMResult(Message('assistant', (TextContent('answer'),)))
        implementation.quote_cost = lambda *args: None
        assert await owner.complete(request())
        assert 'quote' not in owner.usage[1]
        await owner.aclose()
    asyncio.run(run())


def test_helpers_pending_cleanup_is_retryable(selected):
    owner, _, _, _, _ = selected
    owner._timeout = .01
    async def run():
        gate = asyncio.Event()
        helper = asyncio.create_task(gate.wait())
        owner._helpers.add(helper)
        with pytest.raises(CleanupPendingError) as pending: await owner.aclose()
        assert not helper.cancelled() and not owner.inspection._closed
        gate.set()
        await helper
        await pending.value.retry()
        assert owner.inspection._closed
    asyncio.run(run())


def test_readiness_and_catalog_register_each_refreshed_string_auth(selected):
    owner, implementation, _, _, _ = selected
    tokens = iter(('readiness-credential', 'refresh-credential', 'catalog-credential'))
    class Auth:
        async def __call__(self): return next(tokens)
        async def aclose(self): pass
    implementation.asetup = AsyncMock(return_value=Auth())
    async def describe(*, auth):
        assert auth == 'catalog-credential'
        raise ProviderAccessError('unavailable', 'Catalog failed: ' + auth)
    implementation.adescribe = describe
    async def run():
        await owner.ensure_ready()
        assert 'readiness-credential' not in owner.emissions.content_export({'text': 'readiness-credential'})['text']
        with pytest.raises(ProviderAccessError) as failure: await owner.describe()
        safe = owner.emissions.content_export({'text': str(failure.value)})['text']
        assert 'catalog-credential' not in safe and 'Catalog failed' in safe
        assert 'refresh-credential' not in owner.emissions.content_export({'text': 'refresh-credential'})['text']
        await owner.aclose()
    asyncio.run(run())
