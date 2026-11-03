"""Actual native stream framing, rejection and ownership contracts."""
from copy import deepcopy
from types import SimpleNamespace
import json
import time

import pytest

from api_agent.llm_providers.bedrock import converse_stream as stream, worker
from api_agent.providers.contracts import ContractError, ProviderAccessError
from test_bedrock_runtime_worker import request, converse_response, CONVERSE_MODEL, native_events


def frames():
    return [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'text': 'A brief '}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'text': 'explanation.'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'signature': 'opaque-signature'}}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'text': 'Final '}}},
        {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'text': 'answer.'}}},
        {'contentBlockStop': {'contentBlockIndex': 1}},
        {'messageStop': {'stopReason': 'end_turn'}},
        {'metadata': {'usage': {'inputTokens': 2, 'outputTokens': 3, 'totalTokens': 5}, 'metrics': {'latencyMs': 1}}},
    ]


def test_native_fragments_preserve_spaces_signature_and_answer_separation():
    raw = stream.collect(frames(), time.monotonic() + 5)
    from api_agent.llm_providers.bedrock.converse import decode
    result = decode(raw, selected_llm=CONVERSE_MODEL)
    assert result.reasoning_summary == 'A brief explanation.'
    assert result.payload.content[0].text == 'Final answer.'
    assert json.loads(result.payload.continuation)['state'][0] == raw['output']['message']
    assert raw['metrics'] == {'latencyMs': 1}


def test_native_binary_and_tool_json_fragments_are_reassembled_before_decoding():
    events = [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'redactedContent': b'opaque-'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'redactedContent': bytearray(b'binary')}}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'contentBlockStart': {'contentBlockIndex': 1, 'start': {'toolUse': {'toolUseId': 'c', 'name': 'lookup'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'toolUse': {'input': '{"query":'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'toolUse': {'input': '"value"}'}}}},
        {'contentBlockStop': {'contentBlockIndex': 1}},
        {'messageStop': {'stopReason': 'tool_use'}},
        {'metadata': {'usage': {'inputTokens': 1, 'outputTokens': 2, 'totalTokens': 3}}},
    ]
    result = stream.collect(events, time.monotonic() + 5)
    content = result['output']['message']['content']
    assert content == [{'reasoningContent': {'redactedContent': b'opaque-binary'}},
                       {'toolUse': {'toolUseId': 'c', 'name': 'lookup', 'input': {'query': 'value'}}}]


@pytest.mark.parametrize('event', [None, {}, {'messageStart': [], 'private': True},
    {'messageStart': None}, {'messageStart': {'role': 'user'}},
    {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': 'before start'}}},
    {'contentBlockStop': {'contentBlockIndex': 0}}, {'messageStop': {'stopReason': 'end_turn'}},
    {'metadata': {'usage': {}}}, {'unknown': {}},
])
def test_invalid_stream_start_rejects_without_releasing_content(event):
    with pytest.raises(ProviderAccessError):
        stream.collect([event], time.monotonic() + 5)


@pytest.mark.parametrize('index,event', [
    (1, {'contentBlockStart': {'contentBlockIndex': True, 'start': {}}}),
    (1, {'contentBlockStart': {'contentBlockIndex': 1, 'start': {}}}),
    (1, {'contentBlockStart': {'contentBlockIndex': 64, 'start': {}}}),
    (1, {'contentBlockStart': {'contentBlockIndex': 0, 'start': []}}),
    (1, {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolUse': {'name': 'lookup'}}}}),
    (1, {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolUse': {'name': '', 'toolUseId': 'c'}}}}),
    (2, {'contentBlockStart': {'contentBlockIndex': 0, 'start': {}}}),
    (2, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': []}}),
    (2, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': True}}}),
    (2, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolUse': {'input': '{}'}}}}),
    (2, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'extra': 'no'}}}}),
    (2, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'redactedContent': 'not bytes'}}}}),
    (3, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': 'changed block kind'}}}),
    (3, {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'redactedContent': b'changed'}}}}),
    (5, {'contentBlockStop': {'contentBlockIndex': 1}}),
    (9, {'messageStop': {'stopReason': 7}}),
    (10, {'metadata': {'usage': None}}),
])
def test_invalid_or_conflicting_native_block_rejects(index, event):
    events = frames()
    events[index] = event
    with pytest.raises(ProviderAccessError):
        stream.collect(events, time.monotonic() + 5)


