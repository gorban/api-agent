"""Confirmed tool-input rejection stays actionable across provider and IPC boundaries."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from api_agent.llm_providers.gpt import rich as gpt
from api_agent.providers import bounded_schema, rich_inference as subject
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import (CompletionRequest, LLMRequest, LLMResult, Message,
    ResponseEvidence, ResponsePolicy, StructuredOutput, TextContent, Tool, ToolCall, Usage)
from api_agent.providers.setup_transport import _decode, _encode, _error, _raise_error


SCHEMA = json.dumps({'type': 'object', 'properties': {'query': {'type': 'string', 'maxLength': 64}},
                     'required': ['query'], 'additionalProperties': False})


@pytest.fixture(params=['shared_sync', 'shared_async', 'gpt_sync', 'gpt_async'])
def validate(request):
    if request.param == 'shared_sync':
        return subject.validate_result
    if request.param == 'gpt_sync':
        return gpt.validate_result
    function = subject.async_validate_result if request.param == 'shared_async' else gpt.async_validate_result
    return lambda req, result: asyncio.run(function(req, result))


def proposal(arguments='{}', name='second_read'):
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('Read both sources'),)),), tools=(
        Tool('first_read', 'Read first', SCHEMA, strict=False),
        Tool('second_read', 'Read second', SCHEMA, strict=False))))
    result = LLMResult(Message('assistant', calls=(
        ToolCall('first', 'first_read', '{"query":"hello"}'),
        ToolCall('second', name, arguments))), Usage(12, 3), 'tool_calls')
    return request, result


def test_second_call_missing_required_input_retains_safe_causal_diagnostic(validate):
    request, result = proposal()
    evidence = ResponseEvidence('text', 'reject', 'text', None, None, None)
    request = replace(request, payload=replace(request.payload, response=ResponsePolicy('text', 'reject')))
    result = replace(result, response=evidence)

    with pytest.raises(ProviderAccessError) as caught:
        validate(request, result)

    error = caught.value
    assert error.message == ('Generated arguments for tool call 2 (declared tool 2) '
                             'do not match its input schema: missing required property')
    assert error.code == 'tool_arguments_schema'
    assert error.type == 'tool_call_validation'
    assert error.origin == 'local_output_validation'
    assert error.usage == result.usage
    assert error.response == evidence
    with pytest.raises(ProviderAccessError) as restored:
        _raise_error(_decode(_encode(_error(error))))
    assert restored.value.message == error.message
    assert restored.value.code == error.code
    assert restored.value.type == error.type
    assert restored.value.origin == error.origin
    assert restored.value.usage == result.usage
    assert restored.value.response == evidence


@pytest.mark.parametrize('arguments,code,detail', [
    ('{"query":17}', 'tool_arguments_schema', 'do not match its input schema'),
    ('not-json', 'tool_arguments_json', 'expected a bounded, unambiguous JSON object'),
    ('{"query":"a","query":"b"}', 'tool_arguments_json', 'expected a bounded, unambiguous JSON object'),
])
def test_invalid_arguments_preserve_classification_without_echoing_content(validate, arguments, code, detail):
    request, result = proposal(arguments)

    with pytest.raises(ProviderAccessError, match=detail) as caught:
        validate(request, result)

    assert caught.value.code == code
    assert arguments not in str(caught.value)


def test_unknown_tool_diagnostic_never_echoes_untrusted_name(validate):
    request, result = proposal(name='untrusted_name')

    with pytest.raises(ProviderAccessError) as caught:
        validate(request, result)

    assert caught.value.message == 'Generated tool call 2 names an undeclared tool'
    assert caught.value.code == 'tool_name_undeclared'
    assert 'untrusted_name' not in str(caught.value)


def test_unavailable_validation_is_not_a_confirmed_input_error(validate, monkeypatch):
    request, result = proposal('{"query":"hello"}')
    monkeypatch.setattr(bounded_schema, 'isolated_schema_verdict', lambda *args: None)

    async def unavailable(*args):
        return None
    monkeypatch.setattr(bounded_schema, 'async_isolated_schema_verdict', unavailable)

    with pytest.raises(ProviderAccessError, match='local schema validation is unavailable') as caught:
        validate(request, result)

    assert caught.value.code == 'tool_validation_unavailable'
    assert caught.value.usage == result.usage


@pytest.mark.parametrize('asynchronous', [False, True])
def test_invalid_declared_schema_cannot_request_argument_correction(asynchronous):
    request, result = proposal()
    request = replace(request, payload=replace(request.payload, tools=(
        replace(request.payload.tools[0], input_schema='{"type":"object","$ref":"https://example.invalid/schema"}'),
        *request.payload.tools[1:])))

    with pytest.raises(ProviderAccessError) as caught:
        if asynchronous:
            asyncio.run(subject.async_validate_result(request, result))
        else:
            subject.validate_result(request, result)

    assert caught.value.code == 'tool_validation_unavailable'


def test_invalid_result_shape_remains_a_contract_error():
    request, _ = proposal()

    with pytest.raises(ContractError, match='Invalid inference result'):
        subject.validate_result(request, object())


def test_schema_registry_retrieval_never_contacts_remote_resources():
    from referencing.exceptions import NoSuchResource

    with pytest.raises(NoSuchResource):
        subject._no_remote('https://example.invalid/schema')


def test_policy_output_invalid_json_is_not_a_tool_argument_error():
    request, _ = proposal()
    definition = StructuredOutput('answer', SCHEMA, strict=False)
    policy = ResponsePolicy('json', 'reject', definition)
    request = replace(request, payload=replace(request.payload, response=policy))
    evidence = ResponseEvidence('json', 'reject', 'json', sha256(SCHEMA.encode()).hexdigest(), False, None)
    result = LLMResult(Message('assistant', (TextContent('not-json'),)), Usage(9, 2), response=evidence)

    with pytest.raises(ProviderAccessError, match='Response output does not match its original schema') as caught:
        subject.validate_response_result(request, result)

    assert caught.value.code is None
    assert caught.value.usage == result.usage


@pytest.mark.parametrize('origin', [None, 'upstream_body', 1, []])
def test_error_origin_must_be_a_known_local_contract_value(origin):
    with pytest.raises(ContractError, match='Invalid provider error origin'):
        ProviderAccessError('unknown', 'Synthetic failure', origin=origin)


def test_mutated_origin_is_not_transported_as_local_validation():
    error = ProviderAccessError('unknown', 'Synthetic failure')
    error.origin = []

    with pytest.raises(ProviderAccessError) as restored:
        _raise_error(_decode(_encode(_error(error))))

    assert restored.value.origin == 'provider'


@pytest.mark.parametrize('origin', [None, 'upstream_body', 1, []])
def test_invalid_origin_in_an_ipc_frame_rejects(origin):
    frame = _error(ProviderAccessError('unknown', 'Synthetic failure'))
    frame['details']['origin'] = origin

    with pytest.raises(ContractError, match='Invalid provider response'):
        _raise_error(_decode(_encode(frame)))
