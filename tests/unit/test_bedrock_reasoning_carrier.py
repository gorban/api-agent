"""Common envelopes require explicit interpretation, never a model-name guess."""
from copy import deepcopy

import pytest

from api_agent.llm_providers.bedrock import catalog, mantle
from api_agent.llm_providers.bedrock.auth import validate_config
from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream, ParentStream, guard_result
from api_agent.llm_providers.bedrock.reasoning_carrier import final_answer
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import result_to_wire
from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall
from test_bedrock_mantle import completion, response, message, reasoning, chunk, usage, MODEL


@pytest.mark.parametrize('endpoint,mode,model,expected', [
    ('runtime', 'completions', 'openai.gpt-oss-20b-1:0', 'tagged'),
    ('runtime', 'completions', 'openai.gpt-oss-120b-1:0', 'auto'),
    ('runtime', 'completions', 'openai.gpt-oss-20b-2:0', 'auto'),
    ('runtime', 'completions', 'us.openai.gpt-oss-20b-1:0', 'auto'),
    ('mantle', 'completions', 'openai.gpt-oss-20b', 'auto'),
    ('runtime', 'responses', 'openai.gpt-oss-20b-1:0', 'auto'),
])
def test_auto_response_carrier_qualification_is_exact(endpoint, mode, model, expected):
    assert catalog.completions_reasoning_format('auto', endpoint, mode, model) == expected
    assert catalog.completions_reasoning_format('literal', endpoint, mode, model) == 'literal'
    assert catalog.completions_reasoning_format('tagged', endpoint, mode, model) == 'tagged'


def test_qualified_auto_decodes_tagged_answer_but_unqualified_auto_preserves_rejection_and_explicit_literal():
    raw = completion()
    raw['model'] = 'openai.gpt-oss-20b-1:0'
    text = '<reasoning>Internal material.</reasoning>Ok'
    raw['choices'][0]['message']['content'] = text
    qualified = catalog.completions_reasoning_format('auto', 'runtime', 'completions', raw['model'])
    value = mantle.decode(raw, 'completions', selected_llm=raw['model'], endpoint='runtime', reasoning_format=qualified)
    assert value.payload.content[0].text == 'Ok'
    raw['model'] = 'openai.gpt-oss-120b-1:0'
    unqualified = catalog.completions_reasoning_format('auto', 'runtime', 'completions', raw['model'])
    with pytest.raises(ProviderAccessError, match='BEDROCK_COMPLETIONS_REASONING_FORMAT'):
        mantle.decode(raw, 'completions', selected_llm=raw['model'], endpoint='runtime', reasoning_format=unqualified)
    literal = catalog.completions_reasoning_format('literal', 'runtime', 'completions', raw['model'])
    assert mantle.decode(raw, 'completions', selected_llm=raw['model'], endpoint='runtime', reasoning_format=literal).payload.content[0].text == text
    raw['model'] = 'openai.gpt-oss-20b-1:0'
    raw['choices'][0]['message']['reasoning'] = 'native reasoning'
    assert mantle.decode(raw, 'completions', selected_llm=raw['model'], endpoint='runtime', reasoning_format=qualified).payload.content[0].text == text


@pytest.mark.parametrize('text,summary,calls', [
    ('synthetic-', 'credential', ()),
    ('credential', 'synthetic-', ()),
    ('visible', 'synthetic-', (ToolCall('c', 'lookup', '{"value":"credential"}'),)),
])
def test_summary_and_answer_or_tool_cannot_split_a_credential(text, summary, calls):
    value = LLMResult(Message('assistant', (TextContent(text),), calls),
                      outcome='tool_calls' if calls else 'completed', reasoning_summary=summary)
    with pytest.raises(ContractError):
        guard_result(value, {'synthetic-credential'})


@pytest.mark.parametrize('tag', ['think', 'reasoning'])
@pytest.mark.parametrize('model,endpoint', [(MODEL, 'mantle'), ('openai.gpt-oss-20b-1:0', 'runtime'), ('example.model', 'runtime')])
def test_envelope_policy_is_model_independent_and_preserves_literal_text(tag, model, endpoint):
    raw = completion()
    raw['model'] = model
    text = f'<{tag}>Internal material.</{tag}>Ok'
    raw['choices'][0]['message']['content'] = text
    with pytest.raises(ProviderAccessError, match='BEDROCK_COMPLETIONS_REASONING_FORMAT') as error:
        mantle.decode(raw, 'completions', selected_llm=model, endpoint=endpoint)
    assert error.value.code == 'reasoning_format_ambiguous'
    assert error.value.usage.input_tokens == 2
    normalized = mantle.decode(raw, 'completions', selected_llm=model, endpoint=endpoint, reasoning_format='tagged')
    assert normalized.payload.content[0].text == 'Ok'
    assert normalized.reasoning_summary is None
    literal = mantle.decode(raw, 'completions', selected_llm=model, endpoint=endpoint, reasoning_format='literal')
    assert literal.payload.content[0].text == text


@pytest.mark.parametrize('text', ['answer <think>literal</think>', 'plain answer', '<code>literal</code>', '<'])
def test_answer_markup_is_unchanged(text):
    assert final_answer(text) == text
    assert final_answer(text, 'tagged') == text


