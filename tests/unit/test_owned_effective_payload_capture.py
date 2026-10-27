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
            return LLMResult(Message('assistant', (TextContent('synthetic result'),)), Usage(1, 1))

    return Adapter


def capture_request():
    return LLMRequest('synthetic-model', CompletionRequest((Message('user',
        (TextContent('authored-visible ' + SECRET),)),)))


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
