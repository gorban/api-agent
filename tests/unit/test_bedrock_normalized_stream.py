import multiprocessing
from types import SimpleNamespace
import pytest

from api_agent.llm_providers.bedrock import worker
from api_agent.llm_providers.bedrock.normalized_stream import ParentStream, WorkerStream
from api_agent.providers.contracts import ContractError, ProviderAccessError
from test_bedrock_mantle import MODEL, completion_stream, response_stream, chunk, usage


@pytest.mark.parametrize('arguments', ['not JSON', '[1, 2]', '{"safe":"argument"}'])
def test_rejected_response_evidence_is_unchanged_when_safe(arguments):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    raw = {'content':'observed text', 'parts':[{'text':'another fragment'}],
           'arguments':arguments, 'number':1, 'optional':None}
    assert guard_response_evidence(raw, {'synthetic-credential'}) == raw


def test_oversized_rejected_response_evidence_is_not_retained():
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError, match='capture limit'):
        guard_response_evidence({'content':'x' * 524288}, set())


@pytest.mark.parametrize('raw', [
    {'output':[{'content':[{'text':'synthetic-'}, {'text':'credential'}]}]},
    {'content':'synthetic-', 'arguments':'{"value":"credential"}'},
    {'content':'credential', 'arguments':'{"value":"synthetic-"}'},
    {'content':'safe', 'reasoning_content':'synthetic-credential'},
])
def test_rejected_response_evidence_guards_additional_text_and_argument_carriers(raw):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError):
        guard_response_evidence(raw, {'synthetic-credential'})


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content', 'thinking', 'thinking_content',
                                 'chain_of_thought', 'analysis', 'summary', 'reasoning_text', 'output_text'])
@pytest.mark.parametrize('suffix', ['credential', ['credential'], [{'text':'credential'}]])
def test_rejected_evidence_protects_plaintext_carrier_aliases(field, suffix):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    raw = {'content':'synthetic-', field:suffix}
    with pytest.raises(ContractError):
        guard_response_evidence(raw, {'synthetic-credential'})


def test_rejected_evidence_protects_unknown_sdk_text_leaves():
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError):
        guard_response_evidence({'content':'synthetic-', 'unknown':'credential'}, {'synthetic-credential'})


@pytest.mark.parametrize('raw', [
    {'content':'synthetic-', 'intervening':'unrelated', 'unknown':'credential'},
    {'content':'credential', 'intervening':'unrelated', 'unknown':'synthetic-'},
    {'synthetic-':'credential'},
    {'content':'synthetic-', 'extra_a':'cre', 'unrelated':'ordinary', 'extra_b':'dential'},
    {'extra_b':'dential', 'unrelated':'ordinary', 'extra_a':'cre', 'content':'synthetic-'},
    {'content':'prefix synthetic-', 'extra_a':'cre', 'unrelated':'ordinary', 'extra_b':'dential suffix'},
    {'content':'synthetic-', 'extra_a':'c\\u0072e', 'unrelated':'ordinary', 'extra_b':'dential'},
])
def test_rejected_evidence_guards_nonadjacent_fields_and_object_keys(raw):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError):
        guard_response_evidence(raw, {'synthetic-credential'})


@pytest.mark.parametrize('raw,secrets', [
    ({str(index):'' for index in range(100)}, set()),
    ({str(index):'x' * 8000 + str(index) for index in range(50)}, {f'not-observed-{index}' for index in range(64)}),
])
def test_rejected_evidence_carrier_work_is_bounded(raw, secrets):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError, match='carrier protection limit'):
        guard_response_evidence(raw, secrets)


@pytest.mark.parametrize('first,last', [
    ('synthetic-\\u00', '63redential'),
    ('synthetic-\\', 'u0063redential'),
    ('synthetic-\\u006', '3redential'),
    ('synthetic-\\\\u00', '63redential'),
])
def test_rejected_evidence_withholds_escapes_split_between_fields(first, last):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    raw = {'content':first, 'unrelated':'ordinary', 'unexpected':last}
    with pytest.raises(ContractError, match='ambiguous escape'):
        guard_response_evidence(raw, {'synthetic-credential'})


