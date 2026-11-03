"""Plaintext display reasoning stays separate, incremental and protected."""
import pytest

from api_agent.llm_providers.bedrock import mantle
from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream, ParentStream, guard_result
from api_agent.llm_providers.bedrock.reasoning_carrier import display_reasoning
from api_agent.providers.contracts import ContractError, ProviderAccessError
from test_bedrock_mantle import MODEL, completion, chunk, usage


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_native_reasoning_reaches_parent_before_answer_or_completion(field):
    worker = WorkerStream('completions', MODEL, set())
    parent = ParentStream('completions', selected_llm=MODEL)
    worker.feed(chunk({'role': 'assistant'}))
    first = worker.feed(chunk({field: 'Checking '}))
    second = worker.feed(chunk({field: 'the request.'}))
    assert first == [{'kind': 'reasoning_summary', 'text': 'Checking '}]
    assert second == [{'kind': 'reasoning_summary', 'text': 'the request.'}]
    frames = first + second + worker.feed(chunk({'content': 'Ok'}))
    worker.feed(chunk(finish='stop'))
    worker.feed(chunk(choices=[], usage=usage()))
    frames += worker.finish()
    for frame in frames:
        parent.feed(frame)
    result = parent.finish()
    assert result.payload.content[0].text == 'Ok'
    assert result.reasoning_summary == 'Checking the request.'


def test_reasoning_credential_tail_never_reaches_parent():
    worker = WorkerStream('completions', MODEL, {'synthetic-credential'})
    worker.feed(chunk({'role': 'assistant'}))
    frames = worker.feed(chunk({'reasoning_content': 'A long harmless explanation. synthetic-'}))
    assert frames and 'synthetic-' not in ''.join(frame['text'] for frame in frames)
    with pytest.raises(ContractError):
        worker.feed(chunk({'reasoning_content': 'credential'}))


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_native_reasoning_is_protected_even_without_streaming(field):
    raw = completion()
    raw['choices'][0]['message'].update({field: 'synthetic-', 'content': 'credential'})
    result = mantle.decode(raw, 'completions', selected_llm=MODEL)
    with pytest.raises(ContractError):
        guard_result(result, {'synthetic-credential'})


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_empty_native_alias_does_not_override_an_explicit_tagged_envelope(field):
    raw = completion()
    raw['choices'][0]['message'].update({field: '', 'content': '<reasoning>Check format.</reasoning>Ok'})
    decoded = mantle.decode(raw, 'completions', selected_llm=MODEL, reasoning_format='tagged')
    assert decoded.payload.content[0].text == 'Ok'
    assert decoded.reasoning_summary == 'Check format.'
    worker = WorkerStream('completions', MODEL, set(), reasoning_format='tagged')
    worker.feed(chunk({'role': 'assistant', field: ''}))
    for fragment in ['<rea', 'soning>Check format.</reasoning>', 'Ok']:
        assert worker.feed(chunk({'content': fragment})) == []
    worker.feed(chunk(finish='stop'))
    worker.feed(chunk(choices=[], usage=usage()))
    frames = worker.finish()
    assert [frame['text'] for frame in frames if frame['kind'] == 'text'] == ['Ok']
    assert frames[-1]['result']['reasoning_summary'] == 'Check format.'


def test_multiple_native_aliases_are_preserved_but_ambiguous_stream_order_is_rejected():
    raw = completion()
    raw['choices'][0]['message'].update(reasoning='first', reasoning_content='second')
    assert mantle.decode(raw, 'completions', selected_llm=MODEL).reasoning_summary == 'secondfirst'
    worker = WorkerStream('completions', MODEL, set())
    worker.feed(chunk({'role': 'assistant'}))
    with pytest.raises(ProviderAccessError):
        worker.feed(chunk({'reasoning': 'first', 'reasoning_content': 'second'}))


@pytest.mark.parametrize('policy', ['auto', 'literal'])
def test_literal_or_ambiguous_markup_never_becomes_display_reasoning(policy):
    assert display_reasoning('<think>literal</think>answer', policy) == ''


def test_explicit_envelope_display_handles_missing_and_incomplete_carriers():
    assert display_reasoning('ordinary answer', 'tagged') == ''
    assert display_reasoning('<think>partial', 'tagged', incomplete=True) == ''
    assert display_reasoning('<think>partial', 'tagged') == 'partial'


def test_stream_reasoning_is_byte_bounded_before_any_progress_is_released():
    worker = WorkerStream('completions', MODEL, set())
    worker.feed(chunk({'role': 'assistant'}))
    with pytest.raises(ContractError, match='summary'):
        worker.feed(chunk({'reasoning_content': 'é' * 32769}))


def test_worker_rejects_a_decoder_that_rewrites_already_released_reasoning():
    worker = WorkerStream('completions', MODEL, set())
    worker.feed(chunk({'role': 'assistant'}))
    assert worker.feed(chunk({'reasoning_content': 'Original.'}))
    worker.decoder.reasoning['reasoning_content'] = 'Rewritten.'
    with pytest.raises(ContractError, match='summary'):
        worker.feed(chunk({}))


def test_worker_rejects_inconsistent_authoritative_summary(monkeypatch):
    from dataclasses import replace
    worker = WorkerStream('completions', MODEL, set())
    worker.feed(chunk({'role': 'assistant'}))
    worker.feed(chunk({'reasoning_content': 'Original.'}))
    worker.feed(chunk({'content': 'Ok'}))
    worker.feed(chunk(finish='stop'))
    worker.feed(chunk(choices=[], usage=usage()))
    original = worker.decoder.finish()
    monkeypatch.setattr(worker.decoder, 'finish', lambda: replace(original, reasoning_summary='Rewritten.'))
    with pytest.raises(ContractError, match='summary'):
        worker.finish()
