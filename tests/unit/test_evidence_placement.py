"""Identity-based placement retains single file references through finalization."""
from copy import deepcopy
from itertools import permutations
import json
from uuid import UUID
from types import SimpleNamespace

import pytest

from api_agent.testing.evidence_placement import legacy_owner, observed_call_scopes, place_evidence
from api_agent.testing.workflow_accounting import group_execution_steps
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import run_recorded_integration
from api_agent.workflows.run_context import trial
from test_recorded_integration import fixture, Storage
from test_recorded_payload_projection import packet


def identifier(number):
    return str(UUID(int=number))


def step(name, **parameters):
    return dict(name=name, status='passed', start=1, stop=2, steps=[],
                parameters=[dict(name=key, value=str(value)) for key, value in parameters.items()])


def reference(name):
    return dict(name=name, source=name + '.json', type='application/json')


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_explicit_owners_ignore_order_and_retain_one_reference(order):
    first = step('graph GRAPH', scope_id=identifier(1), invocation_id=identifier(2), trial_index=1)
    second = step('graph GRAPH', scope_id=identifier(3), invocation_id=identifier(4), trial_index=2)
    call = step('call ' + identifier(5))
    first['steps'] = [call]
    unrelated = step('graph GRAPH', scope_id=identifier(6), invocation_id=identifier(7),
                     conversation_id=identifier(8), turn_id=identifier(9))
    steps = group_execution_steps([[first, second, unrelated][index] for index in order])
    queued = [({'kind': 'graph', 'invocation_id': identifier(4)}, reference('graph')),
              ({'kind': 'scope', 'invocation_id': identifier(2), 'scope_id': identifier(1)}, reference('scope')),
              ({'kind': 'call', 'call_id': identifier(5)}, reference('call')),
              ({'kind': 'trial', 'trial_index': 1}, reference('trial'))]
    assert place_evidence(steps, queued) == []
    assert place_evidence(steps, queued) == []
    assert first['attachments'] == [reference('scope')]
    assert second['attachments'] == [reference('graph')]
    assert call['attachments'] == [reference('call')]
    wrapper = next(row for row in steps if row['name'] == 'Trial 1')
    assert wrapper['attachments'] == [reference('trial')]
    regrouped = group_execution_steps(steps)
    assert place_evidence(regrouped, queued) == []
    assert next(row for row in regrouped if row['name'] == 'Trial 1')['attachments'] == [reference('trial')]


def test_missing_and_ambiguous_owners_are_unattached_and_graphfree_call_is_observed():
    call = step('call ' + identifier(1))
    container = step('Unattached')
    container['steps'] = [call]
    graphs = [step('graph GRAPH', invocation_id=identifier(2), scope_id=identifier(3)) for _ in range(2)]
    queued = [({'kind': 'graph', 'invocation_id': identifier(2)}, reference('ambiguous')),
              ({'kind': 'scope', 'invocation_id': identifier(4), 'scope_id': identifier(3)}, reference('missing')),
              ({'kind': 'trial', 'trial_index': 1}, reference('progress')),
              ({'kind': 'test'}, reference('summary')),
              ({'kind': 'unattached'}, reference('unknown')),
              ({'kind': 'call', 'call_id': identifier(1)}, reference('call'))]
    remaining = place_evidence([container, *graphs], queued)
    assert [row['name'] for row in remaining] == ['Unattached ambiguous', 'Unattached missing',
        'Unattached progress', 'summary', 'Unattached unknown']
    assert call['attachments'] == [reference('call')]
    assert container['name'] == 'Unattached' and len(container['steps']) == 1


@pytest.mark.parametrize('packet,owner,label', [
    ({'events': []}, {'kind': 'unattached'}, 'Execution diagnostics'),
    ({'trial': 2, 'seen': [], 'complete': False}, {'kind': 'trial', 'trial_index': 2}, 'Coverage progress'),
    ({'coverage': {}}, {'kind': 'test'}, 'Coverage summary'),
    ({'other': []}, {'kind': 'test'}, 'Workflow execution evidence'),
])
def test_legacy_classification(packet, owner, label):
    assert legacy_owner(packet) == (owner, label)


def test_legacy_diagnostics_require_unique_actual_graph_scope():
    packet = {'trial': 9, 'events': [dict(event='scope_start', kind='graph', parent_id=None, scope_id=identifier(1))]}
    owner, label = legacy_owner(packet)
    graph = step('graph GRAPH', scope_id=identifier(1), invocation_id=identifier(2), trial_index=1)
    assert place_evidence([graph], [(owner, reference(label))]) == []
    assert graph['attachments'] == [reference(label)]
    packet['events'].append(dict(packet['events'][0], scope_id=identifier(3)))
    assert legacy_owner(packet)[0] == {'kind': 'unattached'}


