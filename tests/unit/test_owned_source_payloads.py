"""Only an explicit owned source ingress can authorize recorded model wire."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from api_agent.providers.control import OperationalError
from api_agent.testing.inference_usage import delegated_test_node
from api_agent.testing.local_accounting import owned_source_payloads
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.inference_capture import observe_source_captures
from api_agent.workflows.observations import WorkflowObservationFactory
from test_local_llm import request, selected
from test_workflow_observations import definition, config


def observations(owner):
    journal = WorkflowObservationFactory(config(log=True), boundary=owner.emissions).create(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    return journal


@pytest.mark.parametrize('failed', [False, True])
def test_source_observer_failure_preserves_model_outcome_and_marks_loss(selected, monkeypatch, failed):
    from api_agent.workflows import inference_capture
    owner, implementation, _, _, _ = selected
    source = OperationalError('unknown')
    if failed:
        async def fail(value, *, auth):
            raise source
        implementation.acomplete = fail
    journal = observations(owner)
    losses = []
    monkeypatch.setattr(inference_capture, '_report_capture_loss', lambda *args: losses.append(args))
    def broken(rows, call_id):
        raise RuntimeError('observer unavailable')
    async def run():
        try:
            with observe_source_captures(broken), bind_workflow_visit(journal, 'visit'):
                if failed:
                    with pytest.raises(OperationalError) as caught:
                        await owner.complete(request())
                    assert caught.value is source
                else:
                    assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert losses and losses[0][0] == owner.usage[0]['call_id']


def test_source_observer_gets_independent_rows_without_mutating_journal(selected):
    owner, _, _, _, _ = selected
    journal = observations(owner)
    snapshots = []
    def observe(rows, call_id):
        snapshots.extend(deepcopy(rows))
        for row in rows:
            row['content'].clear()
    async def run():
        try:
            with observe_source_captures(observe), bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert {row['phase'] for row in snapshots} == {'request', 'response'}
    retained = [row for row in journal.finalize()['events'] if row['event'] == 'capture']
    assert all(row['values'] for row in retained)
    assert 'hello' in json.dumps(retained) and 'answer' in json.dumps(retained)


def test_generic_raw_capture_never_gains_source_wire_authority(selected, tmp_path):
    from api_agent.workflows.inference_capture import capture_rows
    from api_agent.testing.payloads import PayloadStore
    from test_payloads import recorder
    owner, _, _, _, _ = selected
    journal = observations(owner)
    seen = []
    with observe_source_captures(lambda *args: seen.append(args)), bind_workflow_visit(journal, 'visit'):
        capture_rows([dict(phase='request', content={'messages': []})], str(uuid4()))
    assert seen == []
    with pytest.raises(ValueError, match='no authoritative payload'):
        PayloadStore(recorder(tmp_path), 'test').materialize_journal(journal.finalize())


@pytest.mark.parametrize('mode', ['manual', 'disabled', 'textfalse', 'unowned'])
def test_source_payload_adapter_respects_test_and_scope_ownership(selected, tmp_path, monkeypatch, mode):
    from api_agent.workflows.run_context import test_evidence
    owner, _, _, _, _ = selected
    journal = observations(owner)
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    async def run():
        try:
            with test_evidence(mode != 'manual'), owned_source_payloads(enabled='false' if mode == 'textfalse' else mode != 'disabled'), \
                    bind_workflow_visit(None if mode == 'unowned' else journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert not path.exists()


@pytest.mark.parametrize('node', ['owned.py::test_graph', 'owned.py::test_graph[parameter with spaces]'])
def test_source_sidecar_keeps_delegated_owner_when_global_test_changes(selected, tmp_path, monkeypatch, node):
    from api_agent.testing.inference_content import initialize_evidence, read_evidence
    owner, implementation, _, _, _ = selected
    original = implementation.acomplete
    async def later(value, *, auth):
        monkeypatch.setenv('PYTEST_CURRENT_TEST', 'unrelated.py::test_later (call)')
        return await original(value, auth=auth)
    implementation.acomplete = later
    journal = observations(owner)
    sidecar = str(tmp_path / 'wire.sqlite')
    initialize_evidence(sidecar, [node])
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', str(sidecar))
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    async def run():
        try:
            with delegated_test_node(node), owned_source_payloads(enabled=True), bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    rows, after = [], 0
    while batch := read_evidence(sidecar, after):
        rows.extend(batch)
        after = batch[-1][0]
    assert len(rows) == 2 and {row[1] for row in rows} == {node}
    assert {row[2]['phase'] for row in rows} == {'request', 'response'}
    assert {row[2]['call_id'] for row in rows} == {owner.usage[0]['call_id']}
    assert all(row[2]['capture_source'] == 'fallback' and row[2]['transport_attempt_id'] for row in rows)


def test_late_live_credential_registration_scrubs_source_wire_before_publication(selected, tmp_path, monkeypatch):
    from api_agent.testing.payloads import PayloadStore
    from test_payloads import recorder
    owner, implementation, _, _, _ = selected
    original = implementation.acomplete
    async def register_late(value, *, auth):
        result = await original(value, auth=auth)
        await implementation.authority.aregister_secret('hello')
        return result
    implementation.acomplete = register_late
    journal = observations(owner)
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    async def run():
        try:
            with owned_source_payloads(enabled=True), bind_workflow_visit(journal, 'visit'):
                assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(run())
    rows = [json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert 'hello' not in json.dumps(rows)
    packet = journal.finalize()
    assert 'hello' not in json.dumps(packet)
    store = PayloadStore(recorder(tmp_path / 'recorded'), 'test')
    for row in rows:
        store.put_wire(row['call_id'], row['phase'], row['content'], reason=row['reason'],
            transport_attempt_id=row.get('transport_attempt_id'), capture_source=row.get('capture_source'))
    durable = store.materialize_journal(packet)
    assert 'hello' not in json.dumps(durable)


def test_source_observer_returning_coroutine_is_closed_and_reports_loss(selected, monkeypatch):
    import inspect
    from api_agent.workflows import inference_capture
    owner, _, _, _, _ = selected
    journal = observations(owner)
    returned, losses = [], []
    async def invalid():
        pytest.fail('Observer coroutine must never run')
    def observer(rows, call_id):
        result = invalid()
        returned.append(result)
        return result
    monkeypatch.setattr(inference_capture, '_report_capture_loss', lambda *args: losses.append(args))
    async def run():
        try:
            with observe_source_captures(observer), bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert len(returned) == 1 and inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
    assert {loss[0] for loss in losses} == {owner.usage[0]['call_id']}
    assert {loss[1] for loss in losses} == {'input', 'output'}


@pytest.mark.parametrize('failed', [False, True])
def test_optional_fresh_source_projection_failure_preserves_provider_outcome(selected, monkeypatch, failed):
    from api_agent.workflows import capture, inference_capture
    owner, implementation, _, _, _ = selected
    journal = observations(owner)
    original, project = implementation.acomplete, capture.project_values
    source, fail_projection, losses = OperationalError('unknown'), False, []
    def project_later(*args, **kwargs):
        if fail_projection:
            raise RuntimeError('optional source projection unavailable')
        return project(*args, **kwargs)
    monkeypatch.setattr(capture, 'project_values', project_later)
    monkeypatch.setattr(inference_capture, '_report_capture_loss', lambda *args: losses.append(args))
    async def later(value, *, auth):
        nonlocal fail_projection
        result = await original(value, auth=auth)
        fail_projection = True
        if failed:
            raise source
        return result
    implementation.acomplete = later
    async def run():
        try:
            with observe_source_captures(lambda *args: None), bind_workflow_visit(journal, 'visit'):
                if failed:
                    with pytest.raises(OperationalError) as caught:
                        await owner.complete(request())
                    assert caught.value is source
                else:
                    assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert losses
    assert journal.finalize()['completeness'] == 'partial'


def test_stopped_sidecar_keeps_bounded_omissions_for_later_phase_and_call(tmp_path, monkeypatch):
    from api_agent.testing import inference_content as wire
    from api_agent.testing.payloads import MAX_ENTRIES
    import sqlite3
    path, node = str(tmp_path / 'wire.sqlite'), 'owned.py::test_turn'
    wire.initialize_evidence(path, [node])
    monkeypatch.setattr(wire, 'MAX_TEST_BYTES', 0)
    wire._publish_sidecar(path, 'unselected', dict(phase='request', content={}), str(uuid4()))
    assert wire.read_evidence(path, 0) == []
    first, second, attempt = str(uuid4()), str(uuid4()), str(uuid4())
    for call_id, phase in [(first, 'request'), (first, 'response'), (second, 'request')]:
        wire._publish_sidecar(path, node, dict(phase=phase, content={'text': 'private-body-sentinel'},
            transport_attempt_id=attempt, capture_source='fallback'), call_id)
    rows, after = [], 0
    while batch := wire.read_evidence(path, after):
        rows.extend(batch)
        after = batch[-1][0]
    assert [(row[2]['call_id'], row[2]['phase']) for row in rows] == [
        (first, 'request'), (first, 'response'), (second, 'request')]
    assert all(row[2]['content'] == {} and row[2]['reason'] == 'truncated' for row in rows)
    assert 'private-body-sentinel' not in json.dumps(rows)
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute('SELECT bytes,stopped FROM budget WHERE node=?', (node,)).fetchone() == (0, 1)
        db.execute('UPDATE budget SET rows=? WHERE node=?', (MAX_ENTRIES, node))
    with pytest.raises(ValueError, match='occurrence limit'):
        wire._publish_sidecar(path, node, dict(phase='response', content={}), second)


@pytest.mark.parametrize('field,value', [('transport_attempt_id', 'invalid'), ('transport_attempt_id', None), ('capture_source', 'arbitrary')])
def test_stopped_sidecar_rejects_invalid_omission_metadata(tmp_path, monkeypatch, field, value):
    from api_agent.testing import inference_content as wire
    path, node = str(tmp_path / 'wire.sqlite'), 'owned.py::test_turn'
    wire.initialize_evidence(path, [node])
    monkeypatch.setattr(wire, 'MAX_TEST_BYTES', 0)
    row = dict(phase='request', content={'text': 'body'}, **{field: value})
    with pytest.raises(ValueError):
        wire._publish_sidecar(path, node, row, str(uuid4()))
    assert wire.read_evidence(path, 0) == []


@pytest.mark.parametrize('acknowledged', [True, False])
def test_expired_source_owner_cannot_publish_retained_capture(selected, monkeypatch, acknowledged):
    from api_agent.workflows import inference_capture as capture
    owner, _, _, _, _ = selected
    journal = observations(owner)
    call_id, seen, losses = str(uuid4()), [], []
    def report(*args):
        losses.append(args)
        if not acknowledged:
            raise RuntimeError('loss channel unavailable')
    monkeypatch.setattr(capture, '_report_capture_loss', report)
    with bind_workflow_visit(journal, 'visit'), capture.defer_inference_captures() as pending:
        with capture.bind_inference_call(call_id):
            capture.capture_inference('input', {'text': 'source-admitted'}, call_id, auth='synthetic-credential')
    assert pending['rows']
    capture.publish_deferred_captures(pending, [dict(call_id=call_id)], observer=lambda *args: seen.append(args))
    assert seen == [] and losses


@pytest.mark.parametrize('missing', ['owner', 'node'])
def test_explicit_payload_sink_requires_captured_test_ownership(selected, tmp_path, monkeypatch, missing):
    from api_agent.workflows.inference_capture import current_source_capture_observer
    owner, _, _, _, _ = selected
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    if missing == 'node':
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
    with owned_source_payloads(enabled=True), bind_workflow_visit(
            None if missing == 'owner' else observations(owner), 'visit'):
        current_source_capture_observer()([], str(uuid4()))
    assert not path.exists()


@pytest.mark.parametrize('allowed', [False, True])
def test_source_loss_uses_exact_delegated_owner_after_global_test_changes(selected, monkeypatch, allowed):
    from api_agent.testing import inference_content
    from api_agent.workflows.run_context import test_evidence
    owner, implementation, _, _, _ = selected
    original = implementation.acomplete
    async def later(value, *, auth):
        monkeypatch.setenv('PYTEST_CURRENT_TEST', 'unrelated.py::test_later (call)')
        return await original(value, auth=auth)
    implementation.acomplete = later
    journal = WorkflowObservationFactory(config(log=True), boundary=owner.emissions).create(definition())
    visit_id, notices = str(uuid4()), []
    journal.observe(dict(event='scope_start', scope_id=visit_id, kind='node', owner='Make'))
    monkeypatch.setattr(inference_content, 'notify_failure', lambda node, scope: notices.append((node, scope)))
    def broken(*args):
        raise RuntimeError('optional observer unavailable')
    node = 'owned.py::test_graph[parameter with spaces]'
    async def run():
        try:
            with delegated_test_node(node), test_evidence(allowed), observe_source_captures(broken), \
                    bind_workflow_visit(journal, visit_id):
                assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(run())
    if allowed:
        assert notices == [(node, dict(invocation_id=journal.invocation_id, visit_id=visit_id,
            call_id=owner.usage[0]['call_id'], direction=direction)) for direction in ('input', 'output')]
    else:
        assert notices == []
