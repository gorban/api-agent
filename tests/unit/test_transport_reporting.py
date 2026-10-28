"""Physical retries remain evidence within a single logical workflow call."""
from uuid import uuid4

import pytest

from api_agent.testing.workflow_steps import scope_steps
from api_agent.testing.workflow_accounting import apply_frozen_steps, include_unowned_calls
from api_agent.providers.generation_attempts import validate_transport_attempts


def ledger():
    def row(index, outcome, delay):
        return dict(id=str(uuid4()), index=index, execution='possibly_executed' if outcome == 'failed' else 'completed',
                    usage=None, billing=None, quote=None, retry_delay_seconds=delay,
                    started_ms=index, stopped_ms=index + 1, outcome=outcome,
                    status=503 if outcome == 'failed' else None, error_kind='server' if outcome == 'failed' else None, response=None)
    return dict(version=1, complete=True, attempts=[row(1, 'failed', 0.0), row(2, 'completed', None)])


def events(outcome='completed', attempts=None):
    graph, component, node, call = (str(uuid4()) for _ in range(4))
    def start(id, parent, kind, owner):
        return dict(event='scope_start', scope_id=id, parent_id=parent, kind=kind, owner=owner,
                    started_ms=10, attempt=1, max_passes=1)
    g, c, n = start(graph, None, 'graph', 'GRAPH'), start(component, graph, 'component', 'C'), start(node, component, 'node', 'N')
    call_row = dict(event='call', call_id=call, visit_id=node, outcome=outcome, started_ms=11, stopped_ms=12)
    if attempts is not None:
        call_row['transport_attempts'] = attempts
    def end(row):
        return dict(row, event='scope_end', stopped_ms=20, outcome='passed')
    return [g, c, n, call_row, end(n), end(c), end(g)]


def test_recovered_retry_bubbles_yellow_without_changing_semantic_attempts():
    rows = events(attempts=ledger())
    graph = scope_steps(rows)[0]
    component = graph['steps'][0]
    node = component['steps'][0]
    call = node['steps'][0]
    assert [graph['status'], component['status'], node['status'], call['status']] == ['broken'] * 4
    assert call['steps'][0]['status'] == 'broken'
    assert call['steps'][1]['status'] == 'passed'
    assert {'name': 'outcome', 'value': 'completed'} in call['parameters']
    control = scope_steps([dict(row, **({'transport_attempts': None} if row.get('event') == 'call' else {})) for row in rows])[0]
    assert control['status'] == 'passed'
    assert component['parameters'] == control['steps'][0]['parameters']


def test_terminal_error_stays_red_and_frozen_join_keeps_physical_children():
    snapshot = ledger()
    snapshot['attempts'][-1].update(outcome='failed', execution='possibly_executed', status=503, error_kind='server')
    graph = scope_steps(events('failed', snapshot))[0]
    leaf = graph['steps'][0]['steps'][0]['steps'][0]
    assert graph['status'] == leaf['status'] == 'failed'
    apply_frozen_steps([graph], 'case', {})
    assert len(leaf['steps']) == 2 and leaf['steps'][-1]['status'] == 'failed'


def test_unowned_retry_projects_attention_and_incomplete_evidence():
    snapshot = ledger()
    snapshot['complete'] = False
    call = dict(call_id=str(uuid4()), outcome='completed', transport_attempts=snapshot)
    steps = include_unowned_calls([], [call], [call['call_id']])
    assert steps[0]['status'] == 'broken'
    assert steps[0]['steps'][0]['steps'][-1]['name'] == 'transport evidence incomplete'


def test_invalid_or_content_bearing_attempts_are_rejected():
    snapshot = ledger()
    snapshot['attempts'][0]['prompt'] = 'secret'
    with pytest.raises(ValueError):
        validate_transport_attempts(snapshot)

