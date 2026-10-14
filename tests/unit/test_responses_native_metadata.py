"""Synthetic Responses metadata acceptance, strict validation and native replay."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from api_agent.llm_providers.gpt import inference, rich
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_from_wire, request_to_wire, result_from_wire, result_to_wire
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Tool, ToolResult, Usage

TOKEN = 'synthetic-owned-auth-token'
SCHEMA = json.dumps({'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False})
TOOLS = (Tool('lookup', 'Lookup', SCHEMA),)
REQUEST = LLMRequest('gpt-synthetic', CompletionRequest((Message('user', (TextContent('Question'),)),), TOOLS))
MISSING = object()


def logprob():
    return {'token': 'Working', 'bytes': list(b'Working'), 'logprob': -.2,
            'top_logprobs': [{'token': 'Waiting', 'bytes': list(b'Waiting'), 'logprob': -2}]}


def native(logprobs=MISSING, phase=MISSING, *, reasoning=True):
    part = {'type': 'output_text', 'text': 'Working', 'annotations': []}
    if logprobs is not MISSING:
        part['logprobs'] = deepcopy(logprobs)
    message = {'type': 'message', 'id': 'message-1', 'status': 'completed',
               'role': 'assistant', 'content': [part]}
    if phase is not MISSING:
        message['phase'] = phase
    items = [message, {'type': 'function_call', 'id': 'function-1', 'status': 'completed',
                      'call_id': 'call-1', 'name': 'lookup', 'arguments': '{}'}]
    if reasoning:
        items.insert(0, {'type': 'reasoning', 'id': 'reason-1', 'summary': [],
                         'encrypted_content': 'opaque-synthetic-reasoning'})
    return {'status': 'completed', 'output': items, 'usage': {'input_tokens': 7, 'output_tokens': 3}}


def invoke(data, subscription, *, request=REQUEST, sent=None):
    async def scenario():
        def transport(req):
            if sent is not None:
                sent.append(json.loads(req.content))
            if subscription:
                events = 'data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n'
                return httpx.Response(200, text=events, headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                access_token=TOKEN, subscription=subscription, api_mode='responses', request=request)
    return asyncio.run(scenario())


@pytest.mark.parametrize('subscription', [False, True])
@pytest.mark.parametrize('logprobs', [MISSING, None, [], [logprob()]])
@pytest.mark.parametrize('phase', [MISSING, None, 'commentary', 'final_answer'])
def test_completed_metadata_preserves_usage_and_native_replay(subscription, logprobs, phase):
    data = native(logprobs, phase)
    result = invoke(data, subscription)
    assert result.outcome == 'tool_calls' and result.usage == Usage(7, 3)
    recovered = result_from_wire(result_to_wire(result))
    assert recovered == result
    followup = LLMRequest('gpt-synthetic', CompletionRequest(REQUEST.payload.messages + (
        recovered.payload, Message('tool', results=(ToolResult('call-1', (TextContent('Answer'),)),))), TOOLS))
    wire_request = request_from_wire(request_to_wire(followup))
    final = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'Done'}]}],
             'usage': {'input_tokens': 9, 'output_tokens': 4}}
    sent = []
    assert invoke(final, subscription, request=wire_request, sent=sent).usage == Usage(9, 4)
    assert sent[0]['input'][1:4] == data['output']
    assert sent[0]['input'][4] == {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'Answer'}
    assert TOKEN not in json.dumps(sent)


@pytest.mark.parametrize('subscription', [False, True])
def test_phase_replays_without_reasoning_item(subscription):
    data = native([], 'commentary', reasoning=False)
    result = invoke(data, subscription)
    assert json.loads(result.payload.continuation)['state'] == data['output']
    followup = LLMRequest('gpt-synthetic', CompletionRequest(REQUEST.payload.messages + (
        result.payload, Message('tool', results=(ToolResult('call-1', (TextContent('Answer'),)),))), TOOLS))
    assert rich.payload(followup, 'responses', subscription)['input'][1]['phase'] == 'commentary'


@pytest.mark.parametrize('phase', ['', 'analysis', True, 1, [], {}])
@pytest.mark.parametrize('subscription', [False, True])
def test_invalid_phase_preserves_paid_usage_and_execution_status(phase, subscription):
    with pytest.raises(ProviderAccessError) as caught:
        invoke(native([], phase), subscription)
    assert caught.value.usage == Usage(7, 3)
    assert caught.value.execution == 'possibly_executed'
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize('value', [True, {}, 'invalid', [None], [dict(logprob(), extra=1)],
    [dict(logprob(), token=1)], [dict(logprob(), token='x' * 4097)],
    [dict(logprob(), bytes=None)], [dict(logprob(), bytes=[0] * 4097)],
    [dict(logprob(), bytes=[-1])], [dict(logprob(), bytes=[256])],
    [dict(logprob(), bytes=[True])], [dict(logprob(), bytes=[1.0])],
    [dict(logprob(), logprob=True)], [dict(logprob(), logprob='-.2')],
    [dict(logprob(), logprob=float('nan'))], [dict(logprob(), logprob=float('inf'))],
    [dict(logprob(), logprob=10**400)], [dict(logprob(), top_logprobs={})],
    [dict(logprob(), top_logprobs=[{'token': '', 'bytes': [], 'logprob': 0}] * 21)],
    [dict(logprob(), top_logprobs=[{'token': '', 'bytes': [], 'logprob': 0, 'extra': 1}])],
    [logprob()] * 1025])
def test_malformed_or_unbounded_logprobs_rejected(value):
    with pytest.raises(ContractError, match='logprob'):
        rich._logprobs(value)


@pytest.mark.parametrize('subscription', [False, True])
def test_invalid_logprobs_shape_preserves_paid_usage(subscription):
    with pytest.raises(ProviderAccessError) as caught:
        invoke(native({'token': 'wrong container'}, 'commentary'), subscription)
    assert caught.value.usage == Usage(7, 3)
    assert caught.value.execution == 'possibly_executed'


def test_metadata_accepts_schema_typed_empty_token_and_integer_score():
    rich._logprobs([{'token': '', 'bytes': [], 'logprob': 0, 'top_logprobs': []}])


def test_opaque_metadata_is_not_exported_even_with_content_enabled():
    from api_agent.testing.inference_content import content_boundary, export_content
    result = invoke(native([logprob()], 'commentary'), False)
    exported = json.dumps(export_content(content_boundary({'LOG_CONTENT': True}), REQUEST, result))
    assert 'Working' in exported
    assert 'opaque-synthetic-reasoning' not in exported
    assert 'top_logprobs' not in exported and 'continuation' not in exported
    assert 'opaque-synthetic-reasoning' not in repr(result.payload)


def test_registered_auth_token_in_metadata_rejects_safely():
    row = logprob()
    row['top_logprobs'][0]['token'] = TOKEN
    with pytest.raises(ProviderAccessError) as caught:
        invoke(native([row], 'commentary'), False)
    assert caught.value.usage == Usage(7, 3)
    assert TOKEN not in str(caught.value)


def test_finished_and_terminal_phase_conflict_rejects_with_usage():
    data = native([], 'commentary')
    terminal = deepcopy(data)
    terminal['output'][1]['phase'] = 'final_answer'
    events = []
    for index, item in enumerate(data['output']):
        added = deepcopy(item)
        if item['type'] == 'function_call':
            added.update(status='in_progress', arguments='')
        events.extend([{'type': 'response.output_item.added', 'output_index': index, 'item': added},
                       {'type': 'response.output_item.done', 'output_index': index, 'item': item}])
    events.append({'type': 'response.completed', 'response': terminal})
    raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    with pytest.raises(ProviderAccessError, match='conflicts') as caught:
        inference._sse(raw, TOKEN, None)
    assert caught.value.usage == Usage(7, 3)
    assert caught.value.execution == 'possibly_executed'