@pytest.mark.parametrize('reverse', [False, True])
def test_production_recording_places_custom_and_legacy_after_grouping(tmp_path, reverse):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    journals = []
    wire_events = []
    for index in (1, 2):
        with trial(index):
            journal, request, response = packet(identifier(index), identifier(index + 10), selected.node_id)
            journals.append(journal)
            wire_events.extend([dict(call_id=identifier(index), phase='request', content=request, reason='captured'),
                dict(call_id=identifier(index), phase='response', content=response, reason='partial')])
    events = []
    for index, journal in enumerate(journals, 1):
        events.extend([dict(kind='scoped_attachment', version=1, name='Untimed ' + str(index),
            owner=dict(kind='call', call_id=identifier(index)), content='withheld'), journal, {'trial': 99, 'events': [event for event in journal['events'] if event['event'] in ('scope_start', 'scope_end')]},
            {'trial': index, 'seen': ['Make'], 'complete': False},
            dict(kind='scoped_attachment', version=1, name='Custom ' + str(index),
                 owner=dict(kind='graph', invocation_id=journal['invocation_id']), content='permitted_scrubbed', value={'ok': index})])
    events.extend([{'coverage': {'complete': False}}, dict(kind='scoped_attachment', version=1,
        name='Missing', owner=dict(kind='call', call_id=identifier(99)), content='withheld')])
    outcome = TestResult(selected, 'passed', .01)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for data in wire_events:
            hooks['on_event'](RunnerEvent('inference_content', selected.repo, selected, text=json.dumps(data)))
        for data in reversed(events) if reverse else events:
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())
    assert result.persisted
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert {item['name'] for item in row['attachments']} == {'Coverage summary', 'Unattached Missing (capture diagnostics)', 'Payload index',
        'Suite usage', 'Workflow accounting join', 'Immutable accounting', 'Test cost estimate'}
    sources = []
    for wrapper in row['steps']:
        index = int(wrapper['name'].split()[-1])
        assert [item['name'] for item in wrapper['attachments']] == ['Coverage progress']
        graph = wrapper['steps'][0]
        attachments = {item['name']: item for item in graph['attachments']}
        assert set(attachments) == {'Execution evidence', 'Execution diagnostics', 'Custom ' + str(index)}
        node = graph['steps'][0]
        call = next(child for child in node['steps'] if child['name'] == 'call ' + identifier(index))
        diagnostic_name = 'Untimed ' + str(index) + ' (capture diagnostics)'
        assert diagnostic_name in [item['name'] for item in call['attachments']]
        assert diagnostic_name not in [item['name'] for item in node['attachments']]
        canonical = attachments['Execution evidence']['source']
        assert canonical.endswith(journals[index - 1]['invocation_id'] + '.json')
        sources.append(canonical)
        assert json.loads((result.directory / attachments['Custom ' + str(index)]['source']).read_bytes()) == {'ok': index}
    assert len(set(sources)) == 2 and len(list(result.directory.glob('workflow-0-*.json'))) == 2


@pytest.mark.parametrize('value,count', [(None, 257), ('x' * 64000, 70)])
def test_recording_rejects_aggregate_custom_attachment_overflow(tmp_path, value, count):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    data = dict(kind='scoped_attachment', version=1, name='Bounded', owner={'kind': 'test'},
                content='withheld' if value is None else 'permitted_scrubbed')
    if value is not None:
        data['value'] = value
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for _ in range(count):
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(data)))
        pytest.fail('Recording accepted excessive attachments')
    with pytest.raises(ValueError, match='recording limit exceeded'):
        run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())


@pytest.mark.parametrize('allow_content', [False, True])
def test_graphfree_custom_evidence_retains_withheld_test_and_unknown_owners(tmp_path, allow_content):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = allow_content
    observed = []
    outcome = TestResult(selected, 'passed', .01)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for owner in ({'kind': 'test'}, {'kind': 'graph', 'invocation_id': identifier(1)}):
            data = dict(kind='scoped_attachment', version=1, name='Withheld', owner=owner, content='withheld')
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner,
        storage=Storage(), on_event=observed.append)
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert row['steps'] == []
    evidence = [item for item in row['attachments'] if 'Withheld' in item['name']]
    assert [item['name'] for item in evidence] == ['Withheld (capture diagnostics)', 'Unattached Withheld (capture diagnostics)']
    assert all(json.loads((result.directory / item['source']).read_bytes()) == {
        'capture_reason':'withheld'} for item in evidence)
    assert len(observed) == 2


