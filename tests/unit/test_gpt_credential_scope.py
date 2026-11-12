"""Rotating subscription tokens are scrubbed while a call can emit them, then released."""
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.gpt import discovery, inference
from api_agent.llm_providers.gpt.factory import GPTProvider
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def scrubbed(boundary, secret):
    projection = boundary.for_policy(ContentPolicy(True))
    return secret not in projection.content_export({'prompt': 'before ' + secret + ' after'})['prompt']


def connection(generation):
    return SimpleNamespace(access_token=f'access-{generation}', refresh_token=f'refresh-{generation}',
                           id_token=f'identity-{generation}')


@pytest.fixture
def boundary():
    return EmissionBoundary()


@pytest.fixture
def provider(boundary):
    provider = GPTProvider({})
    provider.bind_credential_authority(boundary)
    return provider


def request():
    return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('hello'),)),)))


@pytest.mark.asyncio
async def test_a_refresh_retires_the_tokens_it_replaces(boundary, provider):
    await provider._register_connection(connection(1))
    await provider._register_connection(connection(2))

    assert [scrubbed(boundary, f'{kind}-{generation}') for generation in (1, 2) for kind in ('access', 'refresh', 'identity')] == [
        False, False, False, True, True, True]


@pytest.mark.asyncio
async def test_the_registry_stays_bounded_across_many_refreshes(boundary, provider):
    for generation in range(300):
        for value in (f'code-{generation}', f'access-{generation}'):
            await provider._register_grant(value)
        await provider._register_connection(connection(generation))

    assert boundary._secrets == {'access-299', 'refresh-299', 'identity-299'}


@pytest.mark.asyncio
async def test_exchange_values_stay_scrubbed_until_the_connection_they_produced_is_registered(boundary, provider):
    await provider._register_connection(connection(1))
    await provider._register_grant('code-2')
    await provider._register_grant('access-2')

    assert scrubbed(boundary, 'code-2') and scrubbed(boundary, 'access-2')
    await provider._register_connection(connection(2))

    assert not scrubbed(boundary, 'code-2')
    assert scrubbed(boundary, 'access-2')


@pytest.mark.asyncio
async def test_a_connection_without_tokens_registers_nothing(boundary, provider):
    await provider._register_connection(SimpleNamespace(access_token='', refresh_token=None, id_token=None))

    assert boundary._secrets == set()


@pytest.mark.asyncio
async def test_configured_and_retrieved_values_stay_scrubbed_across_refreshes(boundary, provider):
    await provider._register_secret('retrieved-password')

    await provider._register_connection(connection(1))
    await provider._register_connection(connection(2))

    assert scrubbed(boundary, 'retrieved-password')


@pytest.mark.asyncio
async def test_a_describe_that_began_before_a_refresh_still_scrubs_its_token(boundary, provider, monkeypatch):
    await provider._register_connection(connection(1))
    observed = []

    async def discover(**kwargs):
        await provider._register_connection(connection(2))
        async with provider._operation_credentials('access-2'):
            pass
        observed.append(scrubbed(boundary, 'access-1'))
        return ()

    monkeypatch.setattr(discovery, 'discover_selections', discover)

    await provider.adescribe(auth='access-1')

    assert observed == [True]
    assert not scrubbed(boundary, 'access-1')
    assert scrubbed(boundary, 'access-2')


@pytest.mark.asyncio
async def test_a_completion_that_began_before_a_refresh_still_scrubs_its_token(boundary, provider, monkeypatch):
    await provider._register_connection(connection(1))
    observed = []

    async def complete(**kwargs):
        await provider._register_connection(connection(2))
        async with provider._operation_credentials('access-2'):
            pass
        observed.append(scrubbed(boundary, 'access-1'))
        return 'completed'

    async def validate(**kwargs):
        pass

    monkeypatch.setattr(inference, 'complete_text', complete)
    monkeypatch.setattr(inference, 'async_validate_text_request', validate)

    assert await provider.acomplete(request(), auth='access-1') == 'completed'

    assert observed == [True]
    assert not scrubbed(boundary, 'access-1')


@pytest.mark.asyncio
async def test_a_failed_call_releases_its_hold(boundary, provider, monkeypatch):
    await provider._register_connection(connection(1))

    async def discover(**kwargs):
        raise RuntimeError('synthetic')

    monkeypatch.setattr(discovery, 'discover_selections', discover)

    with pytest.raises(RuntimeError, match='synthetic'):
        await provider.adescribe(auth='access-1')
    await provider._register_connection(connection(2))

    assert not scrubbed(boundary, 'access-1')


@pytest.mark.asyncio
async def test_a_call_without_a_credential_authority_registers_nothing(monkeypatch):
    provider = GPTProvider({})

    async def discover(**kwargs):
        return ()

    monkeypatch.setattr(discovery, 'discover_selections', discover)

    assert await provider.adescribe(auth='access-1') == ()


@pytest.mark.asyncio
async def test_closing_the_provider_ends_its_tokens(boundary, provider):
    await provider._register_connection(connection(1))

    await provider.aclose()

    assert boundary._secrets == set()


@pytest.mark.asyncio
async def test_closing_a_provider_that_never_registered_tokens_is_harmless(provider):
    await provider.aclose()