def test_malformed_envelope_and_invalid_policy_are_explicit():
    with pytest.raises(ProviderAccessError, match='Incomplete'):
        final_answer('<reasoning>unfinished', 'tagged')
    assert final_answer('<reasoning>unfinished', 'tagged', incomplete=True) == ''
    with pytest.raises(ProviderAccessError, match='Invalid Bedrock reasoning format'):
        final_answer('text', 'unsupported')
    with pytest.raises(ContractError, match='reasoning format'):
        validate_config({'BEDROCK_COMPLETIONS_REASONING_FORMAT': 'unsupported'})


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
@pytest.mark.parametrize('policy', ['auto', 'tagged', 'literal'])
def test_native_reasoning_carrier_leaves_answer_markup_literal(field, policy):
    raw = completion()
    text = '<think>literal answer</think>'
    raw['choices'][0]['message'].update({field: 'internal', 'content': text})
    value = mantle.decode(raw, 'completions', selected_llm=MODEL, reasoning_format=policy)
    assert value.payload.content[0].text == text
    assert value.reasoning_summary is None


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
@pytest.mark.parametrize('native_first', [True, False])
def test_native_stream_carrier_precedence_survives_fragmentation(field, native_first):
    stream = WorkerStream('completions', MODEL, set(), reasoning_format='tagged')
    frames = stream.feed(chunk({'role': 'assistant'}))
    native, content = chunk({field: 'internal'}), chunk({'content': '<think>literal answer</think>'})
    for event in ([native, content] if native_first else [content, native]):
        frames.extend(stream.feed(event))
    frames.extend(stream.feed(chunk(finish='stop')))
    frames.extend(stream.feed(chunk(choices=[], usage=usage())))
    frames.extend(stream.finish())
    assert ''.join(frame['text'] for frame in frames if frame['kind'] == 'text') == '<think>literal answer</think>'
    assert frames[-1]['result']['text'] == '<think>literal answer</think>'


def test_fragmented_envelope_never_crosses_stream_boundary_before_validation():
    stream = WorkerStream('completions', MODEL, set(), reasoning_format='tagged')
    assert stream.feed(chunk({'role': 'assistant'})) == []
    for fragment in ['<rea', 'soning>internal', '</reas', 'oning>Ok']:
        assert stream.feed(chunk({'content': fragment})) == []
    assert stream.feed(chunk(finish='stop')) == []
    assert stream.feed(chunk(choices=[], usage=usage())) == []
    frames = stream.finish()
    assert frames[0] == {'kind': 'text', 'text': 'Ok'}
    parent = ParentStream('completions', selected_llm=MODEL)
    for frame in frames:
        parent.feed(frame)
    assert parent.finish().payload.content[0].text == 'Ok'


def test_stream_prefix_that_becomes_literal_is_released_without_loss():
    stream = WorkerStream('completions', MODEL, set())
    stream.feed(chunk({'role': 'assistant'}))
    frames = stream.feed(chunk({'content': '<'}))
    assert not frames
    frames += stream.feed(chunk({'content': 'code>literal'}))
    stream.feed(chunk(finish='stop'))
    stream.feed(chunk(choices=[], usage=usage()))
    frames += stream.finish()
    assert ''.join(frame['text'] for frame in frames if frame['kind'] == 'text') == '<code>literal'


def test_native_summary_excludes_raw_and_encrypted_reasoning_and_is_credential_guarded():
    raw = response([reasoning(), message('Ok')])
    decoded = mantle.decode(raw, 'responses', selected_llm=MODEL)
    assert decoded.reasoning_summary == 'plan'
    assert decoded.payload.content[0].text == 'Ok'
    assert 'private thought' not in str(result_to_wire(decoded))
    with pytest.raises(ContractError):
        guard_result(decoded, {'plan'})
    raw['output'][0]['summary'][0]['text'] = 'é' * 32769
    with pytest.raises(ProviderAccessError) as error:
        mantle.decode(raw, 'responses', selected_llm=MODEL)
    assert error.value.provider_diagnostics['validation_rule'] == 'summary_size'


@pytest.mark.parametrize('fault', ['duplicate', 'oversized', 'mismatch'])
def test_parent_summary_frames_are_bounded_and_match_terminal(fault):
    decoded = mantle.decode(response([reasoning(), message('')]), 'responses', selected_llm=MODEL)
    parent = ParentStream('responses', selected_llm=MODEL)
    parent.feed({'kind': 'reasoning_summary', 'text': 'plan'})
    frame = ({'kind': 'reasoning_summary', 'text': 'x' * 65537} if fault == 'oversized'
             else {'kind': 'reasoning_summary', 'text': 'plan'} if fault == 'duplicate'
             else {'kind': 'completed', 'result': result_to_wire(decoded) | {'reasoning_summary': 'different'}})
    with pytest.raises(ContractError, match='summary'):
        parent.feed(frame)


def test_native_summary_stream_crosses_guarded_boundary_once():
    item = {'type': 'reasoning', 'id': 'summary-1', 'status': 'in_progress', 'summary': []}
    part = {'type': 'summary_text', 'text': 'Checked format.'}
    done = item | {'status': 'completed', 'summary': [part]}
    base = {'output_index': 0, 'item_id': 'summary-1', 'summary_index': 0}
    events = [
        {'type': 'response.created', 'response': {'id': 'r', 'status': 'in_progress'}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
        base | {'type': 'response.reasoning_summary_part.added', 'part': part | {'text': ''}},
        base | {'type': 'response.reasoning_summary_text.delta', 'delta': part['text']},
        base | {'type': 'response.reasoning_summary_text.done', 'text': part['text']},
        base | {'type': 'response.reasoning_summary_part.done', 'part': part},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': done},
        {'type': 'response.completed', 'response': response([done])},
    ]
    worker = WorkerStream('responses', MODEL, set())
    frames = []
    for index, event in enumerate(events):
        frames.extend(worker.feed(event | {'sequence_number': index}))
    frames.extend(worker.finish())
    assert [frame['kind'] for frame in frames] == ['reasoning_summary', 'completed']
    parent = ParentStream('responses', selected_llm=MODEL)
    assert parent.feed(frames[0])[0].payload.text == part['text']
    assert parent.feed(frames[1]) == ()
    assert parent.finish().reasoning_summary == part['text']
