"""Bounded envelopes are a prompted-JSON decoding policy, never a value repair."""
import asyncio
from dataclasses import replace
import hashlib
import json

import pytest

from api_agent.providers import rich_inference as parsing
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import (CompletionRequest, LLMRequest, LLMResult,
    Message, ResponseEvidence, ResponsePolicy, StructuredOutput, TextContent, Tool,
    ToolCall, Usage)


SHAPE = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}'
ANSWER = '{"answer":"yes"}'


def fenced(text, width=3, label='json'):
    ticks = '`' * width
    return f'{ticks}{label}\n{text}\n{ticks}'


def nested(text, widths):
    for width in reversed(widths):
        text = fenced(text, width)
    return text


def exchange(text, actual='prompted_json', *, requested=None, shape=SHAPE):
    requested = requested or actual
    definition = StructuredOutput('answer', shape)
    request = LLMRequest('synthetic-model', CompletionRequest(
        (Message('user', (TextContent('Question'),)),),
        response=ResponsePolicy(requested, 'allow', definition)), 'envelope-call')
    evidence = ResponseEvidence(requested, 'allow', actual,
        hashlib.sha256(shape.encode()).hexdigest(), True,
        'unsupported_schema' if actual != requested else None)
    result = LLMResult(Message('assistant', (TextContent(text),)), Usage(4, 2), response=evidence)
    return request, result


@pytest.mark.parametrize('value', [{'answer': 'yes'}, [1, 2], 'text', 3, True, None,
    {'answer': '```json\nnot an envelope\n```'}])
@pytest.mark.parametrize('allow', [False, True])
def test_plain_json_is_parsed_first_and_preserves_values(value, allow):
    assert parsing._response_json(json.dumps(value), allow_markdown_fences=allow) == value


@pytest.mark.parametrize('widths', [(3,), (4, 3), (5, 4, 3), (6, 5, 4, 3)])
@pytest.mark.parametrize('label', ['', 'json'])
def test_closed_descending_envelopes_are_opt_in(widths, label):
    text = nested(fenced(ANSWER, widths[-1], label), widths[:-1])
    assert parsing._response_json(text, allow_markdown_fences=True) == {'answer': 'yes'}
    with pytest.raises(ContractError):
        parsing._response_json(text)


def test_whitespace_and_crlf_envelopes_are_supported():
    text = ' \t\r\n```json \t\r\n' + ANSWER + '\r\n``` \t\r\n '
    assert parsing._response_json(text, allow_markdown_fences=True) == {'answer': 'yes'}


@pytest.mark.parametrize('text', [
    fenced(ANSWER, 2), fenced(ANSWER, label='python'), fenced(ANSWER, label='JSON'),
    '~~~json\n' + ANSWER + '\n~~~',
    '```json\n' + ANSWER, '```json\n' + ANSWER + '\n````',
    '````json\n' + ANSWER + '\n```', '```json\n```',
    'Here is JSON:\n' + fenced(ANSWER), fenced(ANSWER) + '\nDone.',
    fenced(ANSWER) + '\n' + fenced(ANSWER), '```json ' + ANSWER + '```',
    fenced(ANSWER + '\n' + ANSWER), fenced('not json'),
])
def test_non_envelopes_and_invalid_contents_reject(text):
    with pytest.raises(ContractError):
        parsing._response_json(text, allow_markdown_fences=True)


@pytest.mark.parametrize('widths', [(3, 3), (3, 4), (5, 4, 4), (7, 6, 5, 4, 3)])
def test_each_layer_keeps_the_width_and_depth_budgets(widths):
    with pytest.raises(ContractError):
        parsing._response_json(nested(ANSWER, widths), allow_markdown_fences=True)


@pytest.mark.parametrize('text', [
    '{"answer":"one","answer":"two"}', '{"answer":NaN}',
    '{"answer":Infinity}', '{"answer":1e999}', '[0,]',
    '[' * 34 + '0' + ']' * 34, json.dumps([0] * 1025),
    json.dumps([[0] * 1024] * 9), json.dumps('x' * 524289),
])
def test_unwrapping_does_not_relax_existing_json_bounds(text):
    with pytest.raises(ContractError):
        parsing._response_json(fenced(text), allow_markdown_fences=True)


