"""Journal/legacy recording joins use admitted execution identities, never names."""
from copy import deepcopy
from itertools import permutations
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import _WorkflowStepBridge, run_recorded_integration
from api_agent.workflows import fixture_driver
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from test_workflow_steps import trace


def packet(events, invocation=None):
    value = {'events': events}
    if invocation is not None:
        value.update(version=1, projection={}, invocation_id=invocation)
    return value


def flatten(step):
    yield step
    for child in step['steps']:
        yield from flatten(child)


@pytest.mark.parametrize('journal_first', [True, False])
def test_enriched_journal_and_legacy_share_one_scope_without_losing_new_visits(journal_first):
    legacy = trace()
    enriched = deepcopy(legacy)
    parent = legacy[1]['scope_id']
    node = dict(event='scope_start', scope_id=str(uuid4()), parent_id=parent,
                owner='Make', kind='node', started_ms=legacy[1]['started_ms'] + 1)
    enriched[2:2] = [node, dict(event='call', call_id=str(uuid4()), visit_id=node['scope_id'],
        usage={'input_tokens': 3}, outcome='completed', started_ms=node['started_ms'],
        stopped_ms=node['started_ms'] + 1), dict(node, event='scope_end',
        stopped_ms=node['started_ms'] + 1, outcome='passed')]
    journal = packet(enriched, str(uuid4()))
    original_journal = deepcopy(journal)
    bridge = _WorkflowStepBridge()
    for item in ([journal, packet(legacy)] if journal_first else [packet(legacy), journal]):
        bridge.add(item)
    bridge.add(journal)
    assert len(bridge.steps) == 1
    assert any(step['name'] == 'node Make' for step in flatten(bridge.steps[0]))
    assert {'name': 'inclusive_input_tokens', 'value': '3.0'} in bridge.steps[0]['parameters']
    # A later packet may contain an additional completed visit to the same node.
    extra = deepcopy(legacy)
    next_node = dict(node, scope_id=str(uuid4()))
    extra[2:2] = [next_node, dict(event='call', call_id=str(uuid4()), visit_id=next_node['scope_id'],
        usage={'input_tokens': 4}, outcome='completed', started_ms=next_node['started_ms'],
        stopped_ms=next_node['started_ms'] + 1), dict(next_node, event='scope_end',
        stopped_ms=next_node['started_ms'] + 1, outcome='passed')]
    bridge.add(packet(extra))
    assert len([step for step in flatten(bridge.steps[0]) if step['name'] == 'node Make']) == 2
    assert len([step for step in flatten(bridge.steps[0]) if step['name'].startswith('call ')]) == 2
    assert journal == original_journal


@pytest.mark.parametrize('conflict', ['parent', 'max_passes', 'outcome'])
def test_same_scope_identity_conflicts_are_visible(conflict):
    original = trace()
    changed = deepcopy(original)
    if conflict == 'max_passes':
        changed[1]['max_passes'] = 9
    elif conflict == 'outcome':
        changed[-1]['outcome'] = 'failed'
    else:
        # Both trees are individually valid, but a stable node cannot move owners.
        node = dict(event='scope_start', scope_id=str(uuid4()), parent_id=original[1]['scope_id'],
                    owner='Make', kind='node', started_ms=original[1]['started_ms'] + 1)
        original[2:2] = [node, dict(node, event='scope_end', outcome='passed', stopped_ms=node['started_ms'] + 1)]
        moved = dict(node, parent_id=original[0]['scope_id'])
        changed[2:2] = [moved, dict(moved, event='scope_end', outcome='passed', stopped_ms=node['started_ms'] + 1)]
    bridge = _WorkflowStepBridge()
    bridge.add(packet(original))
    with pytest.raises(ValueError, match='Conflicting workflow scope'):
        bridge.add(packet(changed))


def test_distinct_invocations_and_ambiguous_legacy_are_not_silently_joined():
    events = trace()
    bridge = _WorkflowStepBridge()
    for invocation in (str(uuid4()), str(uuid4())):
        bridge.add(packet(events, invocation))
    bridge.add(packet(events))
    assert len(bridge.steps) == 3


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_late_invocation_ambiguity_restores_legacy_provenance(order):
    legacy = trace()
    journal = [event for event in deepcopy(legacy) if event['event'] not in ('attempt', 'guard')]
    packets = [packet(legacy), packet(journal, str(uuid4())), packet(journal, str(uuid4()))]
    originals = deepcopy(packets)
    bridge = _WorkflowStepBridge()
    for index in order:
        bridge.add(packets[index])
    bridge.add(packets[0])
    assert len(bridge.steps) == 3
    for entry in bridge.entries:
        parameters = entry['step']['steps'][0]['parameters']
        assert ({'name': 'guard_rejections', 'value': '1'} in parameters) == (entry['invocation'] is None)
        assert ({'name': 'attempts_reached', 'value': '2'} in parameters) == (entry['invocation'] is None)
    assert packets == originals


@pytest.mark.parametrize('journal_first', [True, False])
def test_journal_values_win_over_legacy_overlap(journal_first):
    legacy = trace()
    journal = deepcopy(legacy)
    journal.insert(4, deepcopy(journal[3]))
    packets = [packet(journal, str(uuid4())), packet(legacy)]
    bridge = _WorkflowStepBridge()
    for item in packets if journal_first else reversed(packets):
        bridge.add(item)
    assert len(bridge.steps) == 1
    assert {'name': 'guard_rejections', 'value': '2'} in bridge.steps[0]['steps'][0]['parameters']


