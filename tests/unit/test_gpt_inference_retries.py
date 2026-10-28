"""A GPT provider call is one physical attempt; the shared owner retries."""
import asyncio
import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
from test_gpt_inference import BASE, TOKEN

CODE = 'subscription_sharing_user_unavailable'


async def complete(handler):
    request = LLMRequest('gpt-example', CompletionRequest(
        (Message('user', (TextContent('Say Ok'),)),), generation=GenerationOptions()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN,
            subscription=True, api_mode='responses', request=request)


@pytest.mark.parametrize('body,execution', [
    ({'error': {'code': CODE, 'message': 'Unavailable'}}, 'not_started'),
    ({'error': {'code': CODE, 'message': {'unexpected': 1}}}, 'possibly_executed'),
    ({'error': {'code': CODE}, 'output': []}, 'possibly_executed'),
    ({}, 'possibly_executed'),
])
def test_rejection_is_one_attempt_and_admission_evidence_is_exact(body, execution):
    calls, responses = [], []
    def handler(request):
        calls.append(request)
        response = httpx.Response(503, json=body, headers={'retry-after': '3'})
        responses.append(response)
        return response
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))
    assert len(calls) == 1
    assert responses[0].is_closed
    assert caught.value.http_status == 503
    assert caught.value.retry_after == 3
    assert caught.value.execution == execution
    assert TOKEN not in str(vars(caught.value))


def test_bad_error_body_preserves_received_status():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'not-json'
    def handler(request):
        return httpx.Response(503, stream=Stream(), headers={'content-encoding': 'gzip'})
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))
    assert caught.value.http_status == 503
    assert caught.value.execution == 'possibly_executed'


def test_statusless_failure_is_not_given_http_status():
    def handler(request):
        raise httpx.ReadError('interrupted', request=request)
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))
    assert getattr(caught.value, 'http_status', None) is None
    assert caught.value.execution == 'possibly_executed'


def test_body_read_timeout_retains_http_status_and_closes_response():
    responses = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout('synthetic body timeout')
            yield b''
    def handler(request):
        response = httpx.Response(503, stream=Stream(), headers={'retry-after': '2'})
        responses.append(response)
        return response
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(complete(handler))
    assert caught.value.http_status == 503
    assert caught.value.retry_after == 2
    assert responses[0].is_closed


def test_shared_retry_after_body_timeout_waits_for_response_close(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers import inference_retry
    from api_agent.providers.shared_setup import SharedSetupOwner
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda number, hint: 0)
    async def scenario():
        responses, requests = [], []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadTimeout('body timeout')
                yield b''
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                response = httpx.Response(503, stream=Stream(), headers={'retry-after': '1'})
                responses.append(response)
                return response
            assert responses[0].is_closed
            return httpx.Response(503, json={'error': {'code': CODE}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def physical(request, *, auth):
                return await inference.complete_text(client=client, base_url=BASE, access_token=auth,
                    subscription=True, api_mode='responses', request=request)
            async def call(callback):
                return await callback(auth=TOKEN)
            owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=call, aclose=AsyncMock()),
                                     complete=physical, max_attempts=2)
            req = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Say Ok'),)),)))
            with pytest.raises(ProviderAccessError):
                await owner.complete(req)
            assert len(requests) == 2
            assert requests[0].content == requests[1].content
            await owner.aclose()
    asyncio.run(scenario())