@pytest.mark.parametrize('events', [[], frames()[:-1], frames()[:4], frames() + [frames()[-1]],
                                  frames() + [{'messageStart': {'role': 'assistant'}}]])
def test_truncated_or_duplicate_terminal_state_rejects(events):
    with pytest.raises(ProviderAccessError):
        stream.collect(events, time.monotonic() + 5)


def test_native_stream_deadline_is_checked_during_collection():
    with pytest.raises(TimeoutError, match='deadline'):
        stream.collect(frames(), time.monotonic() - 1)


def test_native_stream_bounds_empty_event_flood_and_large_fragments(monkeypatch):
    monkeypatch.setattr(stream, '_MAX_EVENTS', 2)
    with pytest.raises(ProviderAccessError):
        stream.collect(frames(), time.monotonic() + 5)
    monkeypatch.setattr(stream, '_MAX_EVENTS', 100000)
    events = frames()
    events[2] = {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': 'x' * 524289}}}
    with pytest.raises(ProviderAccessError):
        stream.collect(events, time.monotonic() + 5)


@pytest.mark.parametrize('name,status', [('internalServerException', 500),
    ('modelStreamErrorException', 424), ('validationException', 400), ('throttlingException', 429), ('serviceUnavailableException', 503)])
def test_native_service_exception_retains_its_aws_error_category(name, status):
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError) as caught:
        stream.collect([{name: {'message': 'Synthetic service error'}}], time.monotonic() + 5)
    assert caught.value.response['ResponseMetadata']['HTTPStatusCode'] == status
    assert caught.value.response['Error']['Message'] == 'Synthetic service error'


@pytest.mark.parametrize('operation,maximum', [('converse_stream', 4096), ('converse', 64000)])
def test_worker_uses_real_sdk_stream_and_closes_it_for_large_thinking(operation, maximum):
    calls, output = [], []
    class Body:
        def __iter__(self):
            return iter(native_events(converse_response()))
        def close(self):
            calls.append('stream closed')
    def converse_stream(**kwargs):
        calls.append(kwargs)
        return {'stream': Body()}
    client = SimpleNamespace(converse=lambda **kwargs: pytest.fail('Used nonstreaming SDK'),
        converse_stream=converse_stream, close=lambda: calls.append('client closed'))
    body = {'modelId': CONVERSE_MODEL, 'messages': [], 'inferenceConfig': {'maxTokens': maximum},
            'additionalModelRequestFields': {'thinking': {'type': 'enabled', 'budget_tokens': 1024}}}
    worker.execute(request(operation, {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}),
        output.append, boto_client_factory=lambda *args: client)
    assert calls == [body, 'stream closed', 'client closed']
    assert output[0].get('result') if operation == 'converse' else output[-1]['event']['kind'] == 'completed'


def test_native_stream_preserves_service_tier_and_marks_prior_execution():
    events = frames()
    events[-1]['metadata'].update(serviceTier={'type': 'default'}, performanceConfig={'latency': 'standard'})
    observed = []
    result = stream.collect(events, time.monotonic() + 5, lambda: observed.append(True))
    assert result['serviceTier'] == {'type': 'default'}
    assert result['performanceConfig'] == {'latency': 'standard'}
    assert len(observed) == len(events)


def test_invalid_unicode_cannot_escape_native_stream_contract_diagnostics():
    events = frames()
    events[2]['contentBlockDelta']['delta'] = {'text': '\ud800'}
    with pytest.raises(ProviderAccessError) as caught:
        stream.collect(events, time.monotonic() + 5)
    assert caught.value.provider_diagnostics['validation_rule'] == 'text_value'


@pytest.mark.parametrize('key', ['usage', 'metrics', 'serviceTier', 'performanceConfig'])
def test_retained_metadata_shares_the_content_byte_budget(key):
    events = frames()
    events[-1]['metadata'][key] = {'synthetic': 'x' * 524289}
    with pytest.raises(ProviderAccessError):
        stream.collect(events, time.monotonic() + 5)


