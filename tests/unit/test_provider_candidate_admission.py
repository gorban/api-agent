"""Only definitive completed candidates can carry a repair receipt."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from api_agent.providers.contracts import CandidateOutputError, ContractError, ProviderAccessError
from api_agent.providers.llm import (
    CompletionRequest, LLMRequest, LLMResult, Message, ResponseEvidence,
    ResponsePolicy, StructuredOutput, TextContent, ToolCall, Usage,
)
from api_agent.providers.rich_inference import async_validate_result


def candidate(text='{"answer":"candidate"}'):
    return LLMResult(Message('assistant', (TextContent(text),)), Usage(4, 2))


@pytest.mark.parametrize('invalid', [None, object(),
    replace(candidate(), outcome='length'),
    replace(candidate(), payload=Message('assistant', calls=(ToolCall('id', 'tool', '{}'),))),
])
def test_repair_receipt_rejects_noncompleted_or_call_bearing_candidate(invalid):
    with pytest.raises(ContractError, match='Invalid rejected inference candidate'):
        CandidateOutputError(invalid, {'reason': 'schema'})


@pytest.mark.parametrize('feedback', [None, [], {}, {'detail': 'x' * 65537},
    {'nonfinite': float('nan')}, {'unserializable': object()}, {'tuple': (1, 2)}, {1: 'nonstring key'},
])
def test_repair_receipt_rejects_invalid_or_lossy_feedback(feedback):
    with pytest.raises(ContractError, match='Invalid rejected inference feedback'):
        CandidateOutputError(candidate(), feedback)


def test_repair_receipt_copies_feedback_but_preserves_original_result_identity():
    original = candidate()
    facts = {'reason': 'schema', 'errors': [{'path': ['answer'], 'keyword': 'maxLength', 'actual': 9}]}
    error = CandidateOutputError(original, facts)
    facts['errors'][0]['path'].append('changed')
    assert error.result is original and error.usage is original.usage
    assert error.feedback['errors'][0]['path'] == ['answer']
    assert error.execution == 'completed' and error.code == 'response_candidate_rejected'
    assert 'candidate' not in repr(error.feedback)


def response_request():
    definition = json.dumps({'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 3}},
                             'required': ['answer'], 'additionalProperties': False})
    policy = ResponsePolicy('prompted_json', 'reject', StructuredOutput('answer', definition, strict=False))
    request = LLMRequest('synthetic', CompletionRequest(
        (Message('user', (TextContent('Answer'),)),), response=policy))
    evidence = ResponseEvidence('prompted_json', 'reject', 'prompted_json',
        sha256(definition.encode()).hexdigest(), False, None)
    return request, evidence


def test_fenced_prompted_json_rejection_retains_exact_candidate_without_guessing_diagnostics():
    request, evidence = response_request()
    original = replace(candidate('```json\n{"answer":"oversized"}\n```'), response=evidence)
    with pytest.raises(CandidateOutputError) as caught:
        asyncio.run(async_validate_result(request, original))
    assert caught.value.result is original
    assert caught.value.response is evidence and caught.value.usage is original.usage
    assert caught.value.feedback == {'reason': 'response_schema', 'errors': []}
    assert 'oversized' not in str(caught.value) and 'oversized' not in repr(caught.value)


def test_unavailable_response_validator_never_admits_repair_candidate(monkeypatch):
    from api_agent.providers import rich_inference
    request, evidence = response_request()
    original = replace(candidate(), response=evidence)
    async def unavailable(*args):
        return None
    monkeypatch.setattr(rich_inference, 'async_schema_verdict', unavailable)
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(async_validate_result(request, original))
    assert type(caught.value) is ProviderAccessError
    assert caught.value.usage is original.usage
    assert not hasattr(caught.value, 'result')


@pytest.mark.parametrize('text', ['', '   '])
def test_actual_empty_completed_response_uses_typed_receipt_with_original_usage(text):
    request, evidence = response_request()
    original = replace(candidate(text), response=evidence)
    with pytest.raises(CandidateOutputError) as caught:
        asyncio.run(async_validate_result(request, original))
    assert caught.value.result is original
    assert caught.value.usage is original.usage
    assert caught.value.code == 'empty_final_answer'
    assert caught.value.feedback == {'reason': 'response_json', 'errors': []}


def test_schema_repair_diagnostics_are_capped_without_echoing_rejected_values():
    properties = {f'field_{index}': {'type': 'string', 'maxLength': 1} for index in range(17)}
    definition = json.dumps({'type': 'object', 'properties': properties,
                             'required': list(properties), 'additionalProperties': False})
    request = LLMRequest('synthetic', CompletionRequest(
        (Message('user', (TextContent('Answer'),)),),
        structured=StructuredOutput('answer', definition, strict=False)))
    original = candidate(json.dumps(dict.fromkeys(properties, 'synthetic-rejected-value')))
    with pytest.raises(CandidateOutputError) as caught:
        asyncio.run(async_validate_result(request, original))
    errors = caught.value.feedback['errors']
    assert len(errors) == 16
    assert errors[0] == {'path': ['field_0'], 'keyword': 'maxLength', 'constraint': 1,
                         'actual': len('synthetic-rejected-value')}
    assert errors[-1]['path'] == ['field_15']
    assert 'synthetic-rejected-value' not in json.dumps(caught.value.feedback)
    assert caught.value.result is original
