"""Mantle tool metadata survives the maintained SDK without weakening calls."""
from copy import deepcopy

import httpx
import pytest

from api_agent.llm_providers.bedrock import mantle
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Tool, ToolCall, ToolResult, Usage

MODEL = 'openai.gpt-oss-20b'


def native():
    return {'id': 'response-synthetic', 'created_at': 0, 'object': 'response',
        'model': MODEL, 'status': 'completed', 'error': None, 'incomplete_details': None,
        'output': [
            {'id': 'reasoning-synthetic', 'type': 'reasoning', 'status': 'completed',
             'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'Hidden synthetic reasoning'}]},
            {'type': 'function_call', 'status': 'completed', 'call_id': 'call-synthetic',
             'name': 'lookup', 'arguments': '{"query":"hello"}', 'item_id': 'item-synthetic', 'output_index': 1}],
        'usage': {'input_tokens': 244, 'output_tokens': 54, 'total_tokens': 298,
                  'input_tokens_details': {'cached_tokens': 0},
                  'output_tokens_details': {'reasoning_tokens': 37}}}


def sdk_shape():
    from openai import OpenAI
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=native()))) as http:
        with OpenAI(api_key='synthetic', base_url='https://example.invalid/v1', http_client=http, max_retries=0) as client:
            return client.responses.create(model=MODEL, input='Synthetic question').model_dump(exclude_unset=True)


def test_sdk_item_identity_and_position_preserve_call_and_followup():
    raw = sdk_shape()
    assert raw['output'][1]['item_id'] == 'item-synthetic'
    assert raw['output'][1]['output_index'] == 1
    result = mantle.decode(raw, 'responses', selected_llm=MODEL)
    assert result.payload.calls == (ToolCall('call-synthetic', 'lookup', '{"query":"hello"}'),)
    assert result.usage == Usage(244, 54, 0, 37)
    assert result.outcome == 'tool_calls'
    assert result.payload.continuation is None
    followup = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Question'),)),
        result.payload, Message('tool', results=(ToolResult('call-synthetic', (TextContent('Result'),)),))),
        tools=(Tool('lookup', 'Synthetic lookup', '{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":false}'),)))
    items = mantle.encode(followup, 'responses')['input']
    assert items[2] == {'type': 'function_call', 'call_id': 'call-synthetic', 'name': 'lookup', 'arguments': '{"query":"hello"}'}
    assert items[3] == {'type': 'function_call_output', 'call_id': 'call-synthetic', 'output': 'Result'}
    assert 'Hidden' not in repr(result)


@pytest.mark.parametrize('changes', [
    {'item_id': ''}, {'item_id': False}, {'id': False},
    {'output_index': True}, {'output_index': -1}, {'output_index': 64},
    {'call_id': ''}, {'arguments': '[]'}, {'future_execution': True}, {'status': 'in_progress'}])
def test_invalid_metadata_or_call_semantics_fail_with_observed_usage(changes):
    raw = native()
    raw['output'][1].update(changes)
    with pytest.raises(ProviderAccessError) as caught:
        mantle.decode(raw, 'responses', selected_llm=MODEL)
    assert caught.value.usage == Usage(244, 54, 0, 37)
    assert caught.value.execution == 'possibly_executed'


@pytest.mark.parametrize('metadata', [{'id': 'item-synthetic'}, {'id': 'separate-sdk-id'}, {'output_index': 0}, {'item_id': None, 'output_index': None}, {}])
def test_supported_optional_metadata_has_no_call_semantics(metadata):
    raw = native()
    raw['output'][1].update(metadata)
    assert mantle.decode(raw, 'responses', selected_llm=MODEL).payload.calls[0].call_id == 'call-synthetic'


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': 244, 'output_tokens': 54, 'total_tokens': 299}])
def test_failed_decode_never_invents_or_reuses_invalid_usage(usage):
    raw = native()
    raw['usage'] = deepcopy(usage)
    raw['output'][1]['call_id'] = ''
    with pytest.raises(ProviderAccessError) as caught:
        mantle.decode(raw, 'responses', selected_llm=MODEL)
    assert caught.value.execution == 'possibly_executed'
    assert getattr(caught.value, 'usage', None) is None


def test_stream_metadata_preserves_identity_until_validated_eof():
    from test_bedrock_mantle import response_stream
    events = response_stream(True)
    for event in events:
        if 'item' in event:
            event['item'].update(item_id='fc', output_index=0)
        if event['type'] == 'response.completed':
            event['response']['output'][0].update(item_id='fc', output_index=0)
    decoder = mantle.StreamDecoder('responses', selected_llm=MODEL)
    for event in events:
        assert decoder.feed(event) == ()
    assert decoder.finish().payload.calls == (ToolCall('c', 'lookup', '{}'),)


def test_invalid_stream_terminal_retains_observed_usage_without_tools():
    from test_bedrock_mantle import response_stream
    events = response_stream(True)
    # The helper intentionally shares done/terminal items; isolate this terminal.
    events[-1]['response'] = deepcopy(events[-1]['response'])
    events[-1]['response']['output'][0]['output_index'] = True
    decoder = mantle.StreamDecoder('responses', selected_llm=MODEL)
    for event in events[:-1]:
        assert decoder.feed(event) == ()
    with pytest.raises(ProviderAccessError) as caught:
        decoder.feed(events[-1])
    assert caught.value.usage == Usage(2, 3)
    assert caught.value.execution == 'possibly_executed'
