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
    graph = {'name': 'graph GRAPH', 'parameters': [{'name': 'scope_id', 'value': 'graph'},
            {'name': 'invocation_id', 'value': journal['invocation_id']}],
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
    assert set(facts) == {('test', None, 'other')}
    assert facts[('test', None, 'other')]['list_cost_usd'] is None


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


def test_explicit_conversation_groups_turns_and_keeps_invocations():
    from api_agent.testing.workflow_accounting import group_execution_steps
    def graph(conversation, turn, scope, status='passed'):
        return dict(name='graph GRAPH', start=1, stop=2, status=status, steps=[],
                    parameters=[dict(name=key, value=value) for key, value in
                                dict(conversation_id=conversation, turn_id=turn, scope_id=scope).items()])
    steps = group_execution_steps([graph('chat-a', 'turn-a', 'one'),
        graph('chat-a', 'turn-a', 'two', 'broken'), graph('chat-a', 'turn-b', 'three', 'failed'),
        graph('chat-b', 'turn-c', 'four')])
    assert [row['name'] for row in steps] == ['Conversation chat-a', 'Conversation chat-b']
    assert [row['name'] for row in steps[0]['steps']] == ['User turn 1', 'User turn 2']
    assert len(steps[0]['steps'][0]['steps']) == 2
    assert steps[0]['status'] == 'failed'
    assert steps[0]['steps'][0]['status'] == 'broken'
    assert steps[1]['steps'][0]['name'] == 'User turn 1'


def test_trials_and_legacy_graphs_never_invent_conversations():
    from api_agent.testing.workflow_accounting import group_execution_steps
    legacy = dict(name='graph GRAPH', start=1, stop=3, status='passed', steps=[], parameters=[])
    trial = dict(legacy, parameters=[dict(name='trial_index', value='2')])
    steps = group_execution_steps([legacy, trial])
    assert steps[0] is legacy
    assert steps[1]['name'] == 'Trial 2'
    assert steps[1]['steps'] == [trial]


def test_frozen_conversation_rollup_counts_unique_calls_including_unowned():
    from api_agent.testing.workflow_accounting import group_execution_steps, _conversation_summaries
    facts = {('test', 'leaf', 'one'): {'conversation_id': 'chat', 'turn_id': 'turn',
        **{name: 2 for name in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                               'cache_write_tokens', 'reasoning_tokens', 'list_cost_usd', 'reported_charge_usd')}},
        ('test', None, 'setup'): {'conversation_id': 'chat', 'turn_id': 'turn',
        **{name: None for name in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                                  'cache_write_tokens', 'reasoning_tokens', 'list_cost_usd', 'reported_charge_usd')}}}
    node = dict(name='node Make', parameters=[dict(name='scope_id', value='leaf')], steps=[], start=1, stop=2, status='passed')
    graph = dict(name='graph GRAPH', parameters=[dict(name=k, value=v) for k, v in
        dict(scope_id='graph', conversation_id='chat', turn_id='turn').items()], steps=[node], start=1, stop=2, status='passed')
    grouped = group_execution_steps([graph])
    apply_frozen_steps(grouped, 'test', facts)
    values = lambda row: {p['name']: p['value'] for p in row['parameters']}
    assert values(graph)['inclusive_list_cost_usd'] == '2.0'
    assert values(grouped[0])['inclusive_list_cost_usd'] == 'unknown'
    assert values(grouped[0]['steps'][0])['inclusive_list_cost_usd'] == 'unknown'
    calls = {'one': dict(call_id='one', conversation_id='chat', turn_id='turn', usage={'input_tokens': 2}, quote={'list_cost_usd': 2}),
             'setup': dict(call_id='setup', conversation_id='chat', turn_id='turn', usage=None)}
    summary = _conversation_summaries(calls)[0]
    assert summary['call_ids'] == ['one', 'setup']
    assert summary['list_cost_usd'] is None
    assert summary['turns'][0]['call_ids'] == ['one', 'setup']


def test_missing_observed_call_prevents_partial_sum_from_looking_complete():
    from api_agent.testing.workflow_accounting import _ScopeFacts, _call_facts
    facts = _ScopeFacts()
    facts[('test', 'leaf', 'known')] = _call_facts({'quote': {'list_cost_usd': 2}})
    facts.unknown[('test', 'leaf')].add('missing')
    node = dict(name='node Make', parameters=[dict(name='scope_id', value='leaf')], steps=[])
    graph = dict(name='graph GRAPH', parameters=[dict(name='scope_id', value='graph')], steps=[node])
    apply_frozen_steps([graph], 'test', facts)
    assert {row['name']: row['value'] for row in graph['parameters']}['inclusive_list_cost_usd'] == 'unknown'