@pytest.mark.parametrize('message', ['Synthetic interrupted model', 'Synthetic interruption synthetic-runtime-key'])
def test_worker_preserves_retryable_model_stream_failure_and_closes(message):
    closed, output = [], []
    class Body:
        def __iter__(self):
            return iter([{'modelStreamErrorException': {'message': message}}])
        def close(self):
            closed.append('body')
    client = SimpleNamespace(converse_stream=lambda **kwargs: {'stream': Body()},
                             close=lambda: closed.append('client'))
    body = {'modelId': CONVERSE_MODEL, 'messages': []}
    worker.execute(request('converse_stream', {'endpoint': 'runtime', 'prefix': '/converse', 'body': body}),
                   output.append, boto_client_factory=lambda *args: client, join=lambda secrets, arguments: secrets)
    assert output[0]['error'] == 'unavailable'
    assert output[0]['http_status'] == 424
    assert output[0]['execution'] == 'possibly_executed'
    from api_agent.providers.inference_retry import retryable
    assert output[0]['model_stream_error'] is True
    assert 'synthetic-runtime-key' not in json.dumps(output)
    error = ProviderAccessError(output[0]['error'], 'Synthetic failure', status=424,
                                code='ModelStreamErrorException')
    error.http_status = 424
    assert retryable(error)
    assert closed == ['body', 'client']


@pytest.mark.parametrize('model', ['us.openai.gpt-6-luna', 'global.openai.gpt-6-astra'])
def test_worker_rejects_unqualified_native_schema_stream_before_authentication(model):
    from dataclasses import replace
    from test_bedrock_converse import request as llm_request, SCHEMA
    from api_agent.providers.llm import StructuredOutput
    from api_agent.llm_providers.bedrock.converse import encode
    body = encode(replace(llm_request(structured=StructuredOutput('answer', SCHEMA)), selected_llm=model))
    output = []
    worker.execute(request('converse_stream', {'endpoint': 'runtime', 'prefix': '/converse', 'body': body},
                           auth={'mode': 'session'}), output.append,
                   session_factory=lambda **kwargs: pytest.fail('Invalid stream selected credentials'))
    assert output == [{'error': 'unknown', 'execution': 'not_started'}]


@pytest.mark.parametrize('label', ['function', 'tool_use'])
def test_client_tool_stream_type_is_preserved_in_native_continuation(label):
    from api_agent.llm_providers.bedrock.converse import decode, continuation_message
    events = [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'text': 'Use the lookup tool.'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'signature': 'opaque-signature'}}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'contentBlockStart': {'contentBlockIndex': 1, 'start': {'toolUse': {
            'toolUseId': 'call-1', 'name': 'lookup', 'type': label}}}},
        {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'toolUse': {'input': '{"key":"sample"}'}}}},
        {'contentBlockStop': {'contentBlockIndex': 1}},
        {'messageStop': {'stopReason': 'tool_use'}},
        {'metadata': {'usage': {'inputTokens': 2, 'outputTokens': 3, 'totalTokens': 5}}},
    ]
    raw = stream.collect(events, time.monotonic() + 5)
    result = decode(raw, selected_llm=CONVERSE_MODEL)
    assert result.outcome == 'tool_calls'
    assert result.payload.calls[0].name == 'lookup'
    assert json.loads(result.payload.calls[0].arguments) == {'key': 'sample'}
    assert result.payload.continuation is not None
    assert continuation_message(result.payload, CONVERSE_MODEL) == raw['output']['message']
    assert raw['output']['message']['content'][1]['toolUse']['type'] == label


@pytest.mark.parametrize('label', ['server_tool_use', 'unknown', '', None, 7])
def test_stream_does_not_convert_unmanaged_or_invalid_tool_types_to_client_calls(label):
    events = [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolUse': {
            'toolUseId': 'call-1', 'name': 'lookup', 'type': label}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolUse': {'input': '{}'}}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'messageStop': {'stopReason': 'tool_use'}},
        {'metadata': {'usage': {'inputTokens': 2, 'outputTokens': 3, 'totalTokens': 5}}},
    ]
    with pytest.raises(ProviderAccessError):
        stream.collect(events, time.monotonic() + 5)
