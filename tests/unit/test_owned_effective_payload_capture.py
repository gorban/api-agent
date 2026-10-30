"""Production completion wrappers retain the provider's capture ownership."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_transport import SetupClient
from api_agent.runtime.local_llm import LocalLLM
from api_agent.testing import provider_setup
from api_agent.testing.inference_content import content_boundary
from api_agent.testing.setup_backend import SetupBackend
from api_agent.workflows.inference_capture import collect_inference


SECRET = 'synthetic-auth-secret'


@pytest.fixture
def synthetic_capture_provider():
    class Auth:
        async def __call__(self):
            return SECRET

        async def aclose(self):
            pass

    class Adapter:
        credential_protection_version = 1

        def bind_credential_authority(self, authority):
            self.authority = authority

        def __init__(self, effective_payload):
            self.captures_effective_payload = effective_payload
            self.calls = 0

        async def asetup(self, **kwargs):
            return Auth()

        async def acomplete(self, request, *, auth):
            self.calls += 1
            if self.captures_effective_payload:
                from api_agent.llm_providers.response_policy import capture_payload
                body = {'model': request.selected_llm, 'input': [
                    {'role': 'user', 'content': 'adapter-visible ' + auth}]}
                capture_payload(request, body, None, auth=auth)
                from api_agent.workflows.inference_capture import capture_inference
                capture_inference('output', {'provider_response': {'answer': 'adapter-result ' + auth}},
                                  request.request_id, auth=auth)
            return LLMResult(Message('assistant', (TextContent('synthetic result'),)), Usage(1, 1))

    return Adapter


def capture_request():
    return LLMRequest('synthetic-model', CompletionRequest((Message('user',
        (TextContent('authored-visible ' + SECRET),)),)))


def test_authoritative_large_request_replaces_fallback_before_ipc():
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference

    async def scenario():
        boundary = content_boundary({'LOG_CONTENT': True})
        async def authenticate(callback):
            return await callback(auth=SECRET)
        async def adapter(request, *, auth):
            capture_inference('input', {'provider_payload': {'text': 'b' * 2200000 + auth}},
                              request.request_id, auth=auth)
            return LLMResult(Message('assistant', (TextContent('ok'),)), Usage(1, 1))
        shared = SharedSetupOwner('operation', 'selected',
            lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
            complete=adapter, captures_effective_payload=False)
        async def complete(request):
            bind_boundary(boundary)
            return await shared.complete(request)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=shared.aclose)
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        server = SetupServer(registry, 'synthetic-capability-000000000000', timeout=30)
        address = await server.start()
        request = LLMRequest('selected', CompletionRequest(tuple(
            Message('user', (TextContent('a' * 500000),)) for _ in range(4))))
        def consume():
            client = SetupClient(address, timeout=30)
            try:
                ready = []
                result = client.complete(request, result_ready=ready.append)
                assert ready == [result]
                return result, client.evidence
            finally:
                client.close()
        try:
            result, rows = await asyncio.to_thread(consume)
            assert result.payload.content[0].text == 'ok'
            request_row = next(row for row in rows if row['phase'] == 'request')
            assert request_row['reason'] == 'captured'
            assert request_row['content']['provider_payload']['text'] == 'b' * 2200000 + '[REDACTED]'
            assert SECRET not in json.dumps(rows)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def assert_capture(rows, *, effective_payload, log):
    assert [row['phase'] for row in rows] == ['request', 'response']
    assert SECRET not in json.dumps(rows)
    assert all(row['reason'] == ('captured' if log else 'withheld') for row in rows)
    assert bool(rows[0]['content']) is log
    assert ('provider_payload' in rows[0]['content']) is (effective_payload and log)
    if log:
        if effective_payload:
            assert rows[0]['content']['provider_payload']['input'] == [
                {'role': 'user', 'content': 'adapter-visible [REDACTED]'}]
            assert rows[1]['content']['provider_response']['answer'] == 'adapter-result [REDACTED]'
        else:
            assert rows[0]['content']['messages'][0]['text'] == 'authored-visible [REDACTED]'


@pytest.mark.parametrize('effective_payload', [False, True])
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_integration_host_ipc_preserves_effective_or_generic_capture(tmp_path, monkeypatch,
                                                                  synthetic_capture_provider, effective_payload, log, trace):
    implementation = synthetic_capture_provider(effective_payload)
    loaded = SimpleNamespace(implementation=implementation, aclose=AsyncMock())
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=loaded))
    backend = SetupBackend({'LLM_PROVIDER': 'synthetic', 'LOG_CONTENT': log,
        'TRACELOOP_TRACE_CONTENT': trace, 'NO_STD_IN': True}, tmp_path)
    client = None
    try:
        client = SetupClient(backend.start(), timeout=5)
        assert client.complete(capture_request()).usage == Usage(1, 1)
        assert_capture(client.evidence, effective_payload=effective_payload, log=log)
        attempts = client.transport_attempts
        assert attempts['complete'] and len(attempts['attempts']) == 1
        attempt_id = attempts['attempts'][0]['id']
        if effective_payload and log:
            assert all(row['transport_attempt_id'] == attempt_id for row in client.evidence)
            assert all(row['capture_source'] == 'provider' for row in client.evidence)
        assert implementation.calls == 1
    finally:
        if client is not None:
            client.close()
        backend.close()
    loaded.aclose.assert_awaited_once()


@pytest.mark.parametrize('effective_payload', [False, True])
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_local_owner_preserves_effective_or_generic_capture(tmp_path, synthetic_capture_provider,
                                                          effective_payload, log, trace):
    async def scenario():
        implementation = synthetic_capture_provider(effective_payload)
        loaded = SimpleNamespace(implementation=implementation, aclose=AsyncMock())
        config = {'LLM_PROVIDER': 'synthetic', 'LOG_CONTENT': log,
                  'TRACELOOP_TRACE_CONTENT': trace, 'NO_STD_IN': True}
        boundary = content_boundary(config)
        owner = LocalLLM(config, tmp_path, emissions=boundary,
                         loader=AsyncMock(return_value=loaded))
        try:
            with collect_inference(boundary) as collector:
                assert (await owner.complete(capture_request())).usage == Usage(1, 1)
            assert_capture(collector['rows'], effective_payload=effective_payload, log=log)
            assert implementation.calls == 1
        finally:
            await owner.aclose()
        loaded.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_remote_integration_background_finalizes_large_provider_evidence(monkeypatch):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing import inference_content
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference

    source = EmissionBoundary(ContentPolicy(True), secrets=(SECRET,))
    coordinator = EmissionBoundary(ContentPolicy(True))
    expected = LLMResult(Message('assistant', (TextContent('ok'),)), Usage(1, 1))
    visible = 'z' * 3_900_000
    published = []
    monkeypatch.setattr(inference_content, 'publish_content', lambda rows, call_id: published.extend(rows))

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'provider_payload': {'text': visible + SECRET}}, request.request_id)
            return expected

    async def scenario():
        registry = SetupRegistry('operation', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-capability-000000000000', timeout=30, authority=coordinator)
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 30})
        try:
            with authority_scope(('coordinator',)):
                assert await asyncio.to_thread(provider.complete, capture_request()) == expected
                await asyncio.to_thread(provider.close)
            request_row = next(row for row in published if row['phase'] == 'request')
            assert request_row['reason'] == 'captured'
            assert request_row['content']['provider_payload']['text'] == visible + '[REDACTED]'
            assert SECRET not in json.dumps(published)
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())