def test_turns_and_invocations_follow_recorded_chronology():
    from api_agent.testing.workflow_accounting import group_execution_steps
    def graph(turn_id, invocation, start):
        return dict(name='graph GRAPH', start=start, stop=start + 1, status='passed', steps=[],
                    parameters=[dict(name=k, value=v) for k, v in dict(
                        conversation_id='chat', turn_id=turn_id, invocation_id=invocation).items()])
    steps = group_execution_steps([graph('later', 'three', 30), graph('earlier', 'two', 20), graph('earlier', 'one', 10)])
    turns = steps[0]['steps']
    assert [dict((p['name'], p['value']) for p in turn['parameters'])['turn_id'] for turn in turns] == ['earlier', 'later']
    assert [row['start'] for row in turns[0]['steps']] == [10, 20]
    assert [row['name'] for row in turns] == ['User turn 1', 'User turn 2']


def test_invocation_namespace_keeps_reused_scope_costs_separate():
    from api_agent.testing.workflow_accounting import _ScopeFacts, _call_facts
    facts = _ScopeFacts()
    graphs = []
    for invocation, call_id, price in [('first', 'one', 2), ('second', 'two', 3)]:
        key = ('test', 'leaf', call_id)
        facts[key] = _call_facts({'quote': {'list_cost_usd': price}})
        facts.invocations[key] = invocation
        graphs.append(dict(name='graph GRAPH', parameters=[dict(name='invocation_id', value=invocation)], steps=[
            dict(name='node Make', parameters=[dict(name='scope_id', value='leaf')], steps=[])]))
    apply_frozen_steps(graphs, 'test', facts)
    assert [{p['name']: p['value'] for p in graph['parameters']}['inclusive_list_cost_usd'] for graph in graphs] == ['2.0', '3.0']


def test_graph_free_conversation_has_summary_and_partial_native_steps():
    from api_agent.testing.workflow_accounting import include_unowned_calls
    call = dict(call_id='one', conversation_id='chat', turn_id='turn', outcome='completed',
                quote={'list_cost_usd': 2}, usage={})
    outside = dict(call_id='setup', turn_id='setup-turn', outcome='failed', usage={})
    accounting = {'tests': [dict(test_id='test', calls=[call, outside], calls_complete=False)]}
    joined, facts = join_workflow_accounting({}, accounting)
    record = joined['tests'][0]
    assert record['conversations'][0]['call_ids'] == ['one']
    assert record['conversations'][0]['turns'][0]['calls_complete'] is False
    steps = include_unowned_calls([], [call, outside], record['unowned_accounting_call_ids'])
    apply_frozen_steps(steps, 'test', facts)
    conversation = next(step for step in steps if step['name'] == 'Conversation chat')
    params = {p['name']: p['value'] for p in conversation['parameters']}
    assert params['calls_complete'] == 'false' and params['attribution'] == 'partial'
    assert params['inclusive_list_cost_usd'] == '2.0'
    unattached = next(step for step in steps if step['name'] == 'Unattached')
    assert unattached['status'] == 'failed'
    assert unattached['steps'][0]['name'] == 'call setup'
    assert 'start' not in conversation


def test_duplicate_observed_call_keeps_unknown_for_each_invocation():
    from api_agent.testing.recorded import _WorkflowStepBridge
    identifier = '00000000-0000-4000-8000-000000000006'
    journals = [recorded_call(identifier), recorded_call(identifier)]
    accounting = {'tests': [dict(test_id='test', calls=[dict(call_id=identifier,
        turn_id=journals[-1]['turn_id'])], calls_complete=True)]}
    _, facts = join_workflow_accounting({'test': journals}, accounting,
        admissions={journals[0]['projection']['definition_id']: journals[0]['projection']})
    graphs = [dict(name='graph GRAPH', parameters=[dict(name='invocation_id', value=journal['invocation_id'])],
        steps=[dict(name='node Make', steps=[], parameters=[dict(name='scope_id', value='visit')])])
        for journal in journals]
    steps = apply_frozen_steps(graphs, 'test', facts)
    assert [{p['name']: p['value'] for p in step['parameters']}['inclusive_list_cost_usd']
            for step in steps] == ['unknown', 'unknown']


def test_turn_sequence_uses_precise_recorded_time_before_allure_truncation():
    from api_agent.testing.workflow_accounting import group_execution_steps
    def graph(turn_id, precise):
        return dict(name='graph GRAPH', start=100, stop=101, status='passed', steps=[],
            parameters=[dict(name=k, value=v) for k, v in dict(conversation_id='chat',
                turn_id=turn_id, recorded_started_ms=str(precise)).items()])
    steps = group_execution_steps([graph('a-later', 100.9), graph('z-earlier', 100.1)])
    assert {p['name']: p['value'] for p in steps[0]['steps'][0]['parameters']}['turn_id'] == 'z-earlier'


def test_pending_graph_free_calls_keep_known_start_order_without_fake_stop():
    from api_agent.testing.workflow_accounting import include_unowned_calls
    calls = [dict(call_id='later', conversation_id='chat', turn_id='a-later',
                  started_at='2026-01-01T00:00:00.000900+00:00', finished_at=None),
             dict(call_id='earlier', conversation_id='chat', turn_id='z-earlier',
                  started_at='2026-01-01T00:00:00.000100+00:00', finished_at=None)]
    steps = include_unowned_calls([], calls, ['earlier', 'later'])
    turns = steps[0]['steps']
    assert {p['name']: p['value'] for p in turns[0]['parameters']}['turn_id'] == 'z-earlier'
    assert all('stop' not in turn['steps'][0] for turn in turns)


