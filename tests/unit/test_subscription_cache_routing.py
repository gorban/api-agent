"""Owned subscription affinity is stable, non-secret and absent on API-key routes."""
import asyncio
import json
from uuid import UUID

import httpx
import pytest

from api_agent.llm_providers.gpt import factory, inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent

KEY = 'f392d2ec-3e65-4a6d-b766-026425e77bf2'
REQUEST = LLMRequest('gpt-6-luna', CompletionRequest((Message('user', (TextContent('Say Paris'),)),)))


def response(subscription):
    data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'Paris'}]}],
            'usage': {'input_tokens': 4045, 'output_tokens': 5,
                      'input_tokens_details': {'cached_tokens': 3840, 'cache_write_tokens': 0}}}
    return (httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n',
                           headers={'content-type': 'text/event-stream'}) if subscription else httpx.Response(200, json=data))


@pytest.mark.parametrize('subscription,key,expected_affinity', [
    (True, KEY, {'prompt_cache_key': KEY}), (True, None, {}), (False, KEY, {}),
])
def test_wire_affinity_and_usage_preservation(subscription, key, expected_affinity):
    sent = []
    def handle(request):
        sent.append(request)
        return response(subscription)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token='synthetic-token', subscription=subscription, api_mode='responses', request=REQUEST,
                cache_session_id=key)
    result = asyncio.run(run())
    assert result.usage.cached_input_tokens == 3840 and result.usage.cache_write_tokens == 0
    body = json.loads(sent[0].content)
    assert {name: value for name, value in body.items() if name == 'prompt_cache_key'} == expected_affinity
    assert {name: value for name, value in sent[0].headers.items() if name == 'session-id'} == {
        'session-id': value for value in expected_affinity.values()}
    assert 'session_id' not in sent[0].headers and 'conversation_id' not in sent[0].headers
    assert 'prompt_cache_options' not in body and 'prompt_cache_retention' not in body
    assert body['input'] == [{'role': 'user', 'content': 'Say Paris'}]
    assert REQUEST.payload.messages[0].content == (TextContent('Say Paris'),)


@pytest.mark.parametrize('key', [True, 0, [], '', 'not-a-uuid', KEY.upper(), KEY.replace('-', ''), KEY+'\r\nextra: bad'])
def test_invalid_affinity_rejected_before_http(key):
    sent = []
    def handle(request):
        sent.append(request)
        return response(True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token='synthetic-token', subscription=True, api_mode='responses', request=REQUEST,
                cache_session_id=key)
    with pytest.raises(ProviderAccessError, match='canonical UUID'):
        asyncio.run(run())
    assert sent == []


@pytest.mark.parametrize('mode,expected_affinity_positions', [('subscription', (0, 0, 1)), ('api_key', ())])
def test_provider_reuses_affinity_across_calls_and_isolates_instances(mode, expected_affinity_positions, monkeypatch):
    sent, clients = [], []
    original = httpx.AsyncClient
    def handle(request):
        sent.append(request)
        return response(mode == 'subscription')
    def client(**kwargs):
        value = original(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(value)
        return value
    monkeypatch.setattr(factory.httpx, 'AsyncClient', client)
    provider = factory.GPTProvider({'GPT_AUTH_MODE': mode, 'GPT_API_MODE': 'responses'})
    other = factory.GPTProvider({'GPT_AUTH_MODE': mode, 'GPT_API_MODE': 'responses'})
    assert str(UUID(provider.cache_session_id)) == provider.cache_session_id
    assert other.cache_session_id != provider.cache_session_id
    async def run():
        for token in ('synthetic-token', 'synthetic-refreshed-token'):
            result = await provider.acomplete(REQUEST, auth=token)
            assert result.usage.cached_input_tokens == 3840
        await other.acomplete(REQUEST, auth='synthetic-other-token')
        await provider.aclose()
        await other.aclose()
    asyncio.run(run())
    assert len(clients) == len(sent) == 3 and all(value.is_closed for value in clients)
    keys = [json.loads(value.content).get('prompt_cache_key') for value in sent]
    identities = (provider.cache_session_id, other.cache_session_id)
    expected = [identities[position] for position in expected_affinity_positions]
    assert [value for value in keys if value is not None] == expected
    assert [value.headers['session-id'] for value in sent if 'session-id' in value.headers] == expected
    assert sum('prompt_cache_key' in json.loads(value.content) for value in sent) == len(expected)
