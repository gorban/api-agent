"""Credential protection retains usage from an observed successful dispatch."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, ResponsePolicy, TextContent
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import EmissionBoundary
from test_gpt_inference import BASE, TOKEN


@pytest.mark.parametrize('stream,stage', [(False, 'raw'), (False, 'result'), (False, 'release'),
                                          (True, 'result'), (True, 'release')])
def test_guarded_success_keeps_usage_and_status_in_attempt(monkeypatch, stream, stage):
    secret = 'synthetic-late-authority-secret'
    authority = EmissionBoundary()
    original = inference._guard_content
    target = {'raw': 3, 'result': 3 if stream else 4, 'release': 4 if stream else 5}[stage]
    count = 0
    async def guard(*args, **kwargs):
        nonlocal count
        count += 1
        if count == target:
            authority.register_secret(secret)
        return await original(*args, **kwargs)
    monkeypatch.setattr(inference, '_guard_content', guard)
    body = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': secret}]}],
            'usage': {'input_tokens': 7, 'output_tokens': 3}}
    calls = []
    def respond(request):
        calls.append(request)
        if stream:
            return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': body}) + '\n\n',
                                  headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=body)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async def authenticate(callback):
                return await callback(auth=TOKEN)
            async def physical(request, *, auth):
                return await inference.complete_text(client=client, base_url=BASE, access_token=auth,
                    subscription=stream, api_mode='responses', request=request, authority=authority)
            owner = SharedSetupOwner('operation', 'selected', lambda _: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
                                     complete=physical, max_attempts=2)
            request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Safe'),)),),
                                                                  response=ResponsePolicy('text', 'reject')))
            ledger = TransportAttemptLedger(2)
            try:
                with bind_transport_attempt_ledger(ledger):
                    with pytest.raises(ProviderAccessError) as raised:
                        await owner.complete(request)
                return raised.value, ledger.snapshot()
            finally:
                await owner.aclose()
    error, ledger = asyncio.run(scenario())
    assert len(calls) == 1
    assert secret not in str(error) and secret not in str(vars(error))
    assert error.usage.input_tokens == 7 and error.usage.output_tokens == 3
    attempt = ledger['attempts'][0]
    assert len(ledger['attempts']) == 1 and attempt['status'] == 200
    assert attempt['usage']['input_tokens'] == 7 and attempt['usage']['output_tokens'] == 3
    assert attempt['response']['actual_mode'] == 'text'
