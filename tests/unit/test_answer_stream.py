"""A request may name the structured field whose text streams as the visible answer."""
import json

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import (AnswerStream, CompletionRequest, GenerationOptions, LLMRequest,
    Message, StructuredOutput, TextContent)
from api_agent.providers.rich_inference import request_from_wire, request_to_wire

SCHEMA = json.dumps({'type': 'object', 'properties': {'response_text': {'type': 'string'}},
                     'required': ['response_text'], 'additionalProperties': False})


def answer_request(stream=AnswerStream('response_text', 'Text')):
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Answer.'),)),),
        generation=GenerationOptions(answer_stream=stream),
        structured=StructuredOutput('answer', SCHEMA)), 'answer')


@pytest.mark.parametrize('field,presentation', [
    ('', 'Text'), ('response text', 'Text'), ('1st', 'Text'), ('x' * 65, 'Text'),
    ('response_text', ''), ('response_text', 'x' * 33), ('response_text', 'Te\nxt'),
    (3, 'Text'), ('response_text', None)])
def test_answer_stream_names_one_field_and_a_short_presentation(field, presentation):
    with pytest.raises(ValueError):
        AnswerStream(field, presentation)


def test_generation_accepts_only_an_answer_stream_or_none():
    with pytest.raises(ValueError):
        GenerationOptions(answer_stream=('response_text', 'Text'))
    assert GenerationOptions().answer_stream is None


def test_answer_stream_survives_the_rich_wire_round_trip():
    row = request_to_wire(answer_request())
    assert row['answer_stream'] == {'field': 'response_text', 'presentation': 'Text'}
    assert request_from_wire(row).payload.generation.answer_stream == AnswerStream('response_text', 'Text')
    assert 'answer_stream' not in request_to_wire(answer_request(None))


@pytest.mark.parametrize('value', [
    {'field': 'response_text'}, {'field': 'response_text', 'presentation': 'Text', 'extra': 1},
    {'field': 'bad field', 'presentation': 'Text'}, 'response_text', None])
def test_malformed_wire_answer_stream_is_rejected(value):
    row = request_to_wire(answer_request())
    row['answer_stream'] = value
    with pytest.raises(ContractError):
        request_from_wire(row)
