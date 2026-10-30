from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api_agent.testing.payloads import PayloadStore
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.testing.workflow_steps import attach_payloads


def test_retry_payloads_keep_distinct_attempt_owners_and_chunk_limits(tmp_path):
    run = RunRecorder(tmp_path, RunIdentity(str(uuid4()), 'example', 'integration', datetime.now(timezone.utc)))
    store = PayloadStore(run, 'test')
    call, first, second = (str(uuid4()) for _ in range(3))
    requests = [{'text': 'a' * 220000}, {'text': 'b' * 220000}]
    refs = [store.put_wire(call, 'request', value, transport_attempt_id=attempt)
            for value, attempt in zip(requests, (first, second))]
    assert refs[0]['source'] != refs[1]['source']
    assert all((tmp_path / part).stat().st_size <= 200 * 1024
               for ref in refs for part in store.native_parts(ref))
    assert [entry['occurrence']['transport_attempt_id'] for entry in store.index()['entries']] == [first, second]
    with pytest.raises(ValueError, match='Conflicting'):
        store.put_wire(call, 'request', requests[1], transport_attempt_id=first)


def test_retry_attachments_belong_to_physical_attempt_and_are_not_duplicated():
    call, first, second = (str(uuid4()) for _ in range(3))
    attempts = [dict(name='transport attempt ' + str(i), status=status, steps=[],
                     parameters=[dict(name='transport_id', value=attempt)])
                for i, (attempt, status) in enumerate(((first, 'broken'), (second, 'passed')), 1)]
    steps = [dict(name='call ' + call, status='broken', parameters=[], steps=attempts)]
    first_ref = dict(name='request first', source='first.json', type='application/json')
    second_ref = dict(name='request second', source='second.json', type='application/json')
    phases = {call: {(first, 'request'): [first_ref], (second, 'request'): [second_ref]}}
    assert attach_payloads(steps, [], phases) == []
    assert attach_payloads(steps, [], phases) == []
    assert attempts[0]['attachments'] == [first_ref]
    assert attempts[1]['attachments'] == [second_ref]


def test_retry_records_failed_and_successful_dispatch_separately(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers import inference_retry
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference, select_capture_rows

    monkeypatch.setattr(inference_retry, 'retry_delay', lambda *args: 0)
    calls = []
    async def provider(request, *, auth):
        calls.append(request)
        if len(calls) == 1:
            error = ProviderAccessError('unavailable', 'synthetic', status=503)
            error.http_status = 503
            raise error
        return LLMResult(Message('assistant', (TextContent('success'),)))
    async def authenticate(callback):
        return await callback(auth='synthetic-auth')
    child = SimpleNamespace(call=authenticate, aclose=AsyncMock())
    shared = SharedSetupOwner('operation', 'selected', lambda credentials: child, complete=provider, max_attempts=2)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    ledger = TransportAttemptLedger(2)
    async def run():
        try:
            with bind_transport_attempt_ledger(ledger), collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
                await shared.complete(request)
                return select_capture_rows(collector['rows'])
        finally:
            await shared.aclose()
    rows = asyncio.run(run())
    first, second = ledger.snapshot()['attempts']
    assert [first['outcome'], second['outcome']] == ['failed', 'completed']
    assert [(row['phase'], row['transport_attempt_id']) for row in rows] == [
        ('request', first['id']), ('request', second['id']), ('response', second['id'])]


def test_retry_occurrences_share_the_four_mib_test_budget(tmp_path):
    from api_agent.testing.payloads import PayloadLimitError

    run = RunRecorder(tmp_path, RunIdentity(str(uuid4()), 'example', 'integration', datetime.now(timezone.utc)))
    store = PayloadStore(run, 'test')
    call, first, second = (str(uuid4()) for _ in range(3))
    store.put_wire(call, 'request', {'text': 'a' * (3 * 1024 * 1024)}, transport_attempt_id=first)
    with pytest.raises(PayloadLimitError):
        store.put_wire(call, 'request', {'text': 'b' * (2 * 1024 * 1024)}, transport_attempt_id=second)
    store.omit_wire(call, 'request', transport_attempt_id=second)
    entries = store.index()['entries']
    assert entries[0]['occurrence']['transport_attempt_id'] == first
    assert entries[1]['occurrence']['transport_attempt_id'] == second
    assert entries[1]['reason'] == 'truncated'


def test_attempt_payload_without_matching_step_remains_unattached():
    call, attempt = str(uuid4()), str(uuid4())
    owner = dict(name='call ' + call, status='broken', parameters=[], steps=[])
    reference = dict(name='response', source='response.json', type='application/json')
    remaining = attach_payloads([owner], [], {call: {(attempt, 'response'): [reference]}})
    assert remaining == [dict(reference, name='Unattached response')]
    assert 'attachments' not in owner


@pytest.mark.parametrize('missing', ['call', 'ledger', 'mismatched_attempt'])
def test_attempt_capture_requires_matching_ledger_owner(missing):
    from api_agent.providers.generation_attempts import TransportAttemptLedger
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    from test_workflow_journal import definition

    graph, visit, call, wrong = (str(uuid4()) for _ in range(4))
    ledger = TransportAttemptLedger(1)
    attempt = ledger.begin(1)
    from api_agent.providers.llm import LLMResult, Message, TextContent
    ledger.finish(attempt, result=LLMResult(Message('assistant', (TextContent('ok'),))))
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id=graph, kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', source='START', target='Make', owner='GRAPH', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, kind='node', owner='Make', started_ms=1))
    journal.capture_json(visit_id=visit, direction='input', values={}, schema={'type': 'object'}, cause_id=call,
                         transport_attempt_id=wrong if missing == 'mismatched_attempt' else attempt)
    if missing != 'call':
        journal.record_call(visit_id=visit, call_id=call, transport_attempts=ledger.snapshot() if missing != 'ledger' else None)
    journal.observe(dict(event='scope_end', scope_id=visit, parent_id=graph, kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', source='Make', target='END', owner='GRAPH', implicit=True, visit_id=visit))
    journal.observe(dict(event='scope_end', scope_id=graph, kind='graph', owner='GRAPH', started_ms=1, stopped_ms=2, outcome='passed'))
    with pytest.raises(ValueError, match='lacks its call attempt owner'):
        validate_journal(journal.finalize())


def test_attempt_occurrence_rejects_conflicting_capture_provenance(tmp_path):
    run = RunRecorder(tmp_path, RunIdentity(str(uuid4()), 'example', 'integration', datetime.now(timezone.utc)))
    store = PayloadStore(run, 'test')
    call, attempt = str(uuid4()), str(uuid4())
    store.put_wire(call, 'response', {'normalized': 'ok'}, transport_attempt_id=attempt, capture_source='fallback')
    assert store.index()['entries'][0]['occurrence']['capture_source'] == 'fallback'
    with pytest.raises(ValueError, match='Conflicting wire capture source'):
        store.put_wire(call, 'response', {'raw': 'ok'}, transport_attempt_id=attempt, capture_source='provider')


def test_index_rejects_duplicate_attempt_phase_with_different_sources():
    from api_agent.testing.payloads import validate_payload_index

    call, attempt = str(uuid4()), str(uuid4())
    occurrence = dict(kind='llm', call_id=call, phase='response', transport_attempt_id=attempt)
    entries = [dict(kind='omitted', test_id='test', reason='truncated',
                    occurrence=dict(occurrence, capture_source=source))
               for source in ('fallback', 'provider')]
    with pytest.raises(ValueError, match='Conflicting wire capture source'):
        validate_payload_index(dict(version=1, test_id='test', entries=entries), {'files': {}})
