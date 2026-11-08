"""Canonical local inference capture must retain its terminal call owner."""
import asyncio

import pytest

from api_agent.providers.control import OperationalError
from api_agent.telemetry.privacy import ContentPolicy
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from test_local_llm import request, selected
from test_workflow_observations import definition


@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('failure_type,outcome', [(None, 'completed'),
    (OperationalError, 'failed'), (asyncio.CancelledError, 'cancelled')])
def test_local_physical_capture_joins_real_journal_call(selected, failure_type, outcome, log_content):
    owner, implementation, _, _, _ = selected
    original_error = failure_type('unknown') if failure_type else None
    if failure_type:
        async def failure(value, *, auth):
            raise original_error
        implementation.acomplete = failure
    journal = WorkflowJournal(definition(), log_content=log_content, content_policy=ContentPolicy(log_content))
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def run():
        try:
            with bind_workflow_visit(journal, 'visit'):
                if failure_type:
                    with pytest.raises(failure_type) as caught:
                        await owner.complete(request())
                    if failure_type is OperationalError:
                        assert caught.value is original_error
                else:
                    await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    payload = journal.finalize()
    validate_journal(payload)
    calls = [event for event in payload['events'] if event['event'] == 'call']
    captures = [event for event in payload['events'] if event['event'] == 'capture'
                and event.get('transport_attempt_id')]
    assert len(calls) == 1 and captures
    assert calls[0]['outcome'] == outcome
    assert {event['cause_id'] for event in captures} == {calls[0]['call_id']}
    assert calls[0]['visit_id'] == 'visit'


def test_local_retry_keeps_distinct_physical_attempts_under_one_call(selected, monkeypatch):
    from api_agent.providers import inference_retry
    owner, implementation, _, _, _ = selected
    original = implementation.acomplete
    attempts = []
    async def retried(value, *, auth):
        attempts.append(value)
        if len(attempts) == 1:
            raise OperationalError('network')
        return await original(value, auth=auth)
    implementation.acomplete = retried
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda *args: 0)
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def run():
        try:
            with bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    payload = journal.finalize()
    validate_journal(payload)
    calls = [event for event in payload['events'] if event['event'] == 'call']
    captures = [event for event in payload['events'] if event['event'] == 'capture'
                and event.get('transport_attempt_id')]
    assert len(calls) == len(owner.usage) == 1
    assert len(attempts) == 2
    physical = {row['id'] for row in calls[0]['transport_attempts']['attempts']}
    assert len(physical) == 2
    assert {event['transport_attempt_id'] for event in captures} == physical
    assert {event['cause_id'] for event in captures} == {calls[0]['call_id']}


@pytest.mark.parametrize('central_calls,shared_attempt', [(1, False), (2, False), (2, True)])
def test_unproved_central_transport_capture_fails_closed_without_extra_charge(selected, caplog, central_calls, shared_attempt):
    from uuid import uuid4
    from api_agent.providers.generation_attempts import _CURRENT
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.workflows.attribution import current_workflow_visit
    owner, implementation, _, _, _ = selected
    identifiers = [str(uuid4()) for _ in range(central_calls)]
    async def published(value, *, auth):
        journal, visit, attempt = current_workflow_visit()
        for identifier in identifiers:
            journal.record_call(visit_id=visit, attempt_id=attempt, call_id=identifier,
                usage={'input_tokens': 2, 'output_tokens': 3}, selection='sample', outcome='completed',
                **({'transport_attempts': _CURRENT.get().snapshot()} if shared_attempt else {}))
        return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(2, 3))
    implementation.acomplete = published
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def run():
        try:
            with bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    payload = journal.finalize()
    validate_journal(payload)
    assert {event['call_id'] for event in payload['events'] if event['event'] == 'call'} == set(identifiers)
    assert {row['call_id'] for row in owner.usage} == set(identifiers)
    assert not [event for event in payload['events'] if event['event'] == 'capture']
    assert 'physical owner unavailable' in caplog.text


@pytest.mark.parametrize('central_capture', [False, True])
def test_proved_central_attempt_retains_capture_without_duplicate(selected, central_capture):
    from uuid import uuid4
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.providers.generation_attempts import _CURRENT, current_transport_attempt
    from api_agent.workflows.attribution import current_workflow_visit
    from api_agent.workflows.inference_capture import capture_rows
    owner, implementation, _, _, _ = selected
    identifier = str(uuid4())
    async def published(value, *, auth):
        journal, visit, attempt = current_workflow_visit()
        journal.record_call(visit_id=visit, attempt_id=attempt, call_id=identifier,
            usage={'input_tokens': 2, 'output_tokens': 3}, selection='sample', outcome='completed',
            transport_attempts=_CURRENT.get().snapshot())
        if central_capture:
            # This controlled fixture stands for a central publisher's already
            # admitted provider projection; it contains no credentials.
            capture_rows([dict(phase='request', content={}, reason='withheld',
                transport_attempt_id=current_transport_attempt(), capture_source='provider')], identifier)
        return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(2, 3))
    implementation.acomplete = published
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def run():
        try:
            with bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    payload = journal.finalize()
    validate_journal(payload)
    calls = [event for event in payload['events'] if event['event'] == 'call']
    captures = [event for event in payload['events'] if event['event'] == 'capture']
    assert len(calls) == len(owner.usage) == 1
    assert calls[0]['call_id'] == owner.usage[0]['call_id'] == identifier
    assert len(captures) == 2
    assert {event['cause_id'] for event in captures} == {identifier}
    assert len({event['transport_attempt_id'] for event in captures}) == 1
    if central_capture:
        assert next(event for event in captures if event['direction'] == 'input')['capture_source'] == 'provider'


