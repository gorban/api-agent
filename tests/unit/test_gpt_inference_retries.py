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


@pytest.mark.parametrize('allow', [False, True])
def test_retry_captures_observed_failure_body_per_attempt(monkeypatch, allow):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers import inference_retry
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference

    monkeypatch.setattr(inference_retry, 'retry_delay', lambda number, hint: 0)
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={'error': {'message': f'failure {len(calls)} {TOKEN}'},
                                            'usage': {'input_tokens': 2, 'output_tokens': 1},
                                            'hidden_reasoning': 'hidden-sentinel'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def physical(request, *, auth):
                return await inference.complete_text(client=client, base_url=BASE, access_token=auth,
                    subscription=True, api_mode='responses', request=request)
            async def call(callback):
                return await callback(auth=TOKEN)
            owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=call, aclose=AsyncMock()),
                                     complete=physical, max_attempts=2)
            req = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Say Ok'),)),)))
            ledger = TransportAttemptLedger(2)
            with collect_inference(EmissionBoundary(ContentPolicy(allow))) as collector:
                with bind_transport_attempt_ledger(ledger):
                    with pytest.raises(ProviderAccessError) as caught:
                        await owner.complete(req)
            await owner.aclose()
        rows = [row for row in collector['rows'] if row['phase'] == 'response']
        assert len(calls) == len(rows) == 2
        assert len({row['transport_attempt_id'] for row in rows}) == 2
        assert caught.value.http_status == 503
        serialized = json.dumps(rows)
        assert TOKEN not in serialized and 'hidden-sentinel' not in serialized
        assert all(row['reason'] == ('captured' if allow else 'withheld') for row in rows)
        if allow:
            assert [row['content']['error']['message'] for row in rows] == [
                'failure 1 [REDACTED]', 'failure 2 [REDACTED]']
            assert all(row['content']['usage']['input_tokens'] == 2 for row in rows)
    asyncio.run(scenario())


def test_success_captures_actual_response_instead_of_normalized_result():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    from test_gpt_inference import result, run

    body = result()
    body['provider_extra'] = 'actual provider field'
    body['hidden_reasoning'] = 'hidden-sentinel'
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        value, _ = run(httpx.Response(200, json=body))
    response = next(row for row in collector['rows'] if row['phase'] == 'response')
    assert value.payload.content[0].text == 'Ok'
    assert response['content']['provider_extra'] == 'actual provider field'
    assert response['content']['output'][0]['content'][0]['text'] == 'Ok'
    assert 'hidden_reasoning' not in response['content']


def test_stream_captures_observed_terminal_response():
    import json
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    from test_gpt_inference import result, run

    body = result()
    body['provider_extra'] = 'terminal field'
    stream = 'data: ' + json.dumps({'type': 'response.completed', 'response': body}) + '\n\n'
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        value, _ = run(httpx.Response(200, text=stream, headers={'content-type': 'text/event-stream'}), subscription=True)
    response = next(row for row in collector['rows'] if row['phase'] == 'response')
    assert value.payload.content[0].text == 'Ok'
    assert response['content']['provider_extra'] == 'terminal field'
