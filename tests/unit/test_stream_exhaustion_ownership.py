"""Normal consumption releases the loaded provider's stream lease."""
import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock
import weakref

import pytest

from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, StreamEvent, TextContent
from api_agent.providers.loading import LoadedProvider
from api_agent.providers import loading


def joined(provider):
    # These lifecycle fakes acquire no credentials and emit fixed text only.
    provider.credential_protection_version = 1
    provider.bind_credential_authority = lambda authority: setattr(provider, 'authority', authority)
    return provider


def test_exhaustion_releases_request_and_auth_while_owner_and_stream_survive():
    async def scenario():
        async def events(request, *, auth):
            yield StreamEvent(TextContent('answer'))

        provider = SimpleNamespace(capabilities=ProviderCapabilities('llm', ('stream',)),
                                   astream=events, aclose=AsyncMock())
        owner = LoadedProvider(joined(provider), 'synthetic-exhaustion')
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
        class Auth:
            pass
        auth = Auth()
        request_ref, auth_ref = weakref.ref(request), weakref.ref(auth)
        stream = owner.require('llm', 'stream').astream(request, auth=auth)
        del request, auth

        assert [event.payload.text async for event in stream] == ['answer']
        gc.collect()

        assert not owner._generation_streams
        assert request_ref() is None and auth_ref() is None
        assert not owner.closed
        provider.aclose.assert_not_awaited()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        await owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_read_failure_preserves_lease_until_provider_cleanup(failure):
    async def scenario():
        class Iterator:
            def __aiter__(self): return self
            async def __anext__(self): raise failure()
            aclose = AsyncMock()
        iterator = Iterator()
        owner = LoadedProvider(joined(SimpleNamespace(capabilities=ProviderCapabilities('llm', ('stream',)),
                               astream=lambda *args, **kwargs: iterator)), 'synthetic-read-failure')
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
        auth = object()
        stream = owner.require('llm', 'stream').astream(request, auth=auth)

        with pytest.raises(failure):
            await anext(stream)

        assert stream in owner._generation_streams
        assert stream.request is request and stream.auth is auth
        iterator.aclose.assert_not_awaited()
        await owner.aclose()
        iterator.aclose.assert_awaited_once()
        assert not owner._generation_streams
        assert stream.request is None and stream.auth is None and stream.iterator is None

    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_exhaustion_keeps_failed_cleanup_owned_until_successful_retry(failure, monkeypatch):
    async def scenario():
        class Selection:
            pass
        selection = Selection()
        selection_ref = weakref.ref(selection)
        validate = AsyncMock(return_value=selection)
        monkeypatch.setattr(loading, '_validate_model', validate)
        iterator = SimpleNamespace(__anext__=AsyncMock(side_effect=StopAsyncIteration),
                                   aclose=AsyncMock(side_effect=[failure(), None]))
        # Special methods are resolved on the type, rather than the instance.
        class Iterator:
            def __aiter__(self): return self
            async def __anext__(self): return await iterator.__anext__()
            async def aclose(self): await iterator.aclose()
        provider = SimpleNamespace(capabilities=ProviderCapabilities('llm', ('stream',)),
                                   astream=lambda *args, **kwargs: Iterator())
        owner = LoadedProvider(joined(provider), 'synthetic-exhaustion-retry')
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
        auth = object()
        stream = owner.require('llm', 'stream').astream(request, auth=auth)

        with pytest.raises(failure):
            await anext(stream)

        assert stream in owner._generation_streams
        assert stream.request is request and stream.auth is auth
        assert stream.iterator.selection is selection
        validate.reset_mock(return_value=True)
        del selection
        await stream.aclose()
        iterator.aclose.side_effect = None
        gc.collect()

        assert not owner._generation_streams
        assert stream.iterator is None and stream.request is None and stream.auth is None
        assert selection_ref() is None
        assert iterator.aclose.await_count == 2
        await stream.aclose()
        assert iterator.aclose.await_count == 2
        await owner.aclose()

    asyncio.run(scenario())