@pytest.mark.parametrize('invalid', ['object', 'nan', 'bytes'])
def test_deferred_invalid_carrier_never_masks_original_failure(invalid):
    from uuid import uuid4
    from api_agent.workflows.inference_capture import defer_inference_captures, _publish_admitted_rows
    error = OperationalError('unknown')
    value = {'object': object(), 'nan': float('nan'), 'bytes': b'not-json'}[invalid]
    with pytest.raises(OperationalError) as caught:
        with defer_inference_captures() as state:
            _publish_admitted_rows([dict(phase='request', content={'invalid': value})], str(uuid4()))
            raise error
    assert caught.value is error
    assert state['lost'] is True and state['rows'] == []


@pytest.mark.parametrize('limit', ['rows', 'bytes'])
def test_deferred_retention_is_bounded(limit):
    from uuid import uuid4
    from api_agent.workflows.inference_capture import defer_inference_captures, _publish_admitted_rows
    identifier = str(uuid4())
    with defer_inference_captures() as state:
        if limit == 'rows':
            for _ in range(129):
                _publish_admitted_rows([dict(phase='request', content={})], identifier)
            assert len(state['rows']) == 128
        else:
            _publish_admitted_rows([dict(phase='request', content={'text': 'x' * (4 * 1024 * 1024)})], identifier)
            assert state['rows'] == []
    assert state['lost'] is True


def test_auth_scoped_capture_is_scrubbed_before_deferred_retention():
    import json
    from uuid import uuid4
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import (
        bind_inference_call, capture_inference, defer_inference_captures)
    journal = WorkflowJournal(definition(), log_content=True,
        content_policy=ContentPolicy(True), boundary=EmissionBoundary(ContentPolicy(True), secrets=['private-marker']))
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    with bind_workflow_visit(journal, 'visit'), defer_inference_captures() as state:
        with bind_inference_call(str(uuid4())):
            capture_inference('input', {'message': 'Prompt private-marker'}, str(uuid4()), auth='private-marker')
        assert state['rows']
        assert 'private-marker' not in json.dumps(state['rows'])
        assert '[REDACTED]' in json.dumps(state['rows'])


@pytest.mark.parametrize('rows,identifier', [
    (None, 'valid'),
    ([dict(phase='request', content={})] * 129, 'valid'),
    ([dict(phase='request', content={})], 'not-a-uuid'),
    ([dict(phase='other', content={})], 'valid'),
    ([dict(phase='request', content=None)], 'valid'),
    ([dict(phase='request', content={}, arbitrary='private')], 'valid'),
])
def test_deferred_protocol_rejection_retains_no_partial_carrier(rows, identifier):
    from uuid import uuid4
    from api_agent.workflows.inference_capture import defer_inference_captures, _publish_admitted_rows
    with defer_inference_captures() as state:
        _publish_admitted_rows(rows, str(uuid4()) if identifier == 'valid' else identifier)
    assert state['lost'] is True
    assert state['rows'] == []


def test_capture_loss_without_workflow_owner_warns_without_failure(caplog):
    from api_agent.workflows.inference_capture import defer_inference_captures, publish_deferred_captures
    with defer_inference_captures() as state:
        state['lost'] = True
    publish_deferred_captures(state, [])
    assert 'physical owner unavailable' in caplog.text


def test_central_dedup_identity_retention_is_bounded():
    from uuid import uuid4
    from api_agent.workflows.inference_capture import defer_inference_captures, capture_rows
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    with bind_workflow_visit(journal, 'visit'), defer_inference_captures() as state:
        for _ in range(129):
            capture_rows([dict(phase='request', content={}, reason='withheld')], str(uuid4()))
    assert state['lost'] is True
    assert len(state['published']) == 128
    assert state['rows'] == []
    validate_journal(journal.finalize())


def test_direct_auth_capture_retains_scrubbed_provider_phase_with_valid_journal():
    import json
    from uuid import uuid4
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import (
        capture_inference, defer_inference_captures, publish_deferred_captures)
    identifier = str(uuid4())
    journal = WorkflowJournal(definition(), log_content=True,
        content_policy=ContentPolicy(True), boundary=EmissionBoundary(ContentPolicy(True), secrets=['private-marker']))
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    with bind_workflow_visit(journal, 'visit'):
        with defer_inference_captures() as state:
            capture_inference('input', {'message': 'Prompt private-marker'}, identifier, auth='private-marker')
            assert state['rows'] and 'private-marker' not in json.dumps(state['rows'])
        journal.record_call(visit_id='visit', call_id=identifier, selection='sample')
        publish_deferred_captures(state, [{'call_id': identifier}])
    payload = journal.finalize()
    validate_journal(payload)
    captures = [event for event in payload['events'] if event['event'] == 'capture']
    assert len(captures) == 1 and captures[0]['cause_id'] == identifier
    assert '[REDACTED]' in json.dumps(captures)
    assert 'private-marker' not in json.dumps(captures)
