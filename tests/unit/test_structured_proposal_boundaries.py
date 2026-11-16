"""Boundary checks for owner-issued structured proposal capabilities."""

import asyncio
import json
import pickle
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import Message, TextContent, ToolResult
from api_agent.providers.structured_proposals import (
    StructuredReplayAuthority,
    StructuredWorkerAdmission,
    _checked_provider_controls,
    _wire_digest,
    bind_structured_source_request,
    current_structured_source_request,
)
from test_structured_replay_authority import original, proposal, replay


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {1: 'value'}, {'x': object()}, '\ud800', 'x' * 2097153, [0] * 8193])
def test_replay_evidence_rejects_non_json_or_oversized_values(value):
    with pytest.raises(ContractError, match='Invalid structured replay evidence'):
        _wire_digest(value)


def test_replay_evidence_retains_json_numeric_types():
    assert _wire_digest({'value': 1}) != _wire_digest({'value': 1.0})
    assert _wire_digest({'b': 2, 'a': 1}) == _wire_digest({'a': 1, 'b': 2})


@pytest.mark.parametrize('controls', [[], {'x': True}, {'x': -1}, {'x': 1048577}, {1: 2}, {'x' * 129: 2}, {str(i): 1 for i in range(9)}, {'reasoning_format': 'unknown'}])
def test_worker_controls_reject_invalid_values(controls):
    with pytest.raises(ContractError, match='Invalid structured provider controls'):
        _checked_provider_controls(controls)


def test_worker_controls_copy_valid_values():
    controls = {'reasoning_format': 'tagged', 'budget': 0}
    checked = _checked_provider_controls(controls)
    controls['budget'] = 5
    assert checked == {'reasoning_format': 'tagged', 'budget': 0}
    assert _checked_provider_controls(None) == {}


@pytest.mark.asyncio
async def test_source_binding_rejects_nesting_and_expires_in_inherited_context():
    gate = asyncio.Event()
    async def child():
        await gate.wait()
        with pytest.raises(ContractError, match='expired'):
            current_structured_source_request()

    request = original()
    assert current_structured_source_request() is None
    with bind_structured_source_request(request):
        assert current_structured_source_request() is request
        with pytest.raises(ContractError, match='already bound'):
            with bind_structured_source_request(request):
                pass
        task = asyncio.create_task(child())
    gate.set()
    await task
    assert current_structured_source_request() is None


@pytest.mark.asyncio
async def test_receipt_expiry_and_close_revoke_authority():
    now = [0.0]
    authority = StructuredReplayAuthority(clock=lambda: now[0])
    request, result = original(), proposal()
    first = await authority.issue(request, result)
    with pytest.raises(TypeError):
        pickle.dumps(first)
    now[0] = 300.0
    with pytest.raises(ContractError, match='unavailable'):
        authority.consume(replay(request, result, first))
    second = await authority.issue(request, result)
    authority.close()
    with pytest.raises(ContractError, match='unavailable'):
        authority.consume(replay(request, result, second))
    with pytest.raises(ContractError, match='unavailable'):
        await authority.issue(request, result)


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback', [
    'not json',
    json.dumps({'error': 'other', 'issues': [], 'truncated': False}),
    json.dumps({'error': 'structured_validation_failed', 'issues': [], 'truncated': 0}),
    json.dumps({'error': 'structured_validation_failed', 'issues': [{}] * 17, 'truncated': False}),
    'x' * 16385,
])
async def test_replay_feedback_cannot_supply_untrusted_authority(feedback):
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    receipt = await authority.issue(request, result)
    prepared = replay(request, result, receipt)
    tool = Message('tool', results=(ToolResult('wire-id', (TextContent(feedback),)),))
    prepared = replace(prepared, payload=replace(prepared.payload, messages=(*prepared.payload.messages[:-1], tool)))
    with pytest.raises(ContractError):
        authority.consume(prepared)
    # Rejection must not consume the valid receipt.
    authority.consume(replay(request, result, receipt))


def test_worker_proof_cannot_be_issued_after_close():
    issuer = StructuredWorkerAdmission(b'x' * 32)
    row = request_to_wire(original())
    issuer.close()
    with pytest.raises(ContractError, match='Invalid structured worker admission'):
        issuer.proof(row, {'model': 'selected'}, 'completions')