def test_duplicate_keys_do_not_enter_the_syntax_fallback(monkeypatch):
    def unexpected_envelope(*args, **kwargs):
        pytest.fail('Semantic JSON failures must not trigger envelope parsing')
    monkeypatch.setattr(parsing.re, 'fullmatch', unexpected_envelope)
    with pytest.raises(ContractError):
        parsing._response_json('{"answer":1,"answer":2}', allow_markdown_fences=True)


@pytest.mark.parametrize('validator', ['validate_result', 'async_validate_result', 'validate_response_result'])
@pytest.mark.parametrize('requested', ['prompted_json', 'json', 'structured'])
def test_all_validation_boundaries_and_consumer_agree_without_rewriting_evidence(validator, requested):
    text = nested(ANSWER, (5, 4, 3))
    request, result = exchange(text, requested=requested)
    call = getattr(parsing, validator)(request, result)
    admitted = asyncio.run(call) if validator == 'async_validate_result' else call
    restored = parsing.result_from_wire(parsing.result_to_wire(admitted))
    assert admitted is result
    assert parsing.response_json(restored) == {'answer': 'yes'}
    assert restored.payload.content[0].text == text
    assert restored.response == result.response
    assert restored.usage == result.usage


@pytest.mark.parametrize('validator', ['validate_result', 'async_validate_result', 'validate_response_result'])
@pytest.mark.parametrize('actual', ['json', 'structured'])
def test_native_modes_do_not_accept_fences(validator, actual):
    request, result = exchange(fenced(ANSWER), actual)
    with pytest.raises(ProviderAccessError):
        call = getattr(parsing, validator)(request, result)
        if validator == 'async_validate_result':
            asyncio.run(call)
    with pytest.raises(ContractError):
        parsing.response_json(result)


def test_missing_mode_evidence_is_not_permission_to_unwrap():
    _, result = exchange(fenced(ANSWER))
    with pytest.raises(ContractError):
        parsing.response_json(replace(result, response=None))


@pytest.mark.parametrize('text', [fenced('{"answer":7}'), fenced('{}'),
    fenced('{"answer":"yes","extra":true}'), fenced('')])
@pytest.mark.parametrize('validator', ['validate_result', 'async_validate_result', 'validate_response_result'])
def test_original_schema_and_nonempty_answer_requirements_still_apply(text, validator):
    request, result = exchange(text)
    with pytest.raises(ProviderAccessError):
        call = getattr(parsing, validator)(request, result)
        if validator == 'async_validate_result':
            asyncio.run(call)


@pytest.mark.parametrize('value,shape', [([1], '{"type":"array","items":{"type":"integer"}}'),
    ('yes', '{"type":"string"}'), (None, '{"type":"null"}')])
def test_prompted_response_roots_are_not_forced_to_objects(value, shape):
    request, result = exchange(fenced(json.dumps(value)), shape=shape)
    assert parsing.validate_result(request, result) is result
    assert parsing.response_json(result) == value


def test_prompted_response_does_not_enable_fenced_tool_arguments():
    request, result = exchange('')
    request = replace(request, payload=replace(request.payload, tools=(Tool('lookup', '', SHAPE),)))
    result = replace(result, outcome='tool_calls', payload=Message('assistant', (),
        calls=(ToolCall('call', 'lookup', fenced(ANSWER)),)))
    with pytest.raises(ProviderAccessError, match='bounded, unambiguous JSON object'):
        parsing.validate_result(request, result)


def test_legacy_structured_output_and_object_parser_stay_strict():
    request, result = exchange(fenced(ANSWER))
    request = replace(request, payload=replace(request.payload, response=None,
        structured=StructuredOutput('answer', SHAPE)))
    result = replace(result, response=None)
    with pytest.raises(ProviderAccessError):
        parsing.validate_result(request, result)
    with pytest.raises(ContractError):
        parsing.object_json(fenced(ANSWER))