@pytest.mark.parametrize('conversation_context', [False, True])
def test_graphfree_call_evidence_moves_to_actual_leaves_after_accounting(tmp_path, conversation_context):
    context, selected, config = fixture(tmp_path)
    outcome = TestResult(selected, 'passed', .01)
    calls = [identifier(1), identifier(2)]
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for index, call in enumerate(calls):
            attachment = dict(kind='scoped_attachment', version=1, name='Call evidence',
                owner=dict(kind='call', call_id=call), content='withheld')
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(attachment)))
            base = dict(call_id=call, selection='synthetic-model', turn_id=identifier(index + 10),
                turn_kind='initial', attempt=1, started_at='2026-01-01T00:00:00+00:00',
                finished_at='2026-01-01T00:00:01+00:00')
            if index == 0 and conversation_context:
                base['conversation_id'] = identifier(20)
            for usage in (None, dict(input_tokens=1, output_tokens=2,
                    cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0)):
                data = dict(base, usage=usage, finished_at=base['finished_at'] if usage is not None else None)
                if usage is not None:
                    data.update(outcome='completed', quote={'list_cost_usd': .01, 'payment': None})
                hooks['on_event'](RunnerEvent('inference_usage', identity=selected, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    if conversation_context:
        assert [step['name'] for step in row['steps']] == ['Conversation ' + identifier(20), 'Unattached']
        leaves = [row['steps'][0]['steps'][0]['steps'][0], row['steps'][1]['steps'][0]]
    else:
        assert [step['name'] for step in row['steps']] == ['Unattached']
        leaves = row['steps'][0]['steps']
    assert [leaf['name'] for leaf in leaves] == ['call ' + call for call in calls]
    assert all([item['name'] for item in leaf['attachments']] == ['Call evidence (capture diagnostics)'] for leaf in leaves)
    assert len({leaf['attachments'][0]['source'] for leaf in leaves}) == 2
    assert not any('Call evidence' in item['name'] for item in row['attachments'])
    assert result.usage['test_costs'][selected.id]['list_cost_usd'] == .02


def test_untimed_call_claims_remain_exact_after_accounting_removes_parameters():
    first = step('graph GRAPH', scope_id=identifier(1), invocation_id=identifier(2))
    node = step('node Make', scope_id=identifier(3))
    node['parameters'].append(dict(name='call ' + identifier(4) + ' outcome', value='completed'))
    first['steps'] = [node]
    claims = observed_call_scopes([first])
    assert claims == {identifier(4): [(identifier(2), identifier(3))]}
    node['parameters'] = [row for row in node['parameters'] if row['name'] == 'scope_id']
    queued = [({'kind': 'call', 'call_id': identifier(4)}, reference('untimed'))]
    assert place_evidence([first], queued, call_scopes=claims) == []
    assert node['attachments'] == [reference('untimed')]
    conflicting = deepcopy(first)
    conflicting['parameters'][1]['value'] = identifier(5)
    conflicting['steps'][0]['parameters'].append(dict(name='call ' + identifier(4) + ' outcome', value='completed'))
    claims[identifier(4)].extend(observed_call_scopes([conflicting])[identifier(4)])
    remaining = place_evidence([first, conflicting], queued, call_scopes=claims)
    assert [row['name'] for row in remaining] == ['Unattached untimed']
    assert node['attachments'] == []


def test_partial_journal_without_graph_keeps_one_hydratable_file(tmp_path):
    from api_agent.workflows.journal import WorkflowJournal
    from api_agent.testing.payloads import PayloadResolver, hydrate_journal
    context, selected, config = fixture(tmp_path)
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[], tables={'Edges': []}), resources=[], replacements=[])
    journal = WorkflowJournal(definition, log_content=False, turn_id=identifier(1),
        test_id=selected.node_id).finalize()
    outcome = TestResult(selected, 'passed', .01)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(journal)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert result.persisted and row['steps'] == []
    retained = [item for item in row['attachments'] if item['name'] == 'Unattached Execution evidence']
    assert len(retained) == 1
    resolver = PayloadResolver.local(result.directory, result.manifest)
    resolver.index(next(item['source'] for item in row['attachments'] if item['name'] == 'Payload index'))
    envelope = json.loads((result.directory / retained[0]['source']).read_bytes())
    assert hydrate_journal(envelope, resolver) == journal
    assert len(list(result.directory.glob('workflow-0-*.json'))) == 1