@pytest.mark.parametrize('text', ['harmless\\u0020text', 'path\\\\file'])
def test_rejected_evidence_keeps_safe_complete_escapes(text):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    raw = {'content':text}
    assert guard_response_evidence(raw, {'synthetic-credential'}) == raw


@pytest.mark.parametrize('text', ['prefix {"safe":"value"}', 'prefix ["safe","value"] tail',
                                'the quoted "safe value" is harmless'])
def test_rejected_evidence_keeps_safe_embedded_json_carriers(text):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    raw = {'content':text}
    assert guard_response_evidence(raw, {'synthetic-credential'}) == raw


@pytest.mark.parametrize('text', ['prefix {"a":"synthetic-","b":"credential"}',
                                'unfinished {"a":"synthetic-","b":"credential"',
                                'malformed {"a":"synthetic-","b":"credential",}'])
def test_rejected_evidence_withholds_unsafe_or_ambiguous_embedded_json(text):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_response_evidence
    with pytest.raises(ContractError):
        guard_response_evidence({'content':text}, {'synthetic-credential'})


@pytest.mark.parametrize('mode,tools', [(m, t) for m in ('completions', 'responses') for t in (False, True)])
def test_normalized_parity(mode, tools):
    source = completion_stream(tools) if mode == 'completions' else response_stream(tools)
    child, parent = WorkerStream(mode, MODEL, {'synthetic-credential'}), ParentStream(mode, selected_llm=MODEL)
    emitted = []
    for event in source:
        for frame in child.feed(event):
            emitted.extend(parent.feed(frame))
    for frame in child.finish():
        emitted.extend(parent.feed(frame))
    result = parent.finish()
    assert ''.join(event.payload.text for event in emitted) == ('' if mode == 'responses' and tools else 'hi')
    assert bool(result.payload.calls) == tools
    assert result.usage.input_tokens == 2
    with pytest.raises(ContractError):
        parent.feed({'kind': 'text', 'text': 'late'})


def test_snapshot_mismatch_rejected():
    child = WorkerStream('responses', MODEL, set())
    events = response_stream()
    events[-1]['response']['output'][0]['content'][0]['text'] = 'different'
    with pytest.raises(ProviderAccessError):
        for event in events:
            child.feed(event)


def test_embedded_reordered_json_credential_is_quarantined():
    child = WorkerStream('completions', MODEL, {'{"a":1,"b":2}'})
    child.feed(chunk({'role': 'assistant'}))
    frames = child.feed(chunk({'content': 'prefix {"b":2,'}))
    with pytest.raises(ContractError):
        child.feed(chunk({'content': '"a":1} suffix'}))
    assert not any('{"b":2' in frame['text'] for frame in frames)


def test_multiline_text_continues_streaming():
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    child.feed(chunk({'role': 'assistant'}))
    frames = child.feed(chunk({'content': 'A first line\nA second line with more text'}))
    assert frames and '\n' in frames[0]['text']
    child.feed(chunk(finish='stop'))
    child.feed(chunk(choices=[], usage=usage()))
    assert ''.join(frame.get('text', '') for frame in frames + child.finish()) == 'A first line\nA second line with more text'


def test_closed_quoted_prose_resumes_streaming_before_eof():
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    child.feed(chunk({'role': 'assistant'}))
    frames = child.feed(chunk({'content': 'The response says "hello'}))
    frames += child.feed(chunk({'content': ' world" and continues with ample plain text.'}))
    assert frames and 'hello world' in ''.join(frame['text'] for frame in frames)


def test_quote_state_survives_lookbehind_boundary():
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    child.feed(chunk({'role': 'assistant'}))
    first = child.feed(chunk({'content': 'An answer "long quotation"'}))
    second = child.feed(chunk({'content': ' followed by a long continuation that should stream.'}))
    assert first and second
    assert 'long quotation' in ''.join(frame['text'] for frame in first + second)
    third = child.feed(chunk({'content': ' More ordinary text continues here.'}))
    assert third


