"""Opt-in proposal boundary contracts; default inference remains unchanged."""

import json
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_from_wire, request_to_wire, result_from_wire, result_to_wire
from api_agent.providers.llm import (
    AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message,
    StructuredOutput, StructuredFunctionProfile, StructuredProposal, TextContent, ToolCall, Usage,
)
from api_agent.providers.rich_inference import async_validate_result

PROFILE = StructuredFunctionProfile(None, 'converter')
SCHEMA = json.dumps({'type': 'object', 'properties': {'reference': {'type': 'string'}}, 'required': ['reference']})
STRICT_SCHEMA = json.dumps({'type': 'object', 'properties': {'reference': {'type': 'string'}}, 'required': ['reference'], 'additionalProperties': False})


def request(*, provider_strict=None):
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Read invoices.'),)),),
        structured=StructuredOutput('InvoiceResponse', SCHEMA, strict=False,
            encoding='function_calling', provider_tool_strict=provider_strict, provider_tool_schema=STRICT_SCHEMA if provider_strict is True else SCHEMA, function_profile=PROFILE)), 'request-one')


def result(arguments='{}', *, call_id='provider-id', name='InvoiceResponse'):
    call = ToolCall(call_id, name, arguments)
    return LLMResult(Message('assistant', calls=(call,)), usage=Usage(4, 2), outcome='tool_calls',
        actual_model='selected', structured_proposal=StructuredProposal(call))


@pytest.mark.parametrize('provider_strict', [None, False, True])
def test_function_encoding_strict_omission_round_trips(provider_strict):
    value = request(provider_strict=provider_strict)
    wire = request_to_wire(value)
    assert wire['structured']['encoding'] == 'function_calling'
    assert ('provider_tool_strict' in wire['structured']) is (provider_strict is not None)
    if provider_strict is not None:
        assert wire['structured']['provider_tool_strict'] is provider_strict
    assert request_from_wire(wire) == value
    assert result_from_wire(result_to_wire(result())) == result()


def test_default_structured_wire_stays_unchanged():
    plain = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Read'),)),),
        structured=StructuredOutput('InvoiceResponse', SCHEMA, strict=False)))
    row = request_to_wire(plain)
    assert row['version'] == 2
    assert 'encoding' not in row['structured'] and 'provider_tool_strict' not in row['structured']
    assert request_from_wire(row) == plain




@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [
    result('{"reference":"x","reference":"y"}'),
    result('{"reference":NaN}'), result('{'),
    result('{"reference":"ok"}', name='WrongResponse'),
])
async def test_ambiguous_malformed_or_wrong_name_never_becomes_replayable_proposal(bad):
    with pytest.raises((ContractError, ProviderAccessError)) as caught:
        await async_validate_result(request(), bad)
    assert not hasattr(caught.value, 'structured_replay_receipt')


def test_provider_cannot_embed_receipt_or_mismatch_proposal_call():
    value = result('{"reference":"ok"}')
    with pytest.raises((ValueError, ContractError)):
        replace(value, structured_proposal=StructuredProposal(ToolCall('other', 'InvoiceResponse', '{}')))


def test_capability_is_explicit_and_default_contract_unchanged():
    AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE).validate(request())
    with pytest.raises(Exception):
        AvailableSelection('selected', features=('structured',)).validate(request())


def test_proposal_capture_keeps_plaintext_but_never_sealed_state():
    from api_agent.providers.structured_proposals import proposal_capture_projection

    value = {
        'provider_request': {'messages': [{'role': 'user', 'content': 'Explain invoice data.'}],
                             'tools': [{'function': {'arguments': '{"receipt":"ordinary user field"}'}}]},
        'provider_response': {'content': [
            {'type': 'thinking', 'thinking': 'Plain reasoning.', 'signature': 'sealed-secret'},
            {'type': 'redacted_thinking', 'data': 'sealed-secret'},
            {'type': 'tool_use', 'id': 'native-id', 'name': 'InvoiceResponse',
             'input': {'receipt': 'ordinary user field', 'reference': 'invoice17'}},
        ], 'continuation': 'sealed-secret', 'encrypted_content': 'sealed-secret'},
        'native': {'toolUse': {'input': {'continuation': 'ordinary user key', 'reference': 'invoice18'}},
                   'reasoningContent': {'reasoningText': {'text': 'Native reasoning.', 'signature': 'sealed-secret'},
                                        'redactedContent': 'sealed-secret'}},
        'proposal_replay': 'receipt-secret', 'structured_worker_admission': {'proof': 'proof-secret'},
    }
    projected = proposal_capture_projection(value)
    text = json.dumps(projected)
    assert 'Explain invoice data.' in text and 'Plain reasoning.' in text and 'invoice17' in text
    assert 'ordinary user field' in text and 'ordinary user key' in text and 'Native reasoning.' in text
    assert 'sealed-secret' not in text and 'receipt-secret' not in text and 'proof-secret' not in text


