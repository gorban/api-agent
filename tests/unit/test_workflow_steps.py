from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from api_agent.testing.workflow_steps import scope_steps, _identifier, _time
from api_agent.testing.recorded import run_recorded_integration
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult, RunnerEvent
from api_agent.testing.integration import IntegrationRunResult
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy


def trace():
    root, child = str(uuid4()), str(uuid4())
    start = 1791800000000
    outer = dict(event='scope_start',scope_id=root,parent_id=None,owner='GRAPH',kind='graph',started_ms=start,attempt=1,max_passes=1)
    inner = dict(event='scope_start',scope_id=child,parent_id=root,owner='first',kind='component',started_ms=start+1,attempt=1,max_passes=2,effective_timeout=60,declared_timeout=120,timeout_key='TEST_TIMEOUT')
    return [outer,inner,dict(event='attempt',scope_id=child,attempt=1),dict(event='guard',scope_id=child,accepted=False),
            dict(event='attempt',scope_id=child,attempt=2),dict(inner,event='scope_end',outcome='passed',stopped_ms=start+5),
            dict(outer,event='scope_end',outcome='passed',stopped_ms=start+6)]


def test_scope_steps_preserve_nested_intervals_explicit_outcomes_and_attempts():
    rows=scope_steps(trace())
    assert len(rows)==1 and rows[0]['name']=='graph GRAPH' and rows[0]['status']=='broken'
    child=rows[0]['steps'][0]
    assert child['name']=='component first' and child['stop']-child['start']==4
    assert {'name':'attempts_reached','value':'2'} in child['parameters']
    assert {'name':'guard_rejections','value':'1'} in child['parameters']
    assert {'name':'effective_timeout','value':'60'} in child['parameters']


@pytest.mark.parametrize('outcome,status',[('failed','failed'),('cancelled','broken'),('partial','broken'),('unknown','broken')])
def test_scope_status_is_not_inferred_from_progress_stopping(outcome,status):
    nested = trace()
    events = [nested[0], nested[-1]]  # Child attention cannot mask the root status.
    events[-1]['outcome'] = outcome
    graph = scope_steps(events)[0]
    assert graph['status'] == status
    assert {'name': 'outcome', 'value': outcome} in graph['parameters']


@pytest.mark.parametrize('mutation',[
    lambda rows: rows.insert(0,'not an event'),
    lambda rows: rows[0].update(scope_id='bad'),
    lambda rows: rows[0].update(parent_id=rows[0]['scope_id']),
    lambda rows: rows[0].update(kind='unknown'),
    lambda rows: rows[0].update(owner='https://private.invalid'),
    lambda rows: rows[0].update(owner=1),
    lambda rows: rows[0].update(started_ms=True),
    lambda rows: rows.insert(1,deepcopy(rows[0])),
    lambda rows: rows[-1].update(started_ms=0),
    lambda rows: rows[-1].update(outcome='unsupported'),
    lambda rows: rows[-1].update(stopped_ms=-1),
    lambda rows: rows.append(deepcopy(rows[-1])),
    lambda rows: rows.pop(),
    lambda rows: rows[1].update(started_ms=rows[0]['started_ms']-1),
])
def test_invalid_lifecycle_cannot_claim_success(mutation):
    events=trace();mutation(events)
    with pytest.raises(ValueError):scope_steps(events)


def test_nonworkflow_heartbeats_never_claim_steps():
    assert scope_steps([{'event':'node','node':'first'},{'event':'guard','scope_id':'absent','accepted':False}])==[]
    assert not _identifier(None) and not _identifier('invalid')
    assert not _time(float('inf'))


def test_unknown_external_test_parent_rejected():
    events=trace();parent=str(uuid4())
    events[0]['parent_id']=parent;events[-1]['parent_id']=parent
    with pytest.raises(ValueError, match='parent is absent'):
        scope_steps(events)


def test_accepted_guard_is_not_a_rejection():
    events=trace();events[3]['accepted']=True
    assert not any(p['name']=='guard_rejections' for p in scope_steps(events)[0]['steps'][0]['parameters'])


def test_consistent_child_interval_still_must_fit_parent():
    events=trace()
    events[1]['started_ms']-=10;events[5]['started_ms']-=10
    with pytest.raises(ValueError,match='outside its owner'):scope_steps(events)


def test_cyclic_ownership_cannot_silently_discard_completed_scopes():
    events=trace()
    events[0]['parent_id']=events[1]['scope_id']
    events[-1]['parent_id']=events[1]['scope_id']
    with pytest.raises(ValueError,match='cyclic'):scope_steps(events)