def test_tool_arguments_are_guarded_after_assembly():
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    events = [chunk({'role': 'assistant'}),
        chunk({'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function',
            'function': {'name': 'lookup', 'arguments': '{"value":"sec'}}]}),
        chunk({'tool_calls': [{'index': 0, 'function': {'arguments': 'ret-canary"}'}}]}),
        chunk(finish='tool_calls'), chunk(choices=[], usage=usage())]
    for event in events:
        assert child.feed(event) == []
    with pytest.raises(ContractError) as caught:
        child.finish()
    assert caught.value.usage.input_tokens == 2


@pytest.mark.parametrize('text,argument,tool_first', [
    ('secret-', 'canary', False), ('canary', 'secret-', True),
])
def test_text_and_tool_value_cannot_reassemble_credential(text, argument, tool_first):
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    child.feed(chunk({'role': 'assistant'}))
    text_event = chunk({'content': text})
    tool_event = chunk({'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function',
        'function': {'name': 'lookup', 'arguments': '{"value":"' + argument + '"}'}}]})
    frames = []
    for event in ([tool_event, text_event] if tool_first else [text_event, tool_event]):
        frames.extend(child.feed(event))
    child.feed(chunk(finish='tool_calls'))
    child.feed(chunk(choices=[], usage=usage()))
    with pytest.raises(ContractError):
        child.finish()
    assert not any('secret-' in frame['text'] or 'canary' in frame['text'] for frame in frames)


def test_unrelated_tool_values_are_not_joined():
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    for event in [chunk({'role': 'assistant'}),
        chunk({'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function',
            'function': {'name': 'lookup', 'arguments': '{"one":"secret-","two":"canary"}'}}]}),
        chunk(finish='tool_calls'), chunk(choices=[], usage=usage())]:
        child.feed(event)
    assert child.finish()[-1]['kind'] == 'completed'


def test_worker_rejects_acquired_credential_in_final_request():
    frames = []
    request = {'timeout': 30, 'operation': 'completions',
        'auth': {'mode': 'api_key', 'api_key': 'secret-canary', 'region': 'us-east-1'},
        'payload': {'prefix': '/v1', 'body': {'model': MODEL,
            'messages': [{'role': 'user', 'content': 'secret-canary'}]}}}
    worker.execute(request, frames.append, http_factory=lambda **kw: pytest.fail('SDK was reached'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


def run_worker(out, proceed, pieces):
    class Stream:
        def __iter__(self):
            yield SimpleNamespace(model_dump=lambda **kw: chunk({'role': 'assistant'}))
            for index, text in enumerate(pieces):
                yield SimpleNamespace(model_dump=lambda text=text, **kw: chunk({'content': text}))
                if index == 0:
                    proceed.wait(5)
            yield SimpleNamespace(model_dump=lambda **kw: chunk(finish='stop'))
            yield SimpleNamespace(model_dump=lambda **kw: chunk(choices=[], usage=usage()))
        def close(self): pass
    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: Stream()))
        def close(self): pass
    class HTTP:
        def close(self): pass
    request = {'timeout': 30, 'operation': 'completions_stream',
        'auth': {'mode': 'api_key', 'api_key': 'secret-canary', 'region': 'us-east-1'},
        'payload': {'prefix': '/v1', 'body': {'model': MODEL, 'messages': []}}}
    worker.execute(request, out.put, client_factory=lambda *args: Client(), http_factory=lambda **kw: HTTP())
    out.put(None)


def test_process_streams_safe_text_before_completion():
    ctx = multiprocessing.get_context('spawn')
    queue, gate = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=run_worker, args=(queue, gate, ['A long first line\nmore words', ' and a final sentence']))
    process.start()
    first = queue.get(timeout=5)
    assert first['event']['kind'] == 'text'
    gate.set()
    frames = [first]
    while (frame := queue.get(timeout=5)) is not None:
        frames.append(frame)
    process.join(5)
    assert process.exitcode == 0
    assert frames[-1]['event']['kind'] == 'completed'
    assert ''.join(frame['event'].get('text', '') for frame in frames) == 'A long first line\nmore words and a final sentence'


