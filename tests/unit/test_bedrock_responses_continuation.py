"""Synthetic stateless Responses replay; native state is never report content."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from api_agent.llm_providers.bedrock import mantle
from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.inference import result_from_wire, result_to_wire
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Tool, ToolCall, ToolResult
from test_bedrock_mantle import MODEL, call, message, response

CIPHER = 'opaque-completed-state'
SCHEMA = '{"type":"object","properties":{},"required":[],"additionalProperties":false}'


def native_output():
    return [{'type': 'reasoning', 'id': 'reason-1', 'status': 'completed',
             'summary': [], 'encrypted_content': CIPHER},
            {**message('Looking up.'), 'phase': 'commentary'}, call(True)]


def decoded_output(items=None):
    return mantle.decode(response(native_output() if items is None else items), 'responses', selected_llm=MODEL)


def correction_request(assistant):
    messages = (Message('user', (TextContent('Find the public record.'),)), assistant,
                Message('tool', results=(ToolResult('c', (TextContent('Public tool result.'),)),)))
    return LLMRequest(MODEL, CompletionRequest(messages, tools=(Tool('lookup', 'Public lookup', SCHEMA),)))


def stream_events(items=None, preliminary=None):
    items = native_output() if items is None else items
    events = [{'type': 'response.created', 'response': {'id': 'r', 'status': 'in_progress'}}]
    for index, item in enumerate(items):
        start = deepcopy(item)
        start['status'] = 'in_progress'
        if item['type'] == 'reasoning':
            start.pop('encrypted_content', None)
            if preliminary is not None:
                start['encrypted_content'] = preliminary
        elif item['type'] == 'message':
            start['content'] = []
        else:
            start['arguments'] = ''
        events.append({'type': 'response.output_item.added', 'output_index': index, 'item': start})
        base = {'output_index': index, 'item_id': item['id']}
        if item['type'] == 'message':
            for position, part in enumerate(item['content']):
                scoped = {**base, 'content_index': position}
                events.extend([{**scoped, 'type': 'response.content_part.added', 'part': dict(part, text='')},
                    {**scoped, 'type': 'response.output_text.delta', 'delta': part['text']},
                    {**scoped, 'type': 'response.output_text.done', 'text': part['text']},
                    {**scoped, 'type': 'response.content_part.done', 'part': part}])
        elif item['type'] == 'function_call':
            events.extend([{**base, 'type': 'response.function_call_arguments.delta', 'delta': item['arguments']},
                {**base, 'type': 'response.function_call_arguments.done', 'arguments': item['arguments']}])
        events.append({'type': 'response.output_item.done', 'output_index': index, 'item': deepcopy(item)})
    events.append({'type': 'response.completed', 'response': response(items)})
    return [{**event, 'sequence_number': index} for index, event in enumerate(events)]


def test_buffered_native_state_replays_exactly_once_before_tool_output():
    result = decoded_output()
    wire = result_to_wire(result)
    assert result_from_wire(wire) == result
    assert json.loads(result.payload.continuation)['state'] == native_output()
    body = mantle.encode(correction_request(result.payload), 'responses')
    assert body['store'] is False and body['include'] == ['reasoning.encrypted_content']
    assert body['input'][1:4] == native_output()
    assert body['input'][4] == {'type': 'function_call_output', 'call_id': 'c', 'output': 'Public tool result.'}
    assert len(body['input']) == 5
    assert body['input'][2]['phase'] == 'commentary'


@pytest.mark.parametrize('provider', ['bedrock.responses', 'bedrock.responses.' + 'a'*64])
def test_codec_admits_bare_or_owner_sealed_provider_tag_without_interpreting_seal(provider):
    result = decoded_output()
    envelope = json.loads(result.payload.continuation)
    envelope['provider'] = provider
    assistant = replace(result.payload, continuation=json.dumps(envelope))
    assert mantle.continuation_state(assistant) == native_output()


@pytest.mark.parametrize('change', ['visible_text', 'call_id', 'call_name', 'call_arguments', 'provider', 'seal'])
def test_mismatched_native_replay_is_rejected_before_request_encoding(change):
    result = decoded_output()
    assistant = result.payload
    envelope = json.loads(assistant.continuation)
    if change == 'visible_text':
        assistant = replace(assistant, content=(TextContent('Different visible text'),))
    elif change.startswith('call_'):
        field = {'call_id': 'call_id', 'call_name': 'name', 'call_arguments': 'arguments'}[change]
        assistant = replace(assistant, calls=(replace(assistant.calls[0], **{field: 'different' if field != 'arguments' else '{"other":true}'}),))
    else:
        envelope['provider'] = 'openai.responses' if change == 'provider' else 'bedrock.responses.' + 'A'*64
        assistant = replace(assistant, continuation=json.dumps(envelope))
    with pytest.raises(ProviderAccessError):
        mantle.continuation_state(assistant)
    with pytest.raises(ProviderAccessError):
        mantle.encode(correction_request(assistant), 'responses')


@pytest.mark.parametrize('cipher', ['', 1, 'x'*32769, 'line\nbreak'])
def test_malformed_cipher_fails_with_observed_usage(cipher):
    output = native_output()
    output[0]['encrypted_content'] = cipher
    with pytest.raises(ProviderAccessError) as caught:
        decoded_output(output)
    assert caught.value.usage.input_tokens == 2 and caught.value.usage.output_tokens == 3


@pytest.mark.parametrize('field,content', [('summary', [{'type': 'summary_text', 'text': 'Do not replay hidden text'}]),
    ('content', [{'type': 'reasoning_text', 'text': 'Do not replay hidden text'}])])
def test_plaintext_reasoning_is_not_retained_in_native_tool_continuation(field, content):
    output = native_output()
    output[0][field] = content
    with pytest.raises(ProviderAccessError):
        decoded_output(output)


@pytest.mark.parametrize('index,change', [(0, {'unknown': True}), (1, {'unknown': True}),
    (2, {'caller': {'type': 'program'}}), (1, {'phase': 'unknown'}),
    (1, {'id': 'reason-1'}), (2, {'status': 'in_progress'})])
def test_unsupported_native_state_or_metadata_is_not_silently_discarded(index, change):
    output = native_output()
    output[index].update(change)
    with pytest.raises(ProviderAccessError):
        decoded_output(output)


def test_final_text_accepts_cipher_but_deliberately_has_no_continuation():
    result = decoded_output(native_output()[:2])
    assert result.payload.content == (TextContent('Looking up.'),)
    assert result.payload.continuation is None
    assert result_from_wire(result_to_wire(result)) == result


@pytest.mark.parametrize('preliminary', [None, '', 'opaque-partial-state', CIPHER])
def test_stream_uses_only_validated_completed_cipher_and_replays_exact_native_output(preliminary):
    decoder = WorkerStream('responses', MODEL, [])
    frames = []
    for event in stream_events(preliminary=preliminary):
        produced = decoder.feed(event)
        assert CIPHER not in json.dumps(produced)
        assert 'continuation' not in json.dumps(produced)
        frames.extend(produced)
    assert all(frame['kind'] != 'completed' for frame in frames)
    frames.extend(decoder.finish())
    final = result_from_wire(frames[-1]['result'])
    assert json.loads(final.payload.continuation)['state'] == native_output()
    body = mantle.encode(correction_request(final.payload), 'responses')
    assert body['input'][1:4] == native_output()


@pytest.mark.parametrize('field,value', [('id', 'other'), ('summary', [{'type': 'summary_text', 'text': 'changed'}]),
    ('content', []), ('status', 'incomplete'), ('encrypted_content', 'bad\ncipher')])
def test_stream_item_done_allows_only_valid_completed_cipher_change(field, value):
    events = stream_events(preliminary='partial')
    done = next(event for event in events if event['type'] == 'response.output_item.done')
    done['item'][field] = value
    decoder = mantle.StreamDecoder('responses', selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:
            decoder.feed(event)
        decoder.finish()


def test_stream_terminal_cipher_must_equal_the_completed_native_item():
    events = stream_events()
    events[-1]['response']['output'][0]['encrypted_content'] = 'different-final-state'
    decoder = mantle.StreamDecoder('responses', selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:
            decoder.feed(event)
        decoder.finish()


@pytest.mark.parametrize('log_content,trace_content', [(False, False), (True, False), (False, True), (True, True)])
def test_native_continuation_and_cipher_are_excluded_from_capture(log_content, trace_content):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference, capture_inference
    result = decoded_output()
    from types import SimpleNamespace
    from api_agent.workflows.inference_capture import bind_inference_trace
    trace_boundary = EmissionBoundary(ContentPolicy(trace_content))
    owner = SimpleNamespace(begin=lambda call_id: SimpleNamespace(boundary=trace_boundary))
    with collect_inference(EmissionBoundary(ContentPolicy(log_content))) as logged:
        bind_inference_trace(owner)
        capture_inference('output', {'provider_response': response(native_output()), 'message': {
            'text': 'Looking up.', 'continuation': result.payload.continuation}}, 'public-call')
    traced = logged['trace_collector']
    for observed, permitted in ((logged, log_content), (traced, trace_content)):
        dumped = json.dumps(observed['rows'])
        assert CIPHER not in dumped and 'bedrock.responses' not in dumped
        assert ('Looking up.' in dumped) is permitted


@pytest.mark.parametrize('logprobs', [None, [], [{'token': 'Look', 'bytes': [76, 111, 111, 107],
    'logprob': -0.1, 'top_logprobs': []}]])
def test_documented_message_logprob_metadata_is_preserved_exactly(logprobs):
    output = native_output()
    output[1]['content'][0]['logprobs'] = logprobs
    result = decoded_output(output)
    assert json.loads(result.payload.continuation)['state'] == output
    assert mantle.encode(correction_request(result.payload), 'responses')['input'][1:4] == output


def test_phase_and_logprob_metadata_without_reasoning_still_replays_natively():
    output = native_output()[1:]
    output[0]['content'][0]['logprobs'] = []
    result = decoded_output(output)
    assert json.loads(result.payload.continuation)['state'] == output
    assert mantle.continuation_state(result.payload) == output


@pytest.mark.parametrize('field', ['namespace', 'async', 'caller'])
def test_documented_nullable_function_metadata_is_preserved_as_absence(field):
    output = native_output()
    output[2][field] = None
    result = decoded_output(output)
    assert mantle.continuation_state(result.payload) == output


def test_invalid_logprob_metadata_is_not_silently_dropped():
    output = native_output()
    output[1]['content'][0]['logprobs'] = [{'token': 'Look', 'bytes': [], 'logprob': '-.1', 'top_logprobs': []}]
    with pytest.raises(ProviderAccessError):
        decoded_output(output)


def test_native_replay_rejects_unknown_state_kind_instead_of_dropping_it():
    with pytest.raises(ProviderAccessError):
        mantle.native_state([{'type': 'compaction', 'id': 'unknown'}, *native_output()])


def test_optional_function_item_id_absence_preserves_exact_native_state():
    output = native_output()
    output[-1].pop('id')
    result = decoded_output(output)
    assert mantle.continuation_state(result.payload) == output


def test_replay_without_relevant_native_metadata_is_rejected():
    with pytest.raises(ProviderAccessError):
        mantle.native_state([call(True)])


@pytest.mark.parametrize('added_status', ['absent', None, 'in_progress'])
@pytest.mark.parametrize('done_status', ['absent', None, 'completed'])
def test_stream_reasoning_optional_status_uses_authoritative_done_and_final_snapshot(added_status, done_status):
    events = stream_events(preliminary='')
    added = next(event['item'] for event in events if event['type'] == 'response.output_item.added')
    done = next(event['item'] for event in events if event['type'] == 'response.output_item.done')
    terminal = events[-1]['response']['output'][0]
    if added_status == 'absent':
        added.pop('status')
    else:
        added['status'] = added_status
    for item in (done, terminal):
        if done_status == 'absent':
            item.pop('status')
        else:
            item['status'] = done_status
    decoder = WorkerStream('responses', MODEL, [])
    for event in events:
        assert all(frame['kind'] != 'completed' for frame in decoder.feed(event))
    result = result_from_wire(decoder.finish()[-1]['result'])
    assert json.loads(result.payload.continuation)['state'][0] == done


@pytest.mark.parametrize('kind', ['message', 'function_call'])
def test_optional_reasoning_status_does_not_weaken_other_item_status_checks(kind):
    events = stream_events()
    added = next(event['item'] for event in events
        if event['type'] == 'response.output_item.added' and event['item']['type'] == kind)
    added.pop('status')
    decoder = mantle.StreamDecoder('responses', selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:
            decoder.feed(event)


def test_native_continuation_node_bound_failure_retains_observed_usage():
    output = native_output()
    output[1]['content'][0]['logprobs'] = [{'token': 'word', 'bytes': [119, 111, 114, 100],
        'logprob': -0.1, 'top_logprobs': []} for _ in range(1024)]
    with pytest.raises(ProviderAccessError) as caught:
        decoded_output(output)
    assert caught.value.usage.input_tokens == 2 and caught.value.usage.output_tokens == 3


@pytest.mark.parametrize('nullable_cipher', [False, True])
def test_legacy_plaintext_reasoning_tool_response_keeps_supported_suppression(nullable_cipher):
    output = native_output()
    output[0].pop('encrypted_content')
    if nullable_cipher:
        output[0]['encrypted_content'] = None
    output[0]['content'] = [{'type': 'reasoning_text', 'text': 'Hidden synthetic reasoning'}]
    output[1].pop('phase')
    result = decoded_output(output)
    assert result.payload.continuation is None
    assert result.payload.content == (TextContent('Looking up.'),)
    assert result.payload.calls == (ToolCall('c', 'lookup', '{}'),)
    assert result.usage.input_tokens == 2 and result.usage.output_tokens == 3
    assert 'Hidden synthetic reasoning' not in repr(result)
    body = mantle.encode(correction_request(result.payload), 'responses')
    assert body['input'][1] == {'role': 'assistant', 'content': 'Looking up.'}
    assert sum(item.get('type') == 'function_call' for item in body['input']) == 1
    assert sum(item.get('type') == 'function_call_output' for item in body['input']) == 1