def test_recorded_integration_publishes_workflow_steps_with_content_disabled(tmp_path):
    context=SimpleNamespace(base_root=tmp_path/'base',owner_root=tmp_path/'base')
    test=TestIdentity('base::test.py::test_graph','base','test.py','test.py::test_graph')
    result=TestResult(test,'passed',.01)
    class Storage:
        def persist(self,directory,manifest): pass
    def runner(context,tests,**hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('workflow_evidence','base',test,text=json.dumps(dict(trial=1,events=trace(),output='secret prompt'))))
        hooks['on_result'](result)
        execution=IntegrationRunResult(0,(result,),'')
        hooks['after_run'](execution)
        return execution
    recorded=run_recorded_integration(context,[test],{'TUI_MODE':'integration','ALLURE_S3_BUCKET':'synthetic',
        'ALLURE_RESULTS_DIR':tmp_path/'results','LOG_CONTENT':False},accepted=True,storage=Storage(),runner=runner,
        evidence_boundary=EmissionBoundary(ContentPolicy(False)))
    assert recorded.persisted and recorded.exit_code==0
    row=json.loads(next(recorded.directory.glob('*-result.json')).read_text())
    assert row['steps'][0]['steps'][0]['name']=='component first'
    contents=''.join(p.read_text() for p in recorded.directory.iterdir())
    assert 'secret prompt' not in contents
    assert any('scope_start' in p.read_text() for p in recorded.directory.glob('*-attachment'))


def test_leaf_call_cost_rolls_up_once_and_unknown_stays_unknown():
    events = trace()
    node = str(uuid4())
    parent = events[1]['scope_id']
    start = events[1]['started_ms'] + 1
    events.insert(2, dict(event='scope_start', scope_id=node, parent_id=parent,
                          owner='Make', kind='node', started_ms=start))
    events.insert(3, dict(event='call', call_id=str(uuid4()), visit_id=node,
                          usage={'input_tokens': 3, 'estimated_cost_usd': 0.01},
                          outcome='completed'))
    events.insert(4, dict(event='scope_end', scope_id=node, parent_id=parent,
                          owner='Make', kind='node', started_ms=start,
                          stopped_ms=start + 1, outcome='passed'))
    graph = scope_steps(events)[0]
    totals = {item['name']: item['value'] for item in graph['parameters']}
    assert totals['inclusive_input_tokens'] == '3.0'
    assert totals['inclusive_estimated_cost_usd'] == '0.01'
    assert totals['inclusive_output_tokens'] == 'unknown'


def test_scope_steps_rejects_multiple_graph_roots():
    events = trace()
    second = str(uuid4())
    events.extend((dict(event='scope_start', scope_id=second, parent_id=None,
                        owner='GRAPH', kind='graph', started_ms=1791800000000),
                   dict(event='scope_end', scope_id=second, parent_id=None,
                        owner='GRAPH', kind='graph', started_ms=1791800000000,
                        stopped_ms=1791800000001, outcome='passed')))
    with pytest.raises(ValueError, match='one graph root'):
        scope_steps(events)


def test_leaf_call_owner_usage_and_timing_are_checked_independently():
    events = trace()
    node = str(uuid4())
    parent = events[1]['scope_id']
    start = events[1]['started_ms'] + 1
    events.insert(2, dict(event='scope_start', scope_id=node, parent_id=parent,
                          owner='Make', kind='node', started_ms=start))
    call = dict(event='call', call_id=str(uuid4()), visit_id=node,
                usage={'input_tokens': 3}, selection='synthetic', outcome='completed',
                started_ms=start, stopped_ms=start + 1)
    events.insert(3, call)
    events.insert(4, dict(event='scope_end', scope_id=node, parent_id=parent,
                          owner='Make', kind='node', started_ms=start,
                          stopped_ms=start + 1, outcome='passed'))
    node_step = scope_steps(events)[0]['steps'][0]['steps'][0]
    assert node_step['steps'][0]['name'] == 'call ' + call['call_id']
    assert {'name': 'selection', 'value': 'synthetic'} in node_step['steps'][0]['parameters']
    broken_usage = deepcopy(events)
    broken_usage[3]['usage'] = [1]
    with pytest.raises(ValueError, match='leaf usage'):
        scope_steps(broken_usage)
    wrong_owner = deepcopy(events)
    wrong_owner[3]['visit_id'] = parent
    with pytest.raises(ValueError, match='unique node visit'):
        scope_steps(wrong_owner)
    duplicate = deepcopy(events)
    duplicate.insert(4, deepcopy(call))
    with pytest.raises(ValueError, match='unique node visit'):
        scope_steps(duplicate)