def test_proposal_capture_rejects_unsafe_or_unbounded_objects_without_repr():
    from api_agent.providers.structured_proposals import proposal_capture_projection

    class Secret:
        def __repr__(self):
            raise AssertionError('private repr must not be called')

    with pytest.raises(ContractError):
        proposal_capture_projection({'value': Secret()})
    with pytest.raises(ContractError):
        proposal_capture_projection({'value': 'x' * 4_194_305})


@pytest.mark.parametrize('allow_content', [False, True])
@pytest.mark.parametrize('retain_raw', [False, True])
def test_proposal_capture_policy_preserves_plaintext_only_when_enabled(allow_content, retain_raw):
    from uuid import uuid4
    from api_agent.providers.structured_proposals import _new_admission_authority
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import capture_inference, collect_inference

    class Secret:
        def __repr__(self):
            raise AssertionError('private authority must never be formatted')

    wire = {'provider_response': {'content': [
        {'type': 'thinking', 'thinking': 'visible reasoning', 'signature': 'sealed-state'},
        {'type': 'tool_use', 'name': 'InvoiceResponse', 'input': {'reference': 'invoice17'}},
    ], 'continuation': 'sealed-state'}, 'proposal_replay': Secret(),
        'structured_worker_admission': {'proof': 'proof-secret'}}
    boundary = EmissionBoundary(ContentPolicy(allow_content))
    owner = object()
    authority = _new_admission_authority(owner)
    selection = AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE)
    with collect_inference(boundary, retain_raw=retain_raw) as collector:
        with authority.bind(owner, request(), selection):
            capture_inference('output', wire, str(uuid4()))
    text = json.dumps(collector['rows'])
    assert ('visible reasoning' in text) is allow_content
    assert ('invoice17' in text) is allow_content
    assert 'sealed-state' not in text and 'proof-secret' not in text


@pytest.mark.parametrize('log_content,trace_content', [(False, False), (False, True), (True, False), (True, True)])
def test_proposal_plaintext_obeys_independent_capture_policies(log_content, trace_content):
    from uuid import uuid4
    from api_agent.providers.structured_proposals import _new_admission_authority
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import capture_inference, collect_inference

    wire = {'provider_response': {'content': [{'type': 'tool_use', 'input': {'reference': 'invoice17'}},
        {'type': 'thinking', 'thinking': 'visible reasoning', 'signature': 'sealed-state'}]},
        'structured_worker_admission': {'proof': 'proof-secret'}}
    parent = EmissionBoundary(ContentPolicy(log_content))
    outputs = []
    owner = object()
    authority = _new_admission_authority(owner)
    selection = AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE)
    for boundary in (parent, parent.for_policy(ContentPolicy(trace_content))):
        with collect_inference(boundary) as collector:
            with authority.bind(owner, request(), selection):
                capture_inference('output', wire, str(uuid4()))
        outputs.append(json.dumps(collector['rows']))
    for text, enabled in zip(outputs, (log_content, trace_content), strict=True):
        assert ('invoice17' in text) is enabled
        assert ('visible reasoning' in text) is enabled
        assert 'sealed-state' not in text and 'proof-secret' not in text




def test_untrusted_scope_binding_cannot_issue_worker_authority():
    from api_agent.providers.structured_proposals import (_StructuredAdmissionAuthority, _new_admission_authority, issue_worker_control)
    with pytest.raises(ContractError):
        issue_worker_control({'model': 'selected'}, 'chat/completions')
    with pytest.raises(ContractError):
        _StructuredAdmissionAuthority(object(), object())
    owner = object()
    authority = _new_admission_authority(owner)
    selection = AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE)
    with pytest.raises(ContractError):
        with authority.bind(object(), request(), selection):
            pass


def test_profile_catalog_compatibility_and_mismatch_fail_closed():
    from api_agent.providers.catalog import catalog_from_wire, catalog_to_wire

    ordinary = AvailableSelection('ordinary', features=('structured',))
    assert 'structured_function_profile' not in catalog_to_wire((ordinary,))[0]
    selected = AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE)
    encoded = catalog_to_wire((selected,))
    assert encoded[0]['structured_function_profile'] == {'default_strict': None, 'schema_policy': 'converter'}
    assert catalog_from_wire(encoded) == (selected,)
    conflicting = AvailableSelection('selected', features=('structured', 'structured_function'),
        structured_function_profile=StructuredFunctionProfile(True, 'strict_all_properties'))
    with pytest.raises(Exception):
        conflicting.validate(request())