@pytest.mark.parametrize('pieces', [
    ['safe text sufficiently long sec', 'ret-canary'],
    ['safe text sufficiently long sec', '\\u0072et-canary'],
    ['safe text sufficiently long sec', '\nret-canary'],
])
def test_process_rejects_split_credential_before_ipc(pieces):
    ctx = multiprocessing.get_context('spawn')
    queue, gate = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=run_worker, args=(queue, gate, pieces))
    process.start()
    first = queue.get(timeout=5)
    assert first['event']['kind'] == 'text'
    gate.set()
    frames = [first]
    while (frame := queue.get(timeout=5)) is not None:
        frames.append(frame)
    process.join(5)
    assert process.exitcode == 0
    assert frames[-1]['error'] == 'unknown'
    assert 'secret-canary' not in repr(frames)
    assert ''.join(f.get('event', {}).get('text', '') for f in frames) == 'safe text sufficien'

@pytest.mark.parametrize('text, ambiguous', [
    ('"a\\\"b"', False), ('plain\\escape', True), ('{]', True),
    ('["closed"] suffix', False), ('["unfinished', True),
])
def test_carrier_boundaries_handle_escapes_and_mismatched_delimiters(text, ambiguous):
    from api_agent.llm_providers.bedrock.normalized_stream import ambiguous_carrier, safe_boundary
    assert ambiguous_carrier(text) is ambiguous
    if not ambiguous:
        assert safe_boundary(text, len(text)) == len(text)


def test_normalized_stream_enforces_credential_and_buffer_limits(monkeypatch):
    from api_agent.llm_providers.bedrock import normalized_stream as ns
    monkeypatch.setattr(ns, '_LIMIT', 8)
    with pytest.raises(ContractError, match='limit'):
        WorkerStream('completions', MODEL, {'too-long-secret'})
    child = WorkerStream('completions', MODEL, set())
    child.feed(chunk({'role': 'assistant'}))
    with pytest.raises(ContractError, match='limit'):
        child.feed(chunk({'content': 'longer than limit'}))
    parent = ParentStream('completions', selected_llm=MODEL)
    with pytest.raises(ContractError):
        parent.feed({'kind': 'text', 'text': 'longer than limit'})


def test_parent_rejects_mismatched_result_and_incomplete_or_invalid_frames():
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent
    parent = ParentStream('completions', selected_llm=MODEL)
    with pytest.raises(ContractError): parent.finish()
    with pytest.raises(ContractError): parent.feed({'kind': 'unknown'})
    parent.feed({'kind': 'text', 'text': 'visible'})
    result = LLMResult(Message('assistant', (TextContent('different'),)))
    with pytest.raises(ContractError): parent.feed({'kind': 'completed', 'result': result_to_wire(result)})


@pytest.mark.parametrize('value, safe', [(['ordinary', {'nested': 'value'}], True), (['secret-', {'nested': 'canary'}], True), (['secret-canary'], False)])
def test_nested_tool_values_are_checked_as_independent_carriers(value, safe):
    import json
    child = WorkerStream('completions', MODEL, {'secret-canary'})
    for event in [chunk({'role': 'assistant'}),
        chunk({'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function',
            'function': {'name': 'lookup', 'arguments': json.dumps({'values': value})}}]}),
        chunk(finish='tool_calls'), chunk(choices=[], usage=usage())]:
        child.feed(event)
    if safe:
        assert child.finish()[-1]['kind'] == 'completed'
    else:
        with pytest.raises(ContractError): child.finish()


def test_tool_and_text_carrier_budget_rejects_excessive_cross_checks_with_usage():
    from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall, Usage
    result = LLMResult(Message('assistant', (TextContent('x' * 500000),),
        calls=tuple(ToolCall(f'call{i}', 'lookup', '{"value":"ok"}') for i in range(10))), Usage(9, 4), outcome='tool_calls')
    child = WorkerStream('completions', MODEL, set())
    child.decoder = SimpleNamespace(finish=lambda: result)
    with pytest.raises(ContractError, match='limit') as caught:
        child.finish()
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (9, 4)
