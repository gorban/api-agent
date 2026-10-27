"""Observed Mantle metadata reaches cost attribution without guessing identity."""
import pytest

from api_agent.llm_providers.bedrock import mantle, pricing
from api_agent.llm_providers.bedrock.factory import factory
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


def raw_result(mode, **metadata):
    if mode == 'responses':
        return dict(status='completed', output=[], usage=dict(input_tokens=1000, output_tokens=200,
                    total_tokens=1200), **metadata)
    return dict(choices=[dict(index=0, message=dict(role='assistant', content=''), finish_reason='stop')],
                usage=dict(prompt_tokens=1000, completion_tokens=200, total_tokens=1200), **metadata)


def quoted(mode, result):
    request = LLMRequest(pricing.MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),)))
    return factory({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_API_MODE': mode}, {}).quote_cost(request, result)


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('tier', [None, 'default', 'standard', 'priority'])
def test_real_decode_preserves_metadata_and_quotes_only_standard(mode, tier):
    result = mantle.decode(raw_result(mode, model=pricing.MODEL, service_tier=tier,
        serving_revision='observed-revision'), mode, selected_llm=pricing.MODEL)
    assert (result.actual_model, result.service_tier, result.serving_revision) == (pricing.MODEL, tier, 'observed-revision')
    assert result.usage.cached_input_tokens is None
    quote = quoted(mode, result)
    assert quote['list_cost_usd'] == (None if tier == 'priority' else pytest.approx(.00013))
    assert quote['accounting']['service_tier'] == ('priority' if tier == 'priority' else 'standard')


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_decode_missing_metadata_stays_missing(mode):
    result = mantle.decode(raw_result(mode), mode, selected_llm=pricing.MODEL)
    assert (result.actual_model, result.service_tier, result.serving_revision) == (None, None, None)


@pytest.mark.parametrize('field', ['service_tier', 'serving_revision'])
@pytest.mark.parametrize('value', [False, '', 'x'*201, 'bad\n'])
@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_decode_rejects_invalid_metadata(mode, field, value):
    with pytest.raises(ProviderAccessError):
        mantle.decode(raw_result(mode, **{field:value}), mode, selected_llm=pricing.MODEL)


def completion_chunk(**metadata):
    return dict(object='chat.completion.chunk', id='call', model=pricing.MODEL,
        choices=[dict(index=0, delta=dict(role='assistant'), finish_reason='stop')], **metadata)


def response_event(kind, sequence, **raw):
    return dict(type=kind, sequence_number=sequence, response=dict(id='call', **raw))


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('tier', [None, 'default', 'standard', 'priority'])
def test_stream_retains_observed_metadata_on_terminal_without_repetition(mode, tier):
    decoder = mantle.StreamDecoder(mode, selected_llm=pricing.MODEL)
    metadata = dict(model=pricing.MODEL, service_tier=tier, serving_revision='observed-revision')
    if mode == 'responses':
        decoder.feed(response_event('response.created', 0, status='in_progress', **metadata))
        decoder.feed(response_event('response.in_progress', 1, status='in_progress', **metadata))
        decoder.feed(response_event('response.completed', 2, **raw_result(mode)))
    else:
        metadata.pop('model')
        decoder.feed(completion_chunk(**metadata))
        decoder.feed(dict(object='chat.completion.chunk', id='call', model=pricing.MODEL,
                          choices=[], usage=raw_result(mode)['usage']))
    result = decoder.finish()
    assert (result.actual_model, result.service_tier, result.serving_revision) == (pricing.MODEL, tier, 'observed-revision')
    assert quoted(mode, result)['list_cost_usd'] == (None if tier == 'priority' else pytest.approx(.00013))


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('field', ['service_tier', 'serving_revision'])
def test_stream_rejects_conflicting_observed_metadata(mode, field):
    decoder = mantle.StreamDecoder(mode, selected_llm=pricing.MODEL)
    metadata = {field:'first'}
    if mode == 'responses':
        decoder.feed(response_event('response.created', 0, status='in_progress', **metadata))
        event = response_event('response.completed', 1, **raw_result(mode, **{field:'second'}))
    else:
        decoder.feed(completion_chunk(**metadata))
        event = dict(object='chat.completion.chunk', id='call', model=pricing.MODEL, choices=[],
                     usage=raw_result(mode)['usage'], **{field:'second'})
    with pytest.raises(ProviderAccessError):
        decoder.feed(event)