def test_duplicate_authoritative_call_identity_is_rejected_before_rollup():
    call = dict(call_id='00000000-0000-4000-8000-000000000007',
                conversation_id='00000000-0000-4000-8000-000000000008',
                turn_id='00000000-0000-4000-8000-000000000009',
                quote={'list_cost_usd': 2}, usage={})
    accounting = {'tests': [dict(test_id='test', calls=[call, dict(call)], calls_complete=True)]}
    with pytest.raises(ValueError, match='Duplicate authoritative accounting call'):
        join_workflow_accounting({}, accounting)


def test_repeated_trial_groups_untimed_graphs_without_fabricated_interval():
    from api_agent.testing.workflow_accounting import group_execution_steps
    first = dict(name='graph GRAPH', status='passed', steps=[],
                 parameters=[dict(name='trial_index', value='2')])
    second = dict(name='graph GRAPH', status='broken', steps=[],
                  parameters=[dict(name='trial_index', value='2')])
    steps = group_execution_steps([first, second])
    assert len(steps) == 1
    assert steps[0]['name'] == 'Trial 2' and steps[0]['steps'] == [first, second]
    assert steps[0]['status'] == 'broken'
    assert 'start' not in steps[0] and 'stop' not in steps[0]


def test_unverified_definition_never_asserts_structured_leaf_ownership_or_cost():
    identifier = '00000000-0000-4000-8000-000000000010'
    journal = recorded_call(identifier)
    call = dict(call_id=identifier, turn_id=journal['turn_id'], usage={'input_tokens': 3},
                quote={'list_cost_usd': 2})
    joined, facts = join_workflow_accounting({'test': [journal]}, {'tests': [dict(
        test_id='test', calls=[call], calls_complete=True)]})
    assert joined['tests'][0]['attribution'] == 'partial'
    assert joined['tests'][0]['leaf_calls'] == []
    assert facts == {}


def test_unattached_central_call_keeps_known_facts_without_any_conversation():
    from api_agent.testing.workflow_accounting import include_unowned_calls
    identifier = '00000000-0000-4000-8000-000000000011'
    journal = recorded_call('00000000-0000-4000-8000-000000000012')
    call = dict(call_id=identifier, turn_id=journal['turn_id'], usage={'input_tokens': 7},
                outcome='completed', quote={'list_cost_usd': 2})
    joined, facts = join_workflow_accounting({'test': [journal]}, {'tests': [dict(
        test_id='test', calls=[call], calls_complete=True)]})
    steps = include_unowned_calls([], [call], joined['tests'][0]['unowned_accounting_call_ids'])
    apply_frozen_steps(steps, 'test', facts)
    assert steps[0]['name'] == 'Unattached'
    leaf = steps[0]['steps'][0]
    parameters = {item['name']: item['value'] for item in leaf['parameters']}
    assert parameters['input_tokens'] == '7' and parameters['list_cost_usd'] == '2'
    assert 'conversation_id' not in parameters


def test_frozen_occurrence_keeps_requested_actual_provider_and_version_facts():
    identifier = '00000000-0000-4000-8000-000000000013'
    journal = recorded_call(identifier)
    call = dict(call_id=identifier, turn_id=journal['turn_id'], selection='selected-model',
                actual_model='served-model', reasoning_effort='high', service_tier='priority',
                serving_revision='revision-2', usage={'input_tokens': 3, 'output_tokens': 2,
                    'cached_input_tokens': 0, 'reasoning_tokens': 0},
                started_at=None, finished_at=None, attempt=1, turn_kind='initial', outcome='completed',
                quote={'list_cost_usd': 2, 'payment': None})
    from api_agent.telemetry.accounting import compatibility, validate_evidence
    call['quote']['accounting'] = dict(version=1, requested_model='requested-model',
        catalog_model='catalog-model', actual_model='served-model', service_tier='priority',
        serving_revision='revision-2', billing_mode='unknown', rate_card=None,
        reasoning_effort='high', compatibility=compatibility({'_COST_PROVIDER_IDENTITY': 'a' * 64}))
    validate_evidence(call)
    joined, facts = join_workflow_accounting({'test': [journal]}, {'tests': [dict(
        test_id='test', calls=[call], calls_complete=True)]}, admissions={
            journal['projection']['definition_id']: journal['projection']})
    leaves = [dict(name='call ' + identifier, parameters=[], steps=[])]
    apply_frozen_steps(leaves, 'test', facts)
    parameters = {item['name']: item['value'] for item in leaves[0]['parameters']}
    expected = dict(reasoning_effort='high', service_tier='priority', serving_revision='revision-2',
                    requested_model='requested-model', actual_model='served-model',
                    catalog_model='catalog-model', provider_identity='a' * 64)
    assert {key: parameters[key] for key in expected} == expected
    assert {key: joined['tests'][0]['leaf_calls'][0][key] for key in expected} == expected
    from api_agent.testing.workflow_accounting import _call_facts
    assert all(_call_facts({})[key] is None for key in expected)
