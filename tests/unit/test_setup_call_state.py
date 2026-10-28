import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.llm_providers.gpt.inference import complete_text
from api_agent.providers.generation_attempts import begin_transport_attempt, finish_transport_attempt
from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, ResponsePolicy
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer

CAP = 'synthetic-rich-inference-capability-000000'
REQUEST = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Question'),)),)))
EMPTY = {'version': 1, 'complete': False, 'attempts': []}

@pytest.mark.parametrize('failure', ['invalid_request', 'closed'])
def test_direct_client_rejection_does_not_reuse_previous_attempts(failure):
    async def scenario():
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Answer', 'annotations': []}]}], 'usage': {'input_tokens': 7, 'output_tokens': 2}})
        async def complete(request):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                token = begin_transport_attempt(1)
                result = await complete_text(client=client, base_url='https://api.openai.com/v1', access_token='synthetic-token', subscription=False, api_mode='responses', request=request)
                finish_transport_attempt(token, result=result)
                return result
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        client = SetupClient(await server.start(), timeout=3)
        try:
            result = await asyncio.to_thread(client.complete, REQUEST)
            assert len(result.transport_attempts['attempts']) == 1
            if failure == 'closed':
                await asyncio.to_thread(client.close)
            with pytest.raises((ContractError, SessionClosedError)) as caught:
                await asyncio.to_thread(client.complete, None if failure == 'invalid_request' else REQUEST)
            assert caught.value.transport_attempts == EMPTY
            assert client.transport_attempts == EMPTY
            assert len(sent) == 1
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())



def test_remote_closed_loop_does_not_reuse_successful_call_evidence(monkeypatch):
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.testing import inference_content
    from api_agent.workflows import inference_capture

    request = LLMRequest('gpt-synthetic', CompletionRequest(
        (Message('user', (TextContent('Question'),)),), response=ResponsePolicy('text', 'reject')))
    async def scenario():
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'status': 'completed', 'output': [
                {'type': 'message', 'status': 'completed', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'Answer', 'annotations': []}]}],
                'usage': {'input_tokens': 7, 'output_tokens': 2}})
        async def complete(request):
            inference_capture.bind_boundary(SimpleNamespace(policy=SimpleNamespace(allow_content=False),
                                                            register_secret=lambda secret: None))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await complete_text(client=client, base_url='https://api.openai.com/v1',
                    access_token='synthetic-token', subscription=False, api_mode='responses', request=request)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        provider = RemoteIntegrationLLM(await server.start())
        try:
            result = await asyncio.to_thread(provider.complete, request)
            assert result.response is not None
            assert provider.client.response_evidence == result.response
            assert provider.client.evidence
            await asyncio.to_thread(provider.close)
            observed = []
            monkeypatch.setattr(inference_capture, 'capture_rows', lambda rows, call_id: observed.append(('capture', rows)))
            monkeypatch.setattr(inference_content, 'publish_content', lambda rows, call_id: observed.append(('publish', rows)))
            with pytest.raises(RuntimeError, match='Event loop is closed') as caught:
                await asyncio.to_thread(provider.complete, request)
            assert observed == [('capture', []), ('publish', [])]
            assert getattr(caught.value, 'response', None) is None
            assert len(sent) == 1
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())