@pytest.mark.asyncio
async def test_opt_in_requires_profile_even_when_catalog_was_previously_optional():
    from api_agent.providers.control import OperationalError
    from api_agent.providers.rich_inference import resolve_catalog_selection

    async def no_profile():
        return (AvailableSelection('selected', features=('structured',)),)

    with pytest.raises(OperationalError) as rejected:
        await resolve_catalog_selection(no_profile, request(), clamp_output_limit=True)
    assert rejected.value.kind == 'unsupported' and rejected.value.execution == 'not_started'


def test_function_capability_does_not_enable_existing_json_structured_mode():
    from dataclasses import replace
    from api_agent.providers.control import OperationalError
    selection = AvailableSelection('selected', features=('structured_function',),
                                   structured_function_profile=PROFILE)
    proposal = request()
    selection.validate(proposal)
    json_definition = replace(proposal.payload.structured, encoding='json_schema',
                              function_profile=None, provider_tool_schema=None)
    ordinary = replace(proposal, payload=replace(proposal.payload, structured=json_definition))
    with pytest.raises(OperationalError):
        selection.validate(ordinary)




def test_public_function_proposal_stream_rejects_before_provider_dispatch():
    from unittest.mock import Mock
    from api_agent.providers.control import OperationalError
    from api_agent.providers.loading import _StreamView
    provider = Mock()
    with pytest.raises(OperationalError) as rejected:
        _StreamView(provider).astream(request())
    assert rejected.value.kind == 'unsupported'
    provider.astream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{}', '{"reference":3}', '{"reference":"valid"}'])
async def test_function_proposals_preserve_bounded_object_regardless_of_schema_verdict(arguments):
    candidate = result(arguments)
    admitted = await async_validate_result(request(), candidate)
    assert admitted is candidate
    assert admitted.structured_proposal.call.arguments == arguments


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{"reference":1,"reference":2}', '{"reference":NaN}', '[1,2]', 'not-json'])
async def test_function_proposals_still_reject_malformed_ambiguous_or_nonobject_arguments(arguments):
    candidate = result(arguments)
    with pytest.raises(ProviderAccessError) as caught:
        await async_validate_result(request(), candidate)
    assert caught.value.code == 'tool_arguments_json'
    assert caught.value.origin == 'local_output_validation'
    assert caught.value.execution == 'completed'
    assert caught.value.usage is candidate.usage
    assert caught.value.response is candidate.response


@pytest.mark.asyncio
async def test_function_answer_hint_uses_complete_without_public_streaming():
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.llm import AnswerStream
    from api_agent.providers.presentation import answer_progress, complete_with_progress, progress_call_owner
    proposal = request()
    proposal = replace(proposal, payload=replace(proposal.payload,
        generation=replace(proposal.payload.generation, answer_stream=AnswerStream('answer', 'text'))))
    provider = Mock()
    provider.capabilities.operations = ('complete', 'stream')
    provider.acomplete = AsyncMock(return_value=result('{"reference":3}'))
    received = []
    async def sink(value):
        received.append(value)
    with answer_progress(sink), progress_call_owner('node', 'attempt'):
        response = await complete_with_progress(provider, proposal)
    assert response.structured_proposal.call.arguments == '{"reference":3}'
    provider.acomplete.assert_awaited_once()
    provider.astream.assert_not_called()
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{"reference":1,"reference":2}', '{"reference":NaN}', 'bad', 'x' * 524289])
async def test_invalid_proposal_structure_preserves_safe_completed_usage(arguments):
    candidate = result(arguments)
    with pytest.raises(ProviderAccessError) as caught:
        await async_validate_result(request(), candidate)
    error = caught.value
    assert error.code == 'tool_arguments_json'
    assert error.origin == 'local_output_validation'
    assert error.execution == 'completed'
    assert error.usage is candidate.usage
    assert error.response is candidate.response
    assert arguments not in str(error)


@pytest.mark.asyncio
async def test_call_progress_does_not_publish_unparsed_schema_proposal_as_tool_or_answer():
    from api_agent.providers.presentation import call_progress, publish_call_progress
    candidate = result('{"reference":3}')
    candidate = replace(candidate, payload=replace(candidate.payload, content=(TextContent('unparsed-content'),)))
    received = []
    async def sink(value):
        received.append(value)
    with call_progress(sink):
        await publish_call_progress('node', 'attempt', 'request', candidate)
    assert len(received) == 1
    assert received[0].tool_calls == () and received[0].response_text == ''