def test_failed_join_preserves_validated_originals_for_later_ambiguity():
    legacy = trace()
    journal = [event for event in deepcopy(legacy) if event['event'] not in ('attempt', 'guard')]
    first = packet(journal, str(uuid4()))
    bridge = _WorkflowStepBridge()
    bridge.add(packet(legacy))
    bridge.add(first)
    before = deepcopy(bridge.steps)
    conflicting = deepcopy(first)
    conflicting['events'][-1]['outcome'] = 'failed'
    with pytest.raises(ValueError, match='Conflicting workflow scope'):
        bridge.add(conflicting)
    assert bridge.steps == before
    bridge.add(packet(journal, str(uuid4())))
    assert len(bridge.steps) == 3
    assert sum({'name': 'guard_rejections', 'value': '1'} in step['steps'][0]['parameters']
               for step in bridge.steps) == 1


@pytest.mark.parametrize('order', list(permutations(range(4))))
def test_partial_journal_children_remain_independent_after_legacy_ambiguity(order):
    legacy = trace()
    journal = [event for event in deepcopy(legacy) if event['event'] not in ('attempt', 'guard')]
    invocation_a, invocation_b = str(uuid4()), str(uuid4())
    packets = [packet([journal[0], journal[-1]], invocation_a), packet(journal, invocation_a),
               packet(legacy), packet(journal, invocation_b)]
    originals = deepcopy(packets)
    bridge = _WorkflowStepBridge()
    for index in order:
        bridge.add(packets[index])
    assert len(bridge.steps) == 3
    for entry in bridge.entries:
        guards = [parameter['value'] for step in flatten(entry['step'])
                  for parameter in step['parameters'] if parameter['name'] == 'guard_rejections']
        assert guards == (['1'] if entry['invocation'] is None else [])
    assert packets == originals


def test_legacy_merge_does_not_mutate_retained_partial_journal_projections():
    legacy = trace()
    journal = [event for event in deepcopy(legacy) if event['event'] not in ('attempt', 'guard')]
    invocation = str(uuid4())
    bridge = _WorkflowStepBridge()
    bridge.add(packet([journal[0], journal[-1]], invocation))
    bridge.add(packet(journal, invocation))
    before = deepcopy(bridge._projections)
    bridge.add(packet(legacy))
    assert bridge._projections[:2] == before


def test_legacy_only_repeated_delivery_and_distinct_same_name_graphs():
    bridge = _WorkflowStepBridge()
    events = trace()
    bridge.add(packet(events))
    bridge.add(packet(events))
    bridge.add(packet(trace()))
    assert len(bridge.steps) == 2


@pytest.mark.parametrize('graphs,retry', [(1, False), (2, True)])
def test_actual_driver_dual_publication_records_one_frozen_tree_per_execution(tmp_path, monkeypatch, graphs, retry):
    root = Path(__file__).resolve().parents[2]
    selected = TestIdentity('base::test_graph.py::test_run', 'base', 'test_graph.py', 'test_graph.py::test_run')
    result = TestResult(selected, 'passed', .01)
    context = SimpleNamespace(base_root=root, owner_root=root, pack_root=root / 'demo',
        workflow_entry=root / 'demo/contrived/flow.md.html', workflow_replacements=(), workflow_root='pack')
    packets = []
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'results', 'LOG_CONTENT': False,
        'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}

    def complete(request):
        attempt = json.loads(request.payload.messages[-1].content[0].text)['attempt']
        response = dict(original_input=fixture_driver.USER_INPUT, route='next_a',
            ready=not retry or attempt > 1, final_output=fixture_driver.USER_INPUT)
        return LLMResult(Message('assistant', (TextContent(json.dumps(response)),)), Usage(3, 1))

    def runner(context, tests, **hooks):
        hooks['before_run']()
        event_path = tmp_path / 'events.jsonl'
        monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
        monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(event_path))
        monkeypatch.setenv('PYTEST_CURRENT_TEST', selected.node_id + ' (call)')
        for trial in range(graphs):
            fixture_driver.run_graph(SimpleNamespace(complete=complete), 'synthetic', trial,
                config, root=root, owner_root=root)
        for line in event_path.read_text().splitlines():
            emitted = json.loads(line)
            if emitted['kind'] == 'workflow_evidence':
                packets.append(deepcopy(emitted['data']))
                hooks['on_event'](RunnerEvent('workflow_evidence', 'base', selected,
                    text=json.dumps(emitted['data'])))
        hooks['on_result'](result)
        execution = IntegrationRunResult(0, (result,), '')
        hooks['after_run'](execution)
        return execution

    recorded = run_recorded_integration(context, [selected], config, accepted=True,
        storage=SimpleNamespace(persist=lambda *args: None), runner=runner)
    journals = [item for item in packets if 'projection' in item]
    legacy = [item for item in packets if 'trial' in item]
    assert len(journals) == len(legacy) == graphs
    rendered = json.loads(next(recorded.directory.glob('*-result.json')).read_text())
    assert len(rendered['steps']) == graphs
    scope_ids = [parameter['value'] for graph in rendered['steps'] for step in flatten(graph)
        for parameter in step['parameters'] if parameter['name'] == 'scope_id']
    expected_ids = {event['scope_id'] for item in journals for event in item['events']
        if event['event'] == 'scope_start'}
    assert len(scope_ids) == len(set(scope_ids)) == len(expected_ids)
    assert set(scope_ids) == expected_ids
    assert sum(step['name'].startswith('node ') for graph in rendered['steps']
        for step in flatten(graph)) == graphs * (12 if retry else 6)
    # Both original packet forms remain available; accounting receives journals unchanged.
    attachments = [json.loads((recorded.directory / item['source']).read_text())
        for item in rendered['attachments'] if item['name'] == 'Workflow execution evidence']
    assert attachments == packets