def test_remote_running_loop_rejection_has_no_prior_attempts(monkeypatch):
    from api_agent.providers.llm import LLMResult
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.testing import inference_usage
    from api_agent.workflows import inference_capture

    publications = []
    captures = []
    monkeypatch.setattr(inference_usage, 'publish', lambda request, result=None, **kw: publications.append((request.request_id, kw)) or request.request_id)
    monkeypatch.setattr(inference_capture, 'capture_rows', lambda rows, call_id: captures.append((call_id, rows)))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def complete(request):
            token = begin_transport_attempt(1)
            calls.append(request.request_id)
            entered.set()
            await release.wait()
            result = LLMResult(Message('assistant', (TextContent('Answer'),)))
            finish_transport_attempt(token, result=result)
            return result
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        provider = RemoteIntegrationLLM(await server.start())
        first = LLMRequest('gpt-synthetic', REQUEST.payload, 'first-call')
        second = LLMRequest('gpt-synthetic', REQUEST.payload, 'second-call')
        try:
            active = asyncio.create_task(asyncio.to_thread(provider.complete, first))
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(RuntimeError, match='already running') as rejected:
                await asyncio.wait_for(asyncio.to_thread(provider.complete, second), 1)
            assert rejected.value.transport_attempts == EMPTY
            assert calls == ['first-call']
            assert ('second-call', []) in captures
            assert next(kw['error'] for call_id, kw in publications if call_id == 'second-call' and kw.get('failed')).transport_attempts == EMPTY
            release.set()
            result = await asyncio.wait_for(active, 2)
            assert result.transport_attempts['attempts'][0]['outcome'] == 'completed'
        finally:
            release.set()
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_remote_publishes_each_call_snapshot_after_later_call(monkeypatch):
    from threading import Event
    from api_agent.providers.llm import LLMResult, ResponseEvidence
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.testing import inference_usage
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    from api_agent.workflows import inference_capture

    first_at_publish, release_publish = Event(), Event()
    publications, captures = [], []
    def publish(request, result=None, **kw):
        publications.append((request.request_id, result, kw))
        return request.request_id
    monkeypatch.setattr(inference_usage, 'publish', publish)
    monkeypatch.setattr(inference_capture, 'capture_rows', lambda rows, call_id: captures.append((call_id, rows)))
    async def scenario():
        async def complete(request):
            inference_capture.bind_boundary(EmissionBoundary(ContentPolicy(True)))
            inference_capture.capture_inference('input', {'call': request.request_id}, request.request_id)
            response = ResponseEvidence('text', request.payload.response.fallback, 'text', None, None, None)
            inference_capture.record_response_dispatch(response)
            token = begin_transport_attempt(1)
            result = LLMResult(Message('assistant', (TextContent(request.request_id),)), response=response)
            finish_transport_attempt(token, result=result)
            return result
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
            quote_cost=lambda request, result: {'list_cost_usd': 1 if request.request_id == 'first-call' else 2, 'payment': None})
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        provider = RemoteIntegrationLLM(await server.start())
        original_request = provider._request
        request_count = [0]
        def request_with_barrier(call):
            request_count[0] += 1
            first_call = request_count[0] == 1
            result = original_request(call)
            if first_call:
                first_at_publish.set()
                assert release_publish.wait(3)
            return result
        monkeypatch.setattr(provider, '_request', request_with_barrier)
        def request(identifier, fallback):
            return LLMRequest('gpt-synthetic', CompletionRequest(REQUEST.payload.messages, response=ResponsePolicy('text', fallback)), identifier)
        try:
            first = asyncio.create_task(asyncio.to_thread(provider.complete, request('first-call', 'reject')))
            assert await asyncio.to_thread(first_at_publish.wait, 2)
            second = await asyncio.wait_for(asyncio.to_thread(provider.complete, request('second-call', 'allow')), 2)
            release_publish.set()
            first_result = await asyncio.wait_for(first, 2)
            assert first_result.response.fallback == 'reject' and second.response.fallback == 'allow'
            assert first_result.transport_attempts['attempts'][0]['id'] != second.transport_attempts['attempts'][0]['id']
            quotes = {call: kw['quote']['list_cost_usd'] for call, result, kw in publications if result is not None}
            assert quotes == {'first-call': 1, 'second-call': 2}
            rows = dict(captures)
            assert rows['first-call'][0]['content'] != rows['second-call'][0]['content']
        finally:
            release_publish.set()
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_remote_cancelled_offload_keeps_attempt_from_settled_worker(monkeypatch):
    from threading import Event
    from api_agent.providers.llm import LLMResult
    from api_agent.runtime.tasks import offload
    from api_agent.testing import inference_usage
    from api_agent.testing.provider_setup import RemoteIntegrationLLM

    failed = []
    monkeypatch.setattr(inference_usage, 'publish', lambda request, result=None, **kw: failed.append(kw) or 'call')
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = Event()
        async def complete(request):
            token = begin_transport_attempt(1)
            entered.set()
            await release.wait()
            result = LLMResult(Message('assistant', (TextContent('Answer'),)))
            finish_transport_attempt(token, result=result)
            return result
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('operation', 'synthetic-selection', lambda: owner)
        server = SetupServer(registry, CAP)
        provider = RemoteIntegrationLLM(await server.start())
        def request_with_cancellation(call):
            async def run():
                task = asyncio.create_task(offload(call))
                while not provider.client.transport_attempts or not provider.client.transport_attempts['attempts']:
                    await asyncio.sleep(.01)
                task.cancel()
                cancelled.set()
                return await task
            return asyncio.run(run())
        monkeypatch.setattr(provider, '_request', request_with_cancellation)
        try:
            active = asyncio.create_task(asyncio.to_thread(provider.complete, REQUEST))
            await asyncio.wait_for(entered.wait(), 2)
            assert await asyncio.to_thread(cancelled.wait, 2)
            assert not active.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(active, 2)
            error = failed[-1]['error']
            assert error.transport_attempts['attempts'][0]['outcome'] == 'completed'
            assert error.transport_attempts['complete'] is False
        finally:
            release.set()
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())
