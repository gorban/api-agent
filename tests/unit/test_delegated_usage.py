"""Delegated provider receipts use the ordinary content-free accounting path."""
import json
import os
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest

from api_agent.telemetry.usage import SuiteUsage


def test_delegated_receipts_retain_owner_and_aggregate_once(tmp_path, monkeypatch):
    from api_agent.testing.inference_usage import publish_delegated_usage, capture
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    monkeypatch.setenv('LOG_CONTENT', 'false')
    monkeypatch.setenv('TRACELOOP_TRACE_CONTENT', 'false')
    turn = str(uuid4())
    invocation = str(uuid4())
    journal = Mock(turn_id=turn, invocation_id=invocation, execution_context=None)
    owner = journal, 'exact-visit', 'exact-attempt'
    for index in range(3):
        publish_delegated_usage(call_id=str(uuid4()), selection='selected-model', actual_model='actual-model',
            usage=None if index == 2 else dict(input_tokens=10, output_tokens=2,
                cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=None),
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00',
            outcome='failed' if index == 2 else 'completed', owner=owner, turn_id=turn)
    tracker = SuiteUsage('run')
    tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn)
    call_ids = [row['data']['call_id'] for row in map(json.loads, path.read_text().splitlines())]
    tracker.record_external_invocation(session_id='test', invocation_id=invocation,
        turn_id=turn, call_ids=call_ids, complete=True)
    for row in map(json.loads, path.read_text().splitlines()):
        event = SimpleNamespace(text=json.dumps(row['data']), identity=SimpleNamespace(id='test'))
        capture(event, tracker)
        capture(event, tracker)
    report = tracker.report()
    assert report['prompt_count'] == 1
    assert report['unmeasured_calls'] == {'test': 1}
    assert report['usage_rows'][0]['input_tokens'] == 20
    assert report['usage_rows'][0]['cache_read_tokens'] == 0
    assert report['usage_rows'][0]['reasoning_tokens'] is None
    assert len(tracker.inference_calls) == 3
    assert journal.record_call.call_count == 3
    for call in journal.record_call.call_args_list:
        assert call.kwargs['visit_id'] == 'exact-visit'
        assert call.kwargs['attempt_id'] == 'exact-attempt'
        assert call.kwargs['elapsed'] == 1
    assert 'prompt' not in path.read_text() and 'response' not in path.read_text()


def test_inventory_closure_preserves_missing_and_incomplete_calls():
    tracker = SuiteUsage('run')
    invocation, turn, known, missing = (str(uuid4()) for _ in range(4))
    tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn)
    tracker.record_inference(known, 'test', {'call_id': known, 'turn_id': turn, 'invocation_id': invocation, 'usage': {'input_tokens': 1}})
    assert tracker.report()['unmeasured_calls'] == {'test': 1}
    tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn,
        call_ids=[known, missing], complete=True)
    assert tracker.report()['unmeasured_calls'] == {'test': 1}
    tracker.record_inference(missing, 'test', {'call_id': missing, 'turn_id': turn, 'invocation_id': invocation, 'usage': {'input_tokens': 2}})
    assert tracker.report()['unmeasured_calls'] == {}
    assert tracker.report()['native_invocation_count'] == 1


def test_delegated_transport_ledger_replaces_parent_totals(tmp_path, monkeypatch):
    from api_agent.testing.inference_usage import publish_delegated_usage, capture
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    usage = dict(input_tokens=4, output_tokens=2, cached_input_tokens=0, reasoning_tokens=0)
    attempts = [dict(id=str(uuid4()), index=index, execution='completed', usage=usage,
        billing=None, quote=None, retry_delay_seconds=0 if index == 1 else None,
        started_ms=index * 10, stopped_ms=index * 10 + 1,
        outcome='failed' if index == 1 else 'completed', status=500 if index == 1 else None,
        error_kind='server' if index == 1 else None, response=None) for index in (1, 2)]
    publish_delegated_usage(call_id=str(uuid4()), selection='model', usage=usage,
        started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00',
        outcome='completed', owner=None, transport_attempts={'version': 1, 'complete': True, 'attempts': attempts})
    event = SimpleNamespace(text=json.dumps(json.loads(path.read_text())['data']), identity=SimpleNamespace(id='test'))
    tracker = SuiteUsage('run')
    capture(event, tracker)
    capture(event, tracker)
    assert tracker.report()['usage_rows'][0]['input_tokens'] == 8
    assert tracker.report()['prompt_count'] == 1


