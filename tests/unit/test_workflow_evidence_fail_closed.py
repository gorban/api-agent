"""Reject ambiguous and oversized workflow evidence before publication."""
import asyncio
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.evidence_placement import graph_for_visit
from api_agent.testing.workflow_steps import attach_payloads
from api_agent.workflows.capture import project_values


@pytest.mark.parametrize('value', [{str(i): i for i in range(2049)}, list(range(2049))])
def test_oversized_original_carrier_is_truncated_before_projection(value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
    report = set()
    assert project_values({'payload': value}, {'type': 'object'}, boundary, report=report) == {}
    assert report == {'truncated'}


def test_graph_evidence_requires_unique_invocation_and_observed_ancestor():
    graph, node = str(uuid4()), str(uuid4())
    journal = {'invocation_id': 'invocation', 'events': [
        {'event': 'scope_start', 'kind': 'graph', 'scope_id': graph, 'parent_id': None},
        {'event': 'scope_start', 'kind': 'node', 'scope_id': node, 'parent_id': graph}]}
    assert graph_for_visit([journal], 'invocation', node) == {
        'kind': 'scope', 'invocation_id': 'invocation', 'scope_id': graph}
    assert graph_for_visit([journal, journal], 'invocation', node) == {'kind': 'unattached'}
    assert graph_for_visit([journal], 'invocation', 'missing') == {'kind': 'unattached'}


def test_duplicate_transport_attempt_owners_do_not_receive_evidence():
    attempt = str(uuid4())
    child = {'name': 'attempt', 'parameters': [{'name': 'transport_id', 'value': attempt}]}
    step = {'name': 'call example', 'steps': [child, dict(child)], 'parameters': []}
    reference = {'name': 'response', 'source': 'response.json', 'type': 'application/json'}
    with pytest.raises(ValueError, match='no unique call owner'):
        attach_payloads([step], [], {'example': {(attempt, 'response'): reference}})
    assert 'attachments' not in child


def test_journal_rejects_unknown_capture_source():
    from test_workflow_journal_adversarial import _definition
    from api_agent.workflows.journal import WorkflowJournal
    journal = WorkflowJournal(_definition(), log_content=True, content_policy=ContentPolicy(True))
    with pytest.raises(ValueError, match='capture source'):
        journal.capture_json(visit_id=str(uuid4()), direction='input', values={}, schema={}, capture_source='unknown')
    assert journal.finalize()['events'] == []


def test_ui_start_requires_declared_authority_before_starting_backend(tmp_path):
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.ui_runtime import UIIntegrationOwner
    async def scenario():
        from unittest.mock import AsyncMock, Mock
        with authority_scope(('workflow',)) as scope:
            workflow = EmissionBoundary(ContentPolicy(True))
            scope.join('workflow', workflow)
            assert scope.authorities() == (workflow,)
            backend_factory = Mock()
            owner = UIIntegrationOwner({'config': {}, 'owner_root': tmp_path}, registry=None, backend=backend_factory)
            owner._start = AsyncMock()
            try:
                with pytest.raises(ContractError):
                    await owner.start()
                owner._start.assert_not_awaited()
                backend_factory.assert_not_called()
                with pytest.raises(ContractError):
                    scope.authorities()
            finally:
                await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value,message', [
    ('capture_source', 'provider', 'capture source'),
    ('transport_attempt_id', '00000000-0000-4000-8000-000000000001', 'transport capture identity'),
])
def test_call_event_cannot_claim_capture_provenance(field, value, message):
    from test_workflow_journal_adversarial import _complete, _rehash
    from api_agent.workflows.journal import validate_journal
    journal, _, _, _ = _complete()
    packet = journal.finalize()
    validate_journal(packet)
    call = next(event for event in packet['events'] if event['event'] == 'call')
    call[field] = value
    _rehash(packet)
    with pytest.raises(ValueError, match=message):
        validate_journal(packet)


@pytest.mark.parametrize('invalid', ['invalid', 42])
def test_capture_transport_identity_requires_canonical_uuid(invalid):
    from test_workflow_journal_adversarial import _complete, _rehash
    from api_agent.workflows.journal import validate_journal
    journal, _, visit, call = _complete()
    journal.capture_json(visit_id=visit, direction='input', cause_id=call, values={}, schema={})
    packet = journal.finalize()
    validate_journal(packet)
    capture = next(event for event in packet['events'] if event['event'] == 'capture')
    capture['transport_attempt_id'] = invalid
    _rehash(packet)
    with pytest.raises(ValueError, match='transport capture identity'):
        validate_journal(packet)