@pytest.mark.parametrize('evidence', ['allowance', 'first_attempt', 'retry', 'rejection'])
def test_only_observed_retry_requires_attention(evidence):
    events = [event for event in trace() if event['event'] not in ('attempt', 'guard')]
    component = events[1]
    if evidence in ('first_attempt', 'retry'):
        events.insert(2, dict(event='attempt', scope_id=component['scope_id'],
                              attempt=2 if evidence == 'retry' else 1))
    elif evidence == 'rejection':
        events.insert(2, dict(event='guard', scope_id=component['scope_id'], accepted=False))
    graph = scope_steps(events)[0]
    expected = 'broken' if evidence == 'retry' else 'passed'
    assert graph['status'] == graph['steps'][0]['status'] == expected
    assert {'name': 'outcome', 'value': 'passed'} in graph['parameters']


def visit_events(events, *, outcome='passed', call_outcome=None, timed=True):
    component = events[1]
    node = dict(event='scope_start', scope_id=str(uuid4()), parent_id=component['scope_id'],
                owner='Make', kind='node', started_ms=component['started_ms'] + 1,
                attempt=2)
    visit = [node]
    if call_outcome is not None:
        call = dict(event='call', call_id=str(uuid4()), visit_id=node['scope_id'],
                    outcome=call_outcome, usage={'input_tokens': 3})
        if timed:
            call.update(started_ms=node['started_ms'], stopped_ms=node['started_ms'] + 1)
        visit.append(call)
    visit.append(dict(node, event='scope_end', stopped_ms=node['started_ms'] + 1, outcome=outcome))
    events[2:2] = visit
    return events


@pytest.mark.parametrize('outcome,status', [('completed', 'passed'), ('tool_calls', 'passed'),
    ('failed', 'failed'), ('cancelled', 'broken'), ('length', 'broken'), ('refused', 'broken')])
@pytest.mark.parametrize('timed', [True, False])
def test_call_attention_reaches_ancestors_even_without_leaf_timing(outcome, status, timed):
    events = [event for event in trace() if event['event'] not in ('attempt', 'guard')]
    graph = scope_steps(visit_events(events, call_outcome=outcome, timed=timed))[0]
    component = graph['steps'][0]
    node = component['steps'][0]
    assert graph['status'] == component['status'] == node['status'] == status
    assert {'name': 'outcome', 'value': 'passed'} in node['parameters']
    assert len(node['steps']) == 1
    leaf = node['steps'][0]
    assert leaf['status'] == status
    assert {'name': 'outcome', 'value': outcome} in leaf['parameters']
    assert ('start' in leaf) is timed and ('stop' in leaf) is timed
    assert not any(item['name'].startswith('call ') for item in node['parameters'])


def test_retry_attention_preserves_successful_visits_and_siblings():
    events = visit_events(trace())
    sibling = dict(event='scope_start', scope_id=str(uuid4()), parent_id=events[0]['scope_id'],
                   owner='second', kind='component', started_ms=events[1]['started_ms'])
    events[1:1] = [sibling, dict(sibling, event='scope_end', outcome='passed',
                                stopped_ms=sibling['started_ms'] + 1)]
    graph = scope_steps(events)[0]
    assert graph['status'] == 'broken'
    assert graph['steps'][0]['status'] == 'passed'
    assert graph['steps'][1]['status'] == 'broken'
    assert graph['steps'][1]['steps'][0]['status'] == 'passed'
    retry = {'name': 'retry_observed', 'value': 'true'}
    assert retry in graph['parameters'] and retry in graph['steps'][1]['parameters']
    assert retry not in graph['steps'][0]['parameters']
    assert retry not in graph['steps'][1]['steps'][0]['parameters']


def test_terminal_failure_outranks_retry_and_keeps_successful_child_green():
    events = visit_events(trace())
    events[-2]['outcome'] = 'failed'
    graph = scope_steps(events)[0]
    assert graph['status'] == graph['steps'][0]['status'] == 'failed'
    assert {'name': 'outcome', 'value': 'passed'} in graph['parameters']
    assert graph['steps'][0]['steps'][0]['status'] == 'passed'