@pytest.mark.parametrize('content', [False, True])
def test_trace_keeps_retry_structure_and_truthful_physical_status_independent_of_content(content):
    from unittest.mock import Mock
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.telemetry.tracing import _structural
    from api_agent.workflows.observations import WorkflowObservations

    snapshot = ledger()
    boundary = EmissionBoundary(ContentPolicy(content))
    spans = []
    class Trace:
        def begin(self, name, structural, **kwargs):
            span = Mock()
            spans.append((name, _structural(boundary, structural), kwargs, span))
            return span
    observer = object.__new__(WorkflowObservations)
    observer.trace = Trace()
    observer._spans = {}
    observer._visit_call_kinds = {}
    observer._trace_row(dict(event='call', call_id=str(uuid4()), event_id=str(uuid4()),
                             visit_id=str(uuid4()), outcome='completed', transport_attempts=snapshot))
    logical, first, second = spans
    assert logical[1]['api_agent.transport_recovered'] is True
    assert logical[1]['api_agent.transport_attempt_count'] == 2
    assert first[1]['api_agent.transport_status'] == 503
    assert first[1]['api_agent.transport_id'] == snapshot['attempts'][0]['id']
    assert first[2]['parent'] is logical[3] and second[2]['parent'] is logical[3]
    first[3].attributes.assert_called_once_with({'status': 'error'})
    logical[3].attributes.assert_called_once_with({'duration_seconds': None, 'status': 'ok'})


def test_physical_known_subtotals_do_not_recount_terminal_usage():
    from api_agent.testing.workflow_accounting import _call_facts
    snapshot = ledger()
    facts = _call_facts(dict(transport_attempts=snapshot, usage={'input_tokens': 100},
                             quote={'list_cost_usd': 9}))
    assert facts['input_tokens'] is None
    assert facts['transport_known_input_tokens'] == 0
    assert facts['transport_unknown_input_tokens_attempts'] == 2
    assert facts['list_cost_usd'] is None