def test_published_call_joins_exact_exported_workflow_leaf(tmp_path, monkeypatch):
    from api_agent.testing.inference_usage import publish_delegated_usage, capture
    from api_agent.testing.workflow_accounting import join_workflow_accounting, apply_frozen_steps
    from test_workflow_accounting import recorded_call
    from api_agent.workflows.journal import WorkflowJournal
    identifier = str(uuid4())
    original = WorkflowJournal.record_call

    def delegated(journal, **kwargs):
        publish_delegated_usage(call_id=kwargs['call_id'], selection='selected', actual_model='actual',
            usage=dict(input_tokens=7, output_tokens=3, cached_input_tokens=0,
                cache_write_tokens=0, reasoning_tokens=None), owner=None, turn_id=journal.turn_id,
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00', outcome='completed')
        return original(journal, **kwargs)

    monkeypatch.setattr(WorkflowJournal, 'record_call', delegated)
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    journal = recorded_call(identifier)
    tracker = SuiteUsage('run')
    capture(SimpleNamespace(text=json.dumps(json.loads(path.read_text())['data']), identity=SimpleNamespace(id='test')), tracker)
    joined, facts = join_workflow_accounting({'test': [journal]}, {'tests': [dict(test_id='test',
        calls=list(tracker.inference_calls.values()), calls_complete=True)]},
        admissions={journal['projection']['definition_id']: journal['projection']})
    assert joined['tests'][0]['attribution'] == 'complete'
    steps = apply_frozen_steps([dict(name='graph GRAPH', parameters=[dict(name='scope_id', value='graph'),
        dict(name='invocation_id', value=journal['invocation_id'])], steps=[dict(name='node Make',
        parameters=[dict(name='scope_id', value='visit')], steps=[dict(name='call '+identifier, parameters=[], steps=[])])])], 'test', facts)
    leaf = steps[0]['steps'][0]['steps'][0]
    parameters = {row['name']: row['value'] for row in leaf['parameters']}
    assert parameters['input_tokens'] == '7'
    assert parameters['cached_input_tokens'] == '0'
    assert parameters['actual_model'] == 'actual'


def test_delegated_receipt_rejects_payload_extensions_before_publication(tmp_path, monkeypatch):
    import pytest
    from api_agent.testing.inference_usage import publish_delegated_usage
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    with pytest.raises(ValueError):
        publish_delegated_usage(call_id=str(uuid4()), selection='selected',
            usage=dict(input_tokens=1, output_tokens=2, cached_input_tokens=0,
                reasoning_tokens=0, hidden_reasoning='private'),
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00', outcome='completed')
    assert not path.exists()


def test_native_inventory_publication_round_trip(tmp_path, monkeypatch):
    from api_agent.testing.inference_usage import publish_external_invocation, capture_external_invocation
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    invocation, turn, call = (str(uuid4()) for _ in range(3))
    assert publish_external_invocation(invocation, turn_id=turn, call_ids=[call], complete=True)
    tracker = SuiteUsage('run')
    capture_external_invocation(SimpleNamespace(text=json.dumps(json.loads(path.read_text())['data']),
        identity=SimpleNamespace(id='test')), tracker)
    assert tracker.report()['unmeasured_calls'] == {'test': 1}
    tracker.record_inference(call, 'test', {'call_id': call, 'turn_id': turn, 'invocation_id': invocation, 'usage': {'input_tokens': 1}})
    assert tracker.report()['unmeasured_calls'] == {}


def test_owner_snapshot_survives_completion_context_switch(tmp_path, monkeypatch):
    from api_agent.testing.inference_usage import capture_delegated_owner, publish_delegated_usage, user_turn
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.run_context import turn, conversation
    first, second, conv = (str(uuid4()) for _ in range(3))
    journal = Mock(turn_id=first, invocation_id=str(uuid4()), execution_context={'kind': 'conversation', 'conversation_id': conv})
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_usage (call)')
    with turn(first), conversation(conv), user_turn(first, 'clarification'), bind_workflow_visit(journal, 'visit', None):
        owner = capture_delegated_owner()
    with turn(second), user_turn(second):
        publish_delegated_usage(call_id=str(uuid4()), selection='model', usage=None, owner=owner,
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00', outcome='failed')
    row = json.loads(path.read_text())['data']
    assert row['turn_id'] == first
    assert row['turn_kind'] == 'clarification'
    assert row['conversation_id'] == conv
    assert row['invocation_id'] == journal.invocation_id


def test_invocation_inventories_reject_shared_call_claims():
    import pytest
    tracker = SuiteUsage('run')
    first, second, turn, call = (str(uuid4()) for _ in range(4))
    tracker.record_external_invocation(session_id='test', invocation_id=first, turn_id=turn, call_ids=[call], complete=True)
    with pytest.raises(ValueError, match='multiple invocations'):
        tracker.record_external_invocation(session_id='test', invocation_id=second, turn_id=turn, call_ids=[call], complete=True)


def test_empty_complete_inventory_detects_unexpected_owned_receipt():
    tracker = SuiteUsage('run')
    invocation, turn, call = (str(uuid4()) for _ in range(3))
    tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn, call_ids=[], complete=True)
    tracker.record_inference(call, 'test', dict(call_id=call, turn_id=turn, invocation_id=invocation, usage={'input_tokens': 1}))
    assert tracker.report()['unmeasured_native_invocations'] == {'test': 1}
    assert tracker.report()['measured_calls'] == {'test': 1}
    assert tracker.report()['unmeasured_model_calls'] == {}


def test_accounting_unknown_usage_receipt_is_one_observed_call():
    from api_agent.telemetry.accounting import build_accounting
    call, turn = str(uuid4()), str(uuid4())
    tracker = SuiteUsage('run')
    tracker.record_inference(call, 'test', dict(call_id=call, turn_id=turn, usage=None, selection='model', actual_model=None,
        quote=None, service_tier=None, serving_revision=None, started_at=None, finished_at=None,
        attempt=1, turn_kind='initial', outcome='failed'))
    test = SimpleNamespace(id='test', node_id='test.py::test_usage', root=None)
    accounting = build_accounting(SimpleNamespace(run_id=str(uuid4())), [test], {}, tracker, [])
    assert accounting['tests'][0]['observed_call_count'] == 1
    assert accounting['tests'][0]['calls_complete'] is False


@pytest.mark.parametrize('kind', ['clarification', 'approval'])
@pytest.mark.parametrize('native_first', [False, True])
def test_native_boundary_defers_prompt_kind_to_receipt(kind, native_first):
    from api_agent.testing.inference_usage import capture
    tracker = SuiteUsage('run')
    invocation, turn, call = (str(uuid4()) for _ in range(3))
    event = SimpleNamespace(identity=SimpleNamespace(id='test'), text=json.dumps(dict(
        call_id=call, invocation_id=invocation, turn_id=turn, turn_kind=kind,
        selection='model', usage=None, outcome='failed')))
    def native():
        tracker.record_external_invocation(session_id='test', invocation_id=invocation,
            turn_id=turn, call_ids=[call], complete=True)
    if native_first:
        native()
    capture(event, tracker)
    if not native_first:
        native()
    assert tracker.report()['prompt_count'] == 1
    assert tracker.report()['prompt_counts'][kind] == 1
    assert tracker.report()['prompt_counts']['initial'] == 0


def test_delegated_node_binding_is_task_local_and_snapshot_owned(tmp_path, monkeypatch):
    import asyncio
    from api_agent.testing.inference_usage import delegated_test_node, capture_delegated_owner, publish_delegated_usage, publish_external_invocation
    from api_agent.workflows.attribution import bind_workflow_visit
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'child.py::test_child (call)')
    journal = Mock(turn_id=str(uuid4()), invocation_id=str(uuid4()), execution_context=None)
    with delegated_test_node('parent.py::test_parent'), bind_workflow_visit(journal, 'visit', None):
        owner = capture_delegated_owner()
    with delegated_test_node('different.py::test_other'):
        publish_delegated_usage(call_id=str(uuid4()), selection='model', usage=None, owner=owner,
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00', outcome='failed')
    async def task(node):
        with delegated_test_node(node):
            await asyncio.sleep(0)
            publish_external_invocation(str(uuid4()))
    async def concurrent():
        await asyncio.gather(task('first.py::test_first'), task('second.py::test_second'))
    asyncio.run(concurrent())
    publish_external_invocation(str(uuid4()))
    nodes = [json.loads(line)['node'] for line in path.read_text().splitlines()]
    assert nodes == ['parent.py::test_parent', 'first.py::test_first', 'second.py::test_second', 'child.py::test_child']
    assert os.environ['PYTEST_CURRENT_TEST'] == 'child.py::test_child (call)'


@pytest.mark.parametrize('node', ['', 'bad\nnode', 1])
def test_delegated_node_rejects_invalid_identity(node):
    from api_agent.testing.inference_usage import delegated_test_node
    with pytest.raises(ValueError, match='Invalid delegated test node'):
        with delegated_test_node(node):
            pytest.fail('Invalid node was admitted')


def test_logical_confirmation_projection_retains_all_usage_count_scopes():
    from api_agent.testing.confirmations import logical_usage
    names = ('unmeasured_calls', 'measured_calls', 'unmeasured_model_calls',
             'unmeasured_native_invocations', 'unobserved_calls')
    physical = {name: {'first': 2, 'second': 3} for name in names}
    result = logical_usage(physical, {'first': 'test', 'second': 'test'})
    assert all(result[name] == {'test': 5} for name in names)
    assert all(physical[name] == {'first': 2, 'second': 3} for name in names)