def correction_trace():
    events = trace()
    component = events[1]
    first = dict(event='scope_start', scope_id=str(uuid4()),
        parent_id=component['scope_id'], owner='Make', kind='node',
        started_ms=component['started_ms'] + 1, attempt=1, max_passes=2)
    second = dict(first, scope_id=str(uuid4()), started_ms=first['started_ms'] + 1, attempt=2)
    events[3:3] = [first, dict(first, event='scope_end', outcome='failed',
        stopped_ms=second['started_ms']), second,
        dict(second, event='scope_end', outcome='passed', stopped_ms=second['started_ms'] + 1)]
    return events


def test_recovered_producer_is_yellow_without_rewriting_failed_outcome():
    events = correction_trace()
    graph = scope_steps(events)[0]
    component = graph['steps'][0]
    rejected, accepted = component['steps']
    assert graph['status'] == component['status'] == rejected['status'] == 'broken'
    assert accepted['status'] == 'passed'
    assert dict(name='outcome', value='failed') in rejected['parameters']
    assert dict(name='recovered_by_scope_id', value=accepted['parameters'][0]['value']) in rejected['parameters']


@pytest.mark.parametrize('change', ['terminal_component', 'different_owner', 'same_attempt',
    'no_observed_retry', 'later_failed', 'overlap', 'invalid_attempt', 'outside_budget',
    'unguarded', 'different_budget', 'observed_over_budget'])
def test_unproven_or_terminal_recovery_remains_red(change):
    events = correction_trace()
    if change == 'terminal_component':
        events[-2]['outcome'] = 'failed'
    elif change == 'different_owner':
        events[5]['owner'] = events[6]['owner'] = 'Other'
    elif change == 'same_attempt':
        events[5]['attempt'] = events[6]['attempt'] = 1
    elif change == 'no_observed_retry':
        events = [event for event in events if event['event'] != 'attempt']
    elif change == 'later_failed':
        events[6]['outcome'] = 'failed'
    elif change == 'overlap':
        events[4]['stopped_ms'] += .5
    elif change == 'invalid_attempt':
        events[3]['attempt'] = events[4]['attempt'] = True
    elif change == 'unguarded':
        events[1]['max_passes'] = events[-2]['max_passes'] = 1
    elif change == 'different_budget':
        events[5]['max_passes'] = events[6]['max_passes'] = 3
    elif change == 'observed_over_budget':
        events[5]['attempt'] = events[6]['attempt'] = events[8]['attempt'] = 3
    else:
        events[5]['attempt'] = events[6]['attempt'] = 3
    graph = scope_steps(events)[0]
    assert graph['status'] == graph['steps'][0]['status'] == 'failed'
    assert graph['steps'][0]['steps'][0]['status'] == 'failed'


@pytest.mark.parametrize('replacement_call', ['completed', 'failed'])
def test_recovery_includes_failed_call_only_when_replacement_has_no_terminal_failure(replacement_call):
    events = correction_trace()
    for index, outcome in ((5, replacement_call), (3, 'failed')):
        events.insert(index + 1, dict(event='call', call_id=str(uuid4()),
            visit_id=events[index]['scope_id'], outcome=outcome, usage=None))
    graph = scope_steps(events)[0]
    rejected, accepted = graph['steps'][0]['steps']
    expected = 'broken' if replacement_call == 'completed' else 'failed'
    assert graph['status'] == rejected['status'] == rejected['steps'][0]['status'] == expected
    assert accepted['status'] == ('passed' if replacement_call == 'completed' else 'failed')


def test_passing_same_named_node_in_another_component_cannot_recover_failure():
    events = correction_trace()
    other = dict(events[1], scope_id=str(uuid4()), owner='Other')
    events[5]['parent_id'] = events[6]['parent_id'] = other['scope_id']
    events[1:1] = [other, dict(other, event='scope_end', outcome='passed',
        stopped_ms=events[-2]['stopped_ms'])]
    graph = scope_steps(events)[0]
    assert graph['status'] == 'failed'
    assert graph['steps'][1]['steps'][0]['status'] == 'failed'


def test_rejected_native_candidate_preserves_its_successful_llm_call_green():
    events = correction_trace()
    events.insert(4, dict(event='call', call_id=str(uuid4()),
        visit_id=events[3]['scope_id'], outcome='completed', usage=None))
    graph = scope_steps(events)[0]
    rejected = graph['steps'][0]['steps'][0]
    assert rejected['status'] == 'broken'
    assert rejected['steps'][0]['status'] == 'passed'