def test_journal_retains_validated_content_free_ledger():
    from types import SimpleNamespace
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    step = SimpleNamespace(id='N', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[step], tables={'Edges': []}), resources=[], replacements=[])
    journal = WorkflowJournal(definition)
    journal.observe(dict(event='scope_start', scope_id='graph', parent_id=None, kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='N', implicit=True))
    journal.observe(dict(event='scope_start', scope_id='visit', parent_id='graph', kind='node', owner='N', started_ms=1))
    snapshot = ledger()
    journal.record_call(visit_id='visit', call_id=str(uuid4()), transport_attempts=snapshot)
    journal.observe(dict(event='node', scope_id='visit', node='N'))
    journal.observe(dict(event='scope_end', scope_id='visit', parent_id='graph', kind='node', owner='N', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='N', target='END', implicit=True, visit_id='visit'))
    journal.observe(dict(event='scope_end', scope_id='graph', parent_id=None, kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    result = journal.finalize()
    validate_journal(result)
    assert next(row for row in result['events'] if row['event'] == 'call')['transport_attempts'] == snapshot

    from hashlib import sha256
    from api_agent.workflows.journal import _encoded
    next(row for row in result['events'] if row['event'] == 'node')['transport_attempts'] = snapshot
    result['sha256'] = sha256(_encoded({key: value for key, value in result.items() if key != 'sha256'})).hexdigest()
    with pytest.raises(ValueError, match='Transport attempts require a call'):
        validate_journal(result)


def test_cancellation_remains_broken_with_recovered_retry():
    snapshot = ledger()
    snapshot['attempts'][-1].update(outcome='failed', execution='possibly_executed', error_kind='cancelled')
    graph = scope_steps(events('cancelled', snapshot))[0]
    leaf = graph['steps'][0]['steps'][0]['steps'][0]
    assert graph['status'] == leaf['status'] == 'broken'
    assert {'name': 'outcome', 'value': 'cancelled'} in leaf['parameters']


def test_untimed_publisher_calls_keep_distinct_owners_through_frozen_join(tmp_path, monkeypatch):
    import json
    from copy import deepcopy
    from types import SimpleNamespace
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import attach_transport_attempts
    from api_agent.testing.inference_usage import publish
    from api_agent.testing.workflow_accounting import join_workflow_accounting
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.journal import WorkflowJournal
    from api_agent.workflows.run_context import turn
    from test_inference_shared_join import REQUEST, RESULT

    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_example.py::test_call (call)')
    step = SimpleNamespace(id='N', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[step], tables={'Edges': []}), resources=[], replacements=[])
    journal = WorkflowJournal(definition)
    graph_id, visit = str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=graph_id, parent_id=None, kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='N', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=visit, parent_id=graph_id, kind='node', owner='N', started_ms=1))
    recovered = ledger()
    for attempt in recovered['attempts']:
        attempt['quote'] = {'list_cost_usd': 0.1, 'payment': None}
    failure = ledger()
    failure['attempts'][-1].update(outcome='failed', execution='possibly_executed', status=503, error_kind='server')
    error = attach_transport_attempts(ProviderAccessError('unavailable', 'unavailable', status=503), failure)
    with bind_workflow_visit(journal, visit), turn(journal.turn_id):
        first = publish(REQUEST, attach_transport_attempts(deepcopy(RESULT), recovered))
        second = publish(REQUEST, failed=True, error=error)
    journal.observe(dict(event='node', scope_id=visit, node='N'))
    journal.observe(dict(event='scope_end', scope_id=visit, parent_id=graph_id, kind='node', owner='N', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='N', target='END', implicit=True, visit_id=visit))
    journal.observe(dict(event='scope_end', scope_id=graph_id, parent_id=None, kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    captured = journal.finalize()
    call_events = [row for row in captured['events'] if row['event'] == 'call']
    assert len(call_events) == 2 and all(row['started_ms'] is None for row in call_events)
    calls = [json.loads(line)['data'] for line in path.read_text().splitlines()]
    accounting = {'tests': [{'test_id': 'case', 'calls': calls, 'calls_complete': True}]}
    _, facts = join_workflow_accounting({'case': [captured]}, accounting,
        admissions={captured['projection']['definition_id']: captured['projection']})
    graph = scope_steps(captured['events'])[0]
    apply_frozen_steps([graph], 'case', facts)
    leaves = {leaf['name']: leaf for leaf in graph['steps'][0]['steps']}
    assert set(leaves) == {'call ' + first, 'call ' + second}
    recovered_leaf, failed_leaf = leaves['call ' + first], leaves['call ' + second]
    assert recovered_leaf['status'] == 'broken' and failed_leaf['status'] == 'failed'
    assert [step['status'] for step in recovered_leaf['steps']] == ['broken', 'passed']
    assert [step['status'] for step in failed_leaf['steps']] == ['broken', 'failed']
    assert {p['name']: p['value'] for p in recovered_leaf['parameters']}['list_cost_usd'] == '0.2'
    assert {p['name']: p['value'] for p in failed_leaf['parameters']}['list_cost_usd'] == 'unknown'
    assert all('start' not in leaf and 'stop' not in leaf for leaf in leaves.values())


def test_trace_rejects_out_of_range_transport_fields():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.telemetry.tracing import _structural

    boundary = EmissionBoundary(ContentPolicy(True))
    fields = {'api_agent.transport_index': 21, 'api_agent.transport_status': 99,
              'api_agent.transport_complete': 'true'}
    assert _structural(boundary, fields) == {}


def test_open_physical_attempt_has_no_fabricated_status_or_end_time():
    from unittest.mock import Mock
    from api_agent.workflows.observations import WorkflowObservations

    snapshot = ledger()
    snapshot['complete'] = False
    snapshot['attempts'][-1].update(outcome='started', execution='possibly_executed', stopped_ms=None)
    spans = []
    class Trace:
        def begin(self, name, structural, **kwargs):
            span = Mock()
            spans.append(span)
            return span
    observer = object.__new__(WorkflowObservations)
    observer.trace, observer._spans, observer._visit_call_kinds = Trace(), {}, {}
    observer._trace_row(dict(event='call', call_id=str(uuid4()), event_id=str(uuid4()),
                             visit_id=str(uuid4()), outcome='partial', transport_attempts=snapshot))
    spans[-1].attributes.assert_not_called()
    spans[-1].end.assert_called_once_with(end_time=None)


def test_complete_physical_billing_is_reported_as_authoritative_charge():
    from api_agent.testing.workflow_accounting import _call_facts

    snapshot = ledger()
    for row in snapshot['attempts']:
        row['usage'] = dict(input_tokens=10, output_tokens=2, cached_input_tokens=0,
                            cache_write_tokens=0, reasoning_tokens=0)
        row['billing'] = dict(usd=0.1, request_id='request', generation_id='generation')
    facts = _call_facts({'transport_attempts': snapshot})
    assert facts['reported_charge_usd'] == pytest.approx(0.2)
    assert facts['reported_charge_basis'] == 'qualified_provider_billing'
    assert facts['transport_unknown_charge_attempts'] == 0
