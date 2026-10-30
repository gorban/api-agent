"""Maintained-SDK chat message metadata with no annotation semantics."""
from copy import deepcopy

import httpx
import pytest

from api_agent.llm_providers.bedrock import mantle
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import TextContent

MODEL = 'openai.gpt-oss-20b'


def sdk_completion(annotations=..., **fields):
    from openai import OpenAI
    raw = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': MODEL,
           'choices': [{'index': 0, 'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': 'hi', **fields}}],
           'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    if annotations is not ...:
        raw['choices'][0]['message']['annotations'] = annotations
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))) as http:
        with OpenAI(api_key='synthetic', base_url='https://example.invalid/v1', http_client=http, max_retries=0) as client:
            return client.chat.completions.create(model=MODEL, messages=[{'role': 'user', 'content': 'hello'}]).model_dump(exclude_unset=True)


def test_sdk_empty_annotations_have_no_content_or_usage_semantics():
    raw = sdk_completion([])
    assert raw['choices'][0]['message']['annotations'] == []

    result = mantle.decode(raw, 'completions', selected_llm=MODEL)

    assert result.payload.content == (TextContent('hi'),)
    assert result.usage.input_tokens == 2
    assert result.usage.output_tokens == 3
    assert result.outcome == 'completed'


@pytest.mark.parametrize('annotations', [False, {}, '', [None], [{'type': 'url_citation', 'url_citation': {
    'start_index': 0, 'end_index': 2, 'title': 'synthetic', 'url': 'https://example.invalid'}}]])
def test_annotation_semantics_and_malformed_empty_values_are_rejected(annotations):
    raw = sdk_completion(None)
    raw['choices'][0]['message']['annotations'] = deepcopy(annotations)

    with pytest.raises(ProviderAccessError):
        mantle.decode(raw, 'completions', selected_llm=MODEL)


@pytest.mark.parametrize('fields', [{'reasoning': 'synthetic hidden reasoning'}, {'reasoning': None},
    {'reasoning': 'first hidden', 'reasoning_content': 'second hidden'}])
def test_sdk_reasoning_alias_is_validated_and_suppressed(fields):
    raw = sdk_completion(**fields)
    assert raw['choices'][0]['message']['reasoning'] == fields['reasoning']
    assert 'annotations' not in raw['choices'][0]['message']

    result = mantle.decode(raw, 'completions', selected_llm=MODEL)

    assert result.payload.content == (TextContent('hi'),)
    assert result.payload.calls == ()
    assert result.payload.continuation is None
    assert result.usage.input_tokens == 2 and result.usage.output_tokens == 3
    assert result.usage.reasoning_tokens is None
    assert result.outcome == 'completed'
    assert 'hidden' not in repr(result)


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
@pytest.mark.parametrize('value', [False, 1, [], {}, '\ud800'])
def test_reasoning_aliases_reject_malformed_values_independently(field, value):
    raw = sdk_completion(reasoning='valid hidden', reasoning_content='valid hidden')
    raw['choices'][0]['message'][field] = value

    with pytest.raises(ProviderAccessError):
        mantle.decode(raw, 'completions', selected_llm=MODEL)


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_reasoning_aliases_enforce_own_text_bound(field, monkeypatch):
    raw = sdk_completion()
    raw['choices'][0]['message'][field] = 'x' * (mantle._LIMIT + 1)
    # Isolate per-text validation from the independent whole-response bound.
    monkeypatch.setattr(mantle, '_bounded', lambda raw: None)

    with pytest.raises(ProviderAccessError):
        mantle.decode(raw, 'completions', selected_llm=MODEL)
