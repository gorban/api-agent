"""The ordinary provider owner admits native tool continuations across turns."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.inference import result_to_wire
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Tool, ToolResult


MODEL = 'us.openai.gpt-6-luna'
AUTH = dict(mode='api_key', api_key='synthetic-native-bearer', region='us-east-1', profile=None)


def native_answer():
    return dict(id='resp_fixture', model=MODEL, status='completed',
        output=[dict(type='reasoning', id='rs_fixture', summary=[], content=[],
                     encrypted_content='synthetic-cipher', status='completed'),
                dict(type='function_call', id='fc_fixture', call_id='call_fixture',
                     name='lookup', arguments='{}', status='completed')],
        usage=dict(input_tokens=4, output_tokens=2, total_tokens=6))


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_provider_replays_native_state_once_with_one_auth_snapshot_per_call(monkeypatch, streaming):
    from api_agent.llm_providers.bedrock import factory, mantle
    bodies = []
    class Transport:
        async def call(self, operation, payload, *, auth, join, on_dispatch):
            frozen = await auth()
            assert frozen.worker_config() == AUTH
            bodies.append(deepcopy(payload['body']))
            on_dispatch()
            if len(bodies) == 1:
                return native_answer()
            return dict(model=MODEL, status='completed', output=[dict(type='message', role='assistant',
                content=[dict(type='output_text', text='done')])],
                usage=dict(input_tokens=5, output_tokens=1, total_tokens=6))
        async def stream(self, operation, payload, **kwargs):
            raw = await self.call(operation, payload, **kwargs)
            result = mantle.decode(raw, 'responses', selected_llm=MODEL, endpoint='runtime')
            for part in result.payload.content:
                if part.text:
                    yield dict(kind='text', text=part.text)
            yield dict(kind='completed', result=result_to_wire(result))
        async def aclose(self):
            pass
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: Transport())
    provider = factory.BedrockProvider(dict(BEDROCK_ENDPOINT='runtime', BEDROCK_API_MODE='responses'))
    auth = AsyncMock(return_value=SimpleNamespace(worker_config=lambda: dict(AUTH)))
    user = Message('user', (TextContent('lookup'),))
    tool = Tool('lookup', 'Read synthetic data', '{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    request = LLMRequest(MODEL, CompletionRequest((user,), tools=(tool,)))
    async def execute(value):
        if streaming:
            return [event async for event in provider.astream(value, auth=auth)][-1].payload
        return await provider.acomplete(value, auth=auth)
    try:
        first = await execute(request)
        assert json.loads(first.payload.continuation)['provider'].startswith('bedrock.responses.')
        reply = Message('tool', results=(ToolResult('call_fixture', (TextContent('synthetic result'),)),))
        second = await execute(replace(request, payload=replace(request.payload,
            messages=(user, first.payload, reply))))
        assert second.payload.content == (TextContent('done'),)
        assert second.payload.continuation is None
        assert bodies[1]['input'][1:3] == native_answer()['output']
        assert bodies[1]['input'][3] == dict(type='function_call_output', call_id='call_fixture', output='synthetic result')
        assert len(bodies[1]['input']) == 4
        assert auth.await_count == 2
        assert bodies[0]['include'] == ['reasoning.encrypted_content']
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_refuses_wrong_identity_before_dispatch_and_retains_issue_usage(monkeypatch):
    from api_agent.llm_providers.bedrock import factory
    dispatched = []
    values = dict(AUTH)
    class Transport:
        async def call(self, operation, payload, *, auth, join, on_dispatch):
            await auth()
            dispatched.append(True)
            on_dispatch()
            return native_answer()
        async def aclose(self):
            pass
    monkeypatch.setattr(factory, 'SDKTransport', lambda **kw: Transport())
    provider = factory.BedrockProvider(dict(BEDROCK_ENDPOINT='runtime', BEDROCK_API_MODE='responses'))
    auth = AsyncMock(return_value=SimpleNamespace(worker_config=lambda: dict(values)))
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),),
        tools=(Tool('lookup', 'Read', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),)))
    try:
        first = await provider.acomplete(request, auth=auth)
        reply = Message('tool', results=(ToolResult('call_fixture', (TextContent('ok'),)),))
        replay = replace(request, payload=replace(request.payload, messages=(*request.payload.messages, first.payload, reply)))
        values['api_key'] = 'changed-credential'
        with pytest.raises(ProviderAccessError, match='different credentials'):
            await provider.acomplete(replay, auth=auth)
        assert dispatched == [True]
        values['mode'] = 'session'
        with pytest.raises(ProviderAccessError, match='frozen credentials') as caught:
            await provider.acomplete(request, auth=auth)
        assert caught.value.usage.input_tokens == 4
        assert caught.value.execution == 'possibly_executed'
        assert dispatched == [True, True]
    finally:
        await provider.aclose()
