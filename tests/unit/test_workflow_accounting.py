from api_agent.testing.workflow_accounting import join_workflow_accounting, apply_frozen_steps
from api_agent.workflows.journal import WorkflowJournal
from types import SimpleNamespace
import pytest


def recorded_call(call_id):
    step = SimpleNamespace(id='Make', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64,
        entry_root='base', layout=SimpleNamespace(steps=[step], tables={'Edges': []}),
        resources=[], replacements=[])
    journal = WorkflowJournal(definition)
    journal.observe(dict(event='scope_start', scope_id='graph', parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    journal.observe(dict(event='scope_start', scope_id='visit', parent_id='graph',
                         kind='node', owner='Make', started_ms=1))
    journal.record_call(call_id=call_id, visit_id='visit')
    journal.observe(dict(event='node', scope_id='visit', node='Make'))
    journal.observe(dict(event='scope_end', scope_id='visit', parent_id='graph',
                         kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Make', target='END',
                         implicit=True, visit_id='visit'))
    journal.observe(dict(event='scope_end', scope_id='graph', parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    return journal.finalize()


def test_frozen_call_join_and_inclusive_cost_count_once():
    journal = recorded_call('00000000-0000-4000-8000-000000000001')
    call = {'call_id': '00000000-0000-4000-8000-000000000001', 'selection': 'model', 'actual_model': 'model-v1',
            'turn_id': journal['turn_id'],
            'outcome': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2,
                                            'cached_input_tokens': 1, 'reasoning_tokens': 0},
            'quote': {'list_cost_usd': 0.01}}
    accounting = {'tests': [{'test_id': 'test', 'calls': [call], 'calls_complete': True}]}
    joined, facts = join_workflow_accounting({'test': [journal]}, accounting,
        admissions={journal['projection']['definition_id']: journal['projection']})
    assert joined['tests'][0]['attribution'] == 'complete'
    graph = {'name': 'graph GRAPH', 'parameters': [{'name': 'scope_id', 'value': 'graph'}],
             'steps': [{'name': 'node Make', 'parameters': [{'name': 'scope_id', 'value': 'visit'}],
                        'steps': [{'name': 'call 00000000-0000-4000-8000-000000000001', 'parameters': [{'name': 'input_tokens', 'value': '999'}],
                                   'steps': []}]}]}
    steps = apply_frozen_steps([graph], 'test', facts)
    graph_params = {row['name']: row['value'] for row in steps[0]['parameters']}
    node_params = {row['name']: row['value'] for row in steps[0]['steps'][0]['parameters']}
    leaf_params = {row['name']: row['value'] for row in steps[0]['steps'][0]['steps'][0]['parameters']}
    assert graph_params['inclusive_list_cost_usd'] == node_params['inclusive_list_cost_usd'] == '0.01'
    assert leaf_params['input_tokens'] == '3'
    assert leaf_params['cost_basis'] == 'provider_quote'


def test_unowned_or_unverified_calls_are_partial():
    journal = recorded_call('00000000-0000-4000-8000-000000000002')
    accounting = {'tests': [{'test_id': 'test', 'calls': [{'call_id': 'other', 'turn_id': journal['turn_id']}],
                             'calls_complete': False}]}
    joined, facts = join_workflow_accounting({'test': [journal]}, accounting,
        admissions={journal['projection']['definition_id']: journal['projection']})
    record = joined['tests'][0]
    assert record['attribution'] == 'partial'
    assert record['missing_accounting_call_ids'] == ['00000000-0000-4000-8000-000000000002']
    assert record['unowned_accounting_call_ids'] == ['other']
    assert facts == {}


def test_central_call_turn_must_match_journal_turn():
    journal = recorded_call('00000000-0000-4000-8000-000000000003')
    accounting = {'tests': [{'test_id': 'test', 'calls': [{
        'call_id': '00000000-0000-4000-8000-000000000003',
        'turn_id': '00000000-0000-4000-8000-000000000099'}],
        'calls_complete': True}]}
    joined, facts = join_workflow_accounting({'test': [journal]}, accounting,
        admissions={journal['projection']['definition_id']: journal['projection']})
    assert joined['tests'][0]['attribution'] == 'partial'
    assert joined['tests'][0]['mismatched_turn_call_ids'] == [
        '00000000-0000-4000-8000-000000000003']
    assert facts == {}


def test_frozen_charge_and_list_quote_have_distinct_bases():
    from api_agent.testing.workflow_accounting import _call_facts
    call = {'usage': {'input_tokens': 3, 'output_tokens': 2},
            'quote': {'list_cost_usd': 0.03, 'accounting': {
                'billing_mode': 'api_key',
                'billing': {'usd': 0.02, 'request_id': 'example',
                            'generation_id': 'generation'}}}}
    facts = _call_facts(call)
    assert facts['reported_charge_usd'] == 0.02
    assert facts['reported_charge_basis'] == 'qualified_provider_billing'
    assert facts['list_cost_usd'] == 0.03
    assert facts['list_cost_basis'] == 'provider_quote'
    call['quote']['accounting']['billing_mode'] = 'subscription'
    assert _call_facts(call)['reported_charge_usd'] is None


def test_journal_without_selected_accounting_test_is_rejected():
    journal = recorded_call('00000000-0000-4000-8000-000000000004')
    with pytest.raises(ValueError, match='no selected accounting test'):
        join_workflow_accounting({'missing': [journal]}, {'tests': []})


def test_same_central_call_recorded_twice_is_partial_without_scope_facts():
    identifier = '00000000-0000-4000-8000-000000000005'
    journal = recorded_call(identifier)
    accounting = {'tests': [{'test_id': 'test', 'calls': [{
        'call_id': identifier, 'turn_id': journal['turn_id']}],
        'calls_complete': True}]}
    joined, facts = join_workflow_accounting({'test': [journal, journal]}, accounting,
        admissions={journal['projection']['definition_id']: journal['projection']})
    record = joined['tests'][0]
    assert record['attribution'] == 'partial'
    assert record['duplicate_journal_call_ids'] == [identifier]
    assert facts == {}
