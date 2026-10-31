"""Credential snapshots bind native state without exporting credential material."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.llm_providers.bedrock.continuation_scope import NativeScope
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, ToolCall, Usage


MODEL = 'us.openai.gpt-6-luna'
KEY = b'synthetic-owner-key'
AUTH = dict(mode='api_key', api_key='synthetic-bearer', profile=None, region='us-east-1')
STATE = [{'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture',
          'name': 'lookup', 'arguments': '{}', 'status': 'completed'}]


def request(message=None):
    return LLMRequest(MODEL, CompletionRequest((message or Message('user', (TextContent('hello'),)),)))


def result():
    state = json.dumps(dict(provider='bedrock.responses', state=STATE))
    return LLMResult(Message('assistant', (TextContent(''),),
        (ToolCall('call_fixture', 'lookup', '{}'),), continuation=state), Usage(4, 2), 'tool_calls')


async def admit(scope, values=AUTH):
    source = Mock(worker_config=Mock(return_value=dict(values)))
    auth = AsyncMock(return_value=source)
    frozen = await scope.wrap_auth(auth)()
    auth.assert_awaited_once_with()
    source.worker_config.assert_called_once_with()
    return frozen


@pytest.mark.asyncio
async def test_scope_roundtrip_freezes_one_snapshot_and_keeps_credentials_out_of_envelope():
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    frozen = await admit(scope)
    changed = frozen.worker_config()
    changed['api_key'] = 'other'
    assert frozen.worker_config() == AUTH
    assert repr(frozen) == '<FrozenBedrockWorkerAuth>'
    sealed = scope.seal(result())
    assert 'synthetic-bearer' not in sealed.payload.continuation
    assert json.loads(sealed.payload.continuation)['state'] == STATE
    replay = NativeScope(KEY, request(sealed.payload), 'runtime', 'responses')
    await admit(replay)


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['owner', 'model', 'endpoint', 'api', 'region', 'credential', 'state', 'unsigned'])
async def test_changed_scope_refuses_replay_before_dispatch(changed):
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    await admit(scope)
    sealed = scope.seal(result())
    replay_request, values = request(sealed.payload), dict(AUTH)
    key, endpoint, api = KEY, 'runtime', 'responses'
    if changed == 'owner':
        key = b'other-owner'
    elif changed == 'model':
        replay_request = replace(replay_request, selected_llm='us.openai.gpt-5.6-luna')
    elif changed == 'endpoint':
        endpoint = 'mantle'
    elif changed == 'api':
        api = 'completions'
    elif changed == 'region':
        values['region'] = 'us-west-2'
    elif changed == 'credential':
        values['api_key'] = 'other-key'
    else:
        envelope = json.loads(sealed.payload.continuation)
        if changed == 'state':
            envelope['state'][0]['arguments'] = '{"modified":true}'
        else:
            envelope['provider'] = 'bedrock.responses'
        replay_request = request(replace(sealed.payload, continuation=json.dumps(envelope)))
    replay = NativeScope(key, replay_request, endpoint, api)
    with pytest.raises(ProviderAccessError, match='different credentials'):
        await admit(replay, values)


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [dict(AUTH, mode='session'), dict(AUTH, region=None), dict(AUTH, region='invalid')])
async def test_unresolved_binding_refuses_issue_and_replay_but_not_final_text(values):
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    await admit(scope, values)
    final = LLMResult(Message('assistant', (TextContent('answer'),)))
    assert scope.seal(final) is final
    with pytest.raises(ProviderAccessError, match='frozen credentials') as caught:
        scope.seal(result())
    assert caught.value.usage == Usage(4, 2)
    assert caught.value.execution == 'possibly_executed'
    replay = NativeScope(KEY, request(result().payload), 'runtime', 'responses')
    with pytest.raises(ProviderAccessError, match='frozen credentials'):
        await admit(replay, values)


@pytest.mark.asyncio
async def test_scope_refuses_non_scalar_snapshot():
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    with pytest.raises(ContractError, match='credential snapshot'):
        await admit(scope, dict(AUTH, region=['us-east-1']))


@pytest.mark.asyncio
async def test_scope_concurrent_calls_keep_independent_identity():
    first, second = (NativeScope(KEY, request(), 'runtime', 'responses') for _ in range(2))
    await asyncio.gather(admit(first), admit(second, dict(AUTH, api_key='different-key')))
    one, two = first.seal(result()), second.seal(result())
    assert one.payload.continuation != two.payload.continuation
    await admit(NativeScope(KEY, request(one.payload), 'runtime', 'responses'))
    await admit(NativeScope(KEY, request(two.payload), 'runtime', 'responses'), dict(AUTH, api_key='different-key'))


@pytest.mark.asyncio
async def test_scope_refuses_already_sealed_publication_and_keeps_usage():
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    await admit(scope)
    with pytest.raises(ProviderAccessError, match='publication') as caught:
        scope.seal(scope.seal(result()))
    assert caught.value.usage == Usage(4, 2)


@pytest.mark.asyncio
async def test_scope_malformed_publication_keeps_execution_receipt():
    scope = NativeScope(KEY, request(), 'runtime', 'responses')
    await admit(scope)
    invalid = replace(result(), payload=replace(result().payload, continuation='not-json'))
    with pytest.raises(ContractError) as caught:
        scope.seal(invalid)
    assert caught.value.usage == Usage(4, 2)
    assert caught.value.execution == 'possibly_executed'