def test_missing_replacement_cannot_keep_a_recovery_label():
    from api_agent.testing.workflow_steps import bubble_attention
    graph = scope_steps(correction_trace())[0]
    component = graph['steps'][0]
    component['steps'].pop()
    bubble_attention(graph)
    rejected = component['steps'][0]
    assert graph['status'] == component['status'] == rejected['status'] == 'failed'
    assert not any(item['name'] == 'recovered_by_scope_id' for item in rejected['parameters'])


@pytest.mark.parametrize('outer_outcome', ['passed', 'failed'])
@pytest.mark.parametrize('inner_passes', [1, 2])
@pytest.mark.parametrize('node_passes', [1, 2])
def test_nested_producer_uses_its_enclosing_retry_controller(outer_outcome, inner_passes, node_passes):
    events = correction_trace()
    inner_id = events[1]['scope_id']
    outer = dict(events[1], scope_id=str(uuid4()), owner='Outer',
        started_ms=events[0]['started_ms'] + .5)
    for event in events:
        if event.get('kind') == 'node':
            event['max_passes'] = node_passes
        if event.get('scope_id') == inner_id:
            if event['event'] in ('scope_start', 'scope_end'):
                event.update(parent_id=outer['scope_id'], max_passes=inner_passes)
            elif event['event'] in ('attempt', 'guard'):
                event['scope_id'] = outer['scope_id']
    events.insert(1, outer)
    events.insert(-1, dict(outer, event='scope_end', outcome=outer_outcome,
        stopped_ms=events[-1]['stopped_ms'] - .5))
    graph = scope_steps(events)[0]
    outer_step = graph['steps'][0]
    inner_step = outer_step['steps'][0]
    first, second = inner_step['steps']
    expected = 'broken' if outer_outcome == 'passed' and inner_passes == 1 and node_passes == 2 else 'failed'
    assert graph['status'] == outer_step['status'] == inner_step['status'] == first['status'] == expected
    assert second['status'] == 'passed'


def test_unknown_call_outcome_is_bounded_before_rendering():
    events = visit_events(trace(), call_outcome='unrecognized provider detail')
    graph = scope_steps(events)[0]
    leaf = graph['steps'][0]['steps'][0]['steps'][0]
    assert leaf['status'] == 'broken'
    assert {'name': 'outcome', 'value': 'unknown'} in leaf['parameters']
    assert 'unrecognized provider detail' not in json.dumps(graph)


def test_mixed_llm_tool_children_follow_observation_order_with_untimed_call_ownership():
    root, visit, tool = (str(uuid4()) for _ in range(3))
    initial, relevance, continuation = (str(uuid4()) for _ in range(3))
    graph = dict(event='scope_start', scope_id=root, parent_id=None,
        kind='graph', owner='GRAPH', started_ms=0)
    node = dict(event='scope_start', scope_id=visit, parent_id=root,
        kind='node', owner='Converse', started_ms=1)
    def call(identifier):
        return dict(event='call', call_id=identifier, visit_id=visit,
            outcome='completed', usage={'input_tokens': 2})
    owner = dict(tool_invocation_id=tool, visit_id=visit, attempt_id=None)
    events = [graph, node, call(initial), call(relevance),
        dict(owner, event='tool_start', tool_name='lookup', started_ms=2),
        dict(owner, event='capture', direction='input'),
        dict(owner, event='capture', direction='output'),
        dict(owner, event='tool_end', tool_name='lookup', outcome='success', stopped_ms=3),
        call(continuation), dict(node, event='scope_end', outcome='passed', stopped_ms=4),
        dict(graph, event='scope_end', outcome='passed', stopped_ms=5)]

    projected = scope_steps(events)[0]['steps'][0]

    assert [leaf['name'] for leaf in projected['steps']] == [
        'call ' + initial, 'call ' + relevance, 'Tool lookup', 'call ' + continuation]
    tool_leaf = projected['steps'][2]
    assert {'name': 'tool_invocation_id', 'value': tool} in tool_leaf['parameters']
    assert (tool_leaf['start'], tool_leaf['stop']) == (2, 3)
    calls = [projected['steps'][index] for index in (0, 1, 3)]
    assert all('start' not in leaf and 'stop' not in leaf for leaf in calls)
    assert all({'name': 'input_tokens', 'value': '2'} in leaf['parameters'] for leaf in calls)
    assert {'name': 'inclusive_input_tokens', 'value': '6.0'} in projected['parameters']
    assert not any(item['name'].startswith('call ') for item in projected['parameters'])
