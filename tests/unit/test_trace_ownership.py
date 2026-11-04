"""Synthetic ownership challenges reject altered, replayed and uncertain grants."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.telemetry.trace_ownership import (RemoteHandoff, accept_receipt,
    begin_remote, bind_handoff, receipt, settle_trace_ownership, validate_offer, validate_parent)

CAPABILITY = 'synthetic-ownership-capability-000000'
PARENT = dict(version=1, trace_id='1' * 32, span_id='2' * 16, trace_flags=1)
OFFER = dict(parent=PARENT, pipeline='3' * 64, nonce='4' * 64)
REQUEST = {'model': 'synthetic-model'}
BINDING = {'selected_identity': 'synthetic-account', 'worker_id': 'worker'}


@pytest.mark.parametrize('field,value', [('version', True), ('version', 2), ('trace_id', '0'*32),
    ('trace_id', 'f'*33), ('trace_id', 'F'*32), ('span_id', '0'*16), ('span_id', 'a'*15),
    ('trace_flags', True), ('trace_flags', 2), ('baggage', {'secret': 'excluded'})])
def test_parent_rejects_noncontract_context(field, value):
    with pytest.raises(ContractError):
        validate_parent(dict(PARENT, **{field: value}))


@pytest.mark.parametrize('value', [None, {}, dict(OFFER, pipeline='short'), dict(OFFER, nonce=True),
                                 dict(OFFER, extra='excluded')])
def test_offer_rejects_noncontract_fields(value):
    with pytest.raises(ContractError):
        validate_offer(value)


@pytest.mark.parametrize('state', ['granted', 'inactive'])
def test_signed_receipt_matches_exact_call_and_account(state):
    row = receipt(CAPABILITY, OFFER, 1, REQUEST, BINDING, state)
    assert accept_receipt(row, CAPABILITY, OFFER, 1, REQUEST, BINDING) == state


@pytest.mark.parametrize('change', ['capability', 'offer', 'sequence', 'request', 'binding'])
def test_validly_signed_receipt_cannot_be_replayed_for_changed_call(change):
    row = receipt(CAPABILITY, OFFER, 1, REQUEST, BINDING, 'granted')
    args = dict(capability=CAPABILITY, offer=OFFER, sequence=1, request=REQUEST, binding=BINDING)
    args[change] = dict(capability='another-valid-capability', offer=dict(OFFER, nonce='5'*64),
        sequence=2, request={'model': 'different'}, binding=dict(BINDING, selected_identity='other'))[change]
    with pytest.raises(ContractError):
        accept_receipt(row, **args)


@pytest.mark.parametrize('field,value', [('state', 'inactive'), ('proof', 'f'*64), ('call_sequence', True),
    ('version', True), ('request_sha256', 'a'*64), ('extra', 'excluded')])
def test_tampered_receipt_is_rejected(field, value):
    row = receipt(CAPABILITY, OFFER, 1, REQUEST, BINDING, 'granted')
    row[field] = value
    with pytest.raises(ContractError):
        accept_receipt(row, CAPABILITY, OFFER, 1, REQUEST, BINDING)


def owner(*, enabled=True, pipeline=OFFER['pipeline'], valid=True):
    call = Mock()
    call.span.export_context.return_value = PARENT if valid else None
    return SimpleNamespace(enabled=enabled, runtime=SimpleNamespace(pipeline_id=pipeline),
                           begin=Mock(return_value=call)), call


def test_grant_follows_real_span_creation_and_cannot_be_repeated():
    trace, call = owner()
    acknowledged = []
    def acknowledge(state):
        trace.begin.assert_called_once_with('call', parent=PARENT)
        call.span.export_context.assert_called_once()
        acknowledged.append(state)
    handoff = RemoteHandoff(OFFER, acknowledge)
    with bind_handoff(handoff):
        assert begin_remote(trace, 'call') is call
        settle_trace_ownership()
        assert begin_remote(trace, 'call') is None
    assert acknowledged == ['granted']


@pytest.mark.parametrize('enabled,pipeline,valid', [(False, OFFER['pipeline'], True),
    (True, 'a'*64, True), (True, OFFER['pipeline'], False)])
def test_inactive_before_grant_permits_fallback(enabled, pipeline, valid):
    trace, call = owner(enabled=enabled, pipeline=pipeline, valid=valid)
    acknowledged = []
    handoff = RemoteHandoff(OFFER, acknowledged.append)
    assert handoff.begin(trace, 'call') is None
    handoff.settle()
    assert acknowledged == ['inactive']
    if not enabled or pipeline != OFFER['pipeline']:
        trace.begin.assert_not_called()
    else:
        call.end.assert_called_once_with(failed=True)


def test_acknowledgement_failure_after_grant_never_becomes_inactive():
    trace, call = owner()
    acknowledge = Mock(side_effect=OSError('synthetic write failure'))
    handoff = RemoteHandoff(OFFER, acknowledge)
    with pytest.raises(OSError):
        handoff.begin(trace, 'call')
    handoff.settle()
    assert handoff.state == 'granted'
    acknowledge.assert_called_once_with('granted')
    call.end.assert_called_once_with(failed=True)


def test_no_selected_host_tracing_settles_once_and_context_does_not_escape():
    acknowledged = []
    with bind_handoff(RemoteHandoff(OFFER, acknowledged.append)):
        settle_trace_ownership()
        settle_trace_ownership()
    settle_trace_ownership()
    assert acknowledged == ['inactive']


@pytest.mark.parametrize('state,sequence', [('unknown', 1), ('granted', True), ('inactive', 0)])
def test_receipt_cannot_sign_noncontract_state_or_sequence(state, sequence):
    with pytest.raises(ContractError):
        receipt(CAPABILITY, OFFER, sequence, REQUEST, BINDING, state)


@pytest.mark.parametrize('row', [None, {}, {'state': 'unknown'}])
def test_receipt_rejects_noncontract_shape(row):
    with pytest.raises(ContractError):
        accept_receipt(row, CAPABILITY, OFFER, 1, REQUEST, BINDING)


def test_host_that_cannot_create_call_declines_before_execution():
    trace, _ = owner()
    trace.begin.return_value = None
    acknowledged = []
    handoff = RemoteHandoff(OFFER, acknowledged.append)
    assert handoff.begin(trace, 'call') is None
    assert acknowledged == ['inactive']
