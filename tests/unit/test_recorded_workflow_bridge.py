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
        value.update(version=1, projection={}, invocation_id=invocation, turn_id=invocation)
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
        retry = {'name': 'retry_observed', 'value': 'true'}
        assert (retry in entry['step']['parameters']) == (entry['invocation'] is None)
        assert (retry in entry['step']['steps'][0]['parameters']) == (entry['invocation'] is None)
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


@pytest.fixture
def committed_driver_root(tmp_path):
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.provenance import committed_documents
    from test_workflow_provenance import git
    source = Path(__file__).resolve().parents[2]
    definition = load_definition(source / 'demo/contrived/flow.md.html',
                                 base_root=source, pack_root=source / 'demo')
    root = tmp_path / 'workflow-owner'
    root.mkdir()
    copied = []
    for path, body in definition.contents.items():
        if not isinstance(path, Path):
            continue
        relative = path.relative_to(source)
        assert relative.parts[:2] in (('demo', 'contrived'), ('demo', 'cfg'))
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        copied.append(relative.as_posix())
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Example')
    git(root, 'config', 'user.email', 'example@example.invalid')
    git(root, 'add', '--', *sorted(copied))
    git(root, 'commit', '-qm', 'Add synthetic workflow test snapshot')
    captured = load_definition(root / 'demo/contrived/flow.md.html',
                               base_root=root, pack_root=root / 'demo')
    manifest = committed_documents(captured)
    commit = git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
    assert all(owner['commit'] == commit for owner in manifest['owners'])
    assert git(root, 'status', '--porcelain').stdout == b''
    for path, body in captured.contents.items():
        if isinstance(path, Path):
            assert git(root, 'show', 'HEAD:' + path.relative_to(root).as_posix()).stdout == body
    return root


@pytest.mark.parametrize('graphs,retry', [(1, False), (2, True)])
def test_actual_driver_dual_publication_records_one_frozen_tree_per_execution(tmp_path, monkeypatch, committed_driver_root, graphs, retry):
    root = committed_driver_root
    source = Path(__file__).resolve().parents[2]
    selected = TestIdentity('base::test_graph.py::test_run', 'base', 'test_graph.py', 'test_graph.py::test_run')
    result = TestResult(selected, 'passed', .01)
    context = SimpleNamespace(base_root=root, owner_root=root, pack_root=root / 'demo',
        workflow_entry=root / 'demo/contrived/flow.md.html', workflow_replacements=(), workflow_root='pack')
    packets = []
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'results', 'LOG_CONTENT': False,
        'LLM_PROVIDER': str(source / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}

    from test_workflow_trial_decisions import trial_uuid
    original_build = fixture_driver.build
    def controlled_build(*args, **kwargs):
        kwargs['trial'] = trial_uuid('next_a', first_ready=not retry, second_ready=not retry)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(fixture_driver, 'build', controlled_build)

    def complete(request):
        decisions = json.loads(request.payload.messages[-1].content[0].text)['decisions']
        response = dict(original_input=fixture_driver.USER_INPUT, route=decisions['route'],
            ready=decisions['ready'], final_output=fixture_driver.USER_INPUT)
        result = LLMResult(Message('assistant', (TextContent(json.dumps(response)),)), Usage(3, 1))
        from api_agent.testing.inference_usage import publish
        publish(request, result)
        return result

    def runner(context, tests, **hooks):
        hooks['before_run']()
        event_path = tmp_path / 'events.jsonl'
        monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
        monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(event_path))
        monkeypatch.setenv('PYTEST_CURRENT_TEST', selected.node_id + ' (call)')
        import asyncio
        from api_agent.providers.progress import component_timeout
        from api_agent.runtime.tasks import offload
        async def run_graphs():
            async with component_timeout('Workflow case coverage', 30):
                for trial in range(1, graphs + 1):
                    await offload(fixture_driver.run_graph, SimpleNamespace(complete=complete),
                        'synthetic', trial, config, root=root, owner_root=root)
        asyncio.run(run_graphs())
        for line in event_path.read_text().splitlines():
            emitted = json.loads(line)
            if emitted['kind'] == 'workflow_evidence':
                packets.append(deepcopy(emitted['data']))
                hooks['on_event'](RunnerEvent('workflow_evidence', 'base', selected,
                    text=json.dumps(emitted['data'])))
            elif emitted['kind'] == 'inference_usage':
                hooks['on_event'](RunnerEvent('inference_usage', 'base', selected,
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
    assert all(item['completeness'] == 'complete' for item in journals)
    assert recorded.exit_code == 0
    accounting = json.loads((recorded.directory / 'accounting.json').read_text())
    calls = accounting['tests'][0]['calls']
    expected_calls = graphs * (4 if retry else 2)
    assert len(calls) == expected_calls
    assert sum(call['usage']['input_tokens'] for call in calls) == 3 * expected_calls
    assert sum(call['usage']['output_tokens'] for call in calls) == expected_calls
    journal_calls = [call for item in journals for call in item['events'] if call['event'] == 'call']
    assert {call['call_id'] for call in calls} == {call['call_id'] for call in journal_calls}
    rendered = json.loads(next(recorded.directory.glob('*-result.json')).read_text())
    assert len(rendered['steps']) == graphs
    assert [step['name'] for step in rendered['steps']] == ['Trial ' + str(index + 1) for index in range(graphs)]
    assert all(step['steps'][0]['name'] == 'graph GRAPH' for step in rendered['steps'])
    assert rendered['status'] == 'passed'
    assert all(graph['status'] == ('broken' if retry else 'passed') for graph in rendered['steps'])
    assert all(step['status'] == 'passed' for graph in rendered['steps']
               for step in flatten(graph) if step['name'].startswith(('node ', 'call ')))
    scope_ids = [parameter['value'] for graph in rendered['steps'] for step in flatten(graph)
        for parameter in step['parameters'] if parameter['name'] == 'scope_id']
    expected_ids = {event['scope_id'] for item in journals for event in item['events']
        if event['event'] == 'scope_start'}
    assert len(scope_ids) == len(set(scope_ids)) == len(expected_ids)
    assert set(scope_ids) == expected_ids
    assert sum(step['name'].startswith('node ') for graph in rendered['steps']
        for step in flatten(graph)) == graphs * (12 if retry else 6)
    # Durable journals hydrate exactly; accounting still receives original packets.
    assert not any(item['name'] == 'Workflow execution evidence' for item in rendered['attachments'])
    attachments = [json.loads((recorded.directory / item['source']).read_text())
        for wrapper in rendered['steps'] for item in wrapper['steps'][0]['attachments']
        if item['name'] == 'Execution evidence']
    diagnostics = [json.loads((recorded.directory / item['source']).read_text())
        for wrapper in rendered['steps'] for item in wrapper['steps'][0]['attachments']
        if item['name'] == 'Execution diagnostics']
    assert diagnostics == legacy
    for wrapper, evidence in zip(rendered['steps'], attachments):
        invocation = next(item['value'] for item in wrapper['steps'][0]['parameters']
                          if item['name'] == 'invocation_id')
        assert invocation == evidence['invocation_id']
    from api_agent.testing.payloads import hydrate_journal, PayloadResolver
    resolver = PayloadResolver.local(recorded.directory, recorded.manifest)
    resolver.index(next(item['source'] for item in rendered['attachments'] if item['name'] == 'Payload index'))
    restored = [hydrate_journal(item, resolver) if item.get('version') == 2 else item for item in attachments]
    assert restored == journals


@pytest.mark.parametrize('journal_first', [True, False])
def test_attention_enrichment_merges_without_outcome_conflict(journal_first):
    legacy = trace()
    journal = [event for event in deepcopy(legacy) if event['event'] not in ('attempt', 'guard')]
    packets = [packet(journal, str(uuid4())), packet(legacy)]
    bridge = _WorkflowStepBridge()
    for item in packets if journal_first else reversed(packets):
        bridge.add(item)
    assert len(bridge.steps) == 1
    assert bridge.steps[0]['status'] == bridge.steps[0]['steps'][0]['status'] == 'broken'
    assert {'name': 'outcome', 'value': 'passed'} in bridge.steps[0]['parameters']


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_partial_enrichment_bubbles_failure_and_preserves_original_outcome(order):
    from test_workflow_steps import visit_events
    complete = visit_events(trace(), outcome='failed')
    invocation = str(uuid4())
    packets = [packet([complete[0], complete[-1]], invocation),
               packet(complete, invocation), packet(trace())]
    bridge = _WorkflowStepBridge()
    for index in order:
        bridge.add(packets[index])
    enriched = next(step for step in bridge.steps if step['steps'][0]['steps'])
    assert enriched['status'] == 'failed'
    assert {'name': 'outcome', 'value': 'passed'} in enriched['parameters']


def test_successful_call_outcome_conflict_remains_visible():
    from test_workflow_steps import visit_events
    original = visit_events(trace(), call_outcome='completed')
    changed = deepcopy(original)
    next(event for event in changed if event['event'] == 'call')['outcome'] = 'tool_calls'
    bridge = _WorkflowStepBridge()
    bridge.add(packet(original))
    with pytest.raises(ValueError, match='Conflicting workflow scope'):
        bridge.add(packet(changed))


@pytest.mark.parametrize('authoritative', [False, True])
@pytest.mark.parametrize('failed_first', [False, True])
@pytest.mark.parametrize('timing', [(False, False), (True, False), (False, True)])
def test_untimed_call_outcome_conflict_preserves_retained_state(authoritative, failed_first, timing):
    from test_workflow_steps import visit_events
    original = visit_events(trace(), call_outcome='completed', timed=False)
    changed = deepcopy(original)
    next(event for event in changed if event['event'] == 'call')['outcome'] = 'failed'
    calls = [next(event for event in rows if event['event'] == 'call') for rows in (original, changed)]
    node = next(event for event in original if event.get('kind') == 'node')
    for call, timed in zip(calls, timing):
        if timed:
            call.update(started_ms=node['started_ms'], stopped_ms=node['started_ms'] + 1)
    invocation = str(uuid4()) if authoritative else None
    first, second = (changed, original) if failed_first else (original, changed)
    bridge = _WorkflowStepBridge()
    bridge.add(packet(first, invocation))
    retained_steps = deepcopy(bridge.steps)
    retained_projections = deepcopy(bridge._projections)
    with pytest.raises(ValueError, match='Conflicting workflow scope'):
        bridge.add(packet(second, invocation))
    assert bridge.steps == retained_steps
    assert bridge._projections == retained_projections
    bridge.add(packet(first, invocation))
    assert bridge.steps == retained_steps


@pytest.mark.parametrize('authoritative', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('timing', [(False, False), (True, False), (False, True)])
def test_call_cannot_move_between_visits_with_missing_timing(authoritative, reverse, timing):
    from test_workflow_steps import visit_events
    original = visit_events(visit_events(trace(), call_outcome='completed', timed=False))
    nodes = [event for event in original if event['event'] == 'scope_start' and event['kind'] == 'node']
    changed = deepcopy(original)
    call = next(event for event in changed if event['event'] == 'call')
    call['visit_id'] = nodes[0]['scope_id']
    for rows, timed in zip((original, changed), timing):
        if timed:
            next(event for event in rows if event['event'] == 'call').update(
                started_ms=nodes[0]['started_ms'], stopped_ms=nodes[0]['started_ms'] + 1)
    packets = [packet(rows, 'same-invocation' if authoritative else None) for rows in (original, changed)]
    bridge = _WorkflowStepBridge()
    first, second = reversed(packets) if reverse else packets
    bridge.add(first)
    retained = deepcopy((bridge.steps, bridge._projections))
    with pytest.raises(ValueError, match='Conflicting workflow scope'):
        bridge.add(second)
    assert (bridge.steps, bridge._projections) == retained


@pytest.mark.parametrize('journal_timed', [False, True])
@pytest.mark.parametrize('journal_first', [False, True])
def test_call_timing_enrichment_keeps_compatible_outcome_and_owner(journal_timed, journal_first):
    from test_workflow_steps import visit_events
    legacy = visit_events(trace(), call_outcome='completed', timed=False)
    journal = deepcopy(legacy)
    timed = journal if journal_timed else legacy
    node = next(event for event in timed if event.get('kind') == 'node')
    next(event for event in timed if event['event'] == 'call').update(
        started_ms=node['started_ms'], stopped_ms=node['started_ms'] + 1)
    packets = [packet(journal, str(uuid4())), packet(legacy)]
    bridge = _WorkflowStepBridge()
    for item in packets if journal_first else reversed(packets):
        bridge.add(item)
    calls = [step for step in flatten(bridge.steps[0]) if step['name'].startswith('call ')]
    assert len(bridge.steps) == len(calls) == 1
    assert calls[0]['status'] == 'passed'
    assert {'name': 'outcome', 'value': 'completed'} in calls[0]['parameters']


@pytest.mark.parametrize('journal_first', [False, True])
def test_richer_legacy_retry_remains_explained_with_authoritative_first_attempt(journal_first):
    from test_workflow_steps import visit_events
    legacy = visit_events([event for event in trace() if event['event'] != 'guard'])
    journal = [event for event in deepcopy(legacy)
               if not (event['event'] == 'attempt' and event['attempt'] > 1)]
    packets = [packet(journal, str(uuid4())), packet(legacy)]
    bridge = _WorkflowStepBridge()
    for item in packets if journal_first else reversed(packets):
        bridge.add(item)
    graph = bridge.steps[0]
    component = graph['steps'][0]
    assert graph['status'] == component['status'] == 'broken'
    assert {'name': 'attempts_reached', 'value': '1'} in component['parameters']
    for step in (graph, component):
        assert {'name': 'retry_observed', 'value': 'true'} in step['parameters']
        assert {'name': 'outcome', 'value': 'passed'} in step['parameters']
    assert not any(item['name'] == 'retry_observed' for item in component['steps'][0]['parameters'])


def test_actual_driver_rejects_dirty_fixture_before_inference(committed_driver_root, monkeypatch):
    from api_agent.workflows.layout import LayoutError
    root = committed_driver_root
    source = Path(__file__).resolve().parents[2]
    node = root / 'demo/contrived/workflows/components/first.md.html'
    node.write_text(node.read_text() + '\n')
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    calls = []

    def complete(request):
        calls.append(request)
        raise AssertionError('Inference reached a dirty workflow fixture')

    config = {'LLM_PROVIDER': str(source / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}
    with pytest.raises(LayoutError, match='differs from HEAD'):
        fixture_driver.run_graph(SimpleNamespace(complete=complete), 'synthetic', 1,
                                 config, root=root, owner_root=root)
    assert calls == []


@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('journal_first', [False, True])
def test_recording_joins_untimed_transport_call_and_freezes_allure(tmp_path, terminal, journal_first):
    from api_agent.workflows.journal import WorkflowJournal
    from test_transport_reporting import ledger

    selected = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    result = TestResult(selected, 'failed' if terminal else 'passed', .01)
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[SimpleNamespace(id='N', kind='node', parent=None)], tables={'Edges': []}),
        resources=[], replacements=[])
    journal = WorkflowJournal(definition, test_id=selected.node_id)
    graph, node, call = (str(uuid4()) for _ in range(3))
    graph_scope = dict(scope_id=graph, parent_id=None, kind='graph', owner='GRAPH', started_ms=1)
    node_scope = dict(scope_id=node, parent_id=graph, kind='node', owner='N', started_ms=1)
    journal.observe(dict(event='scope_start', **graph_scope))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='N', implicit=True))
    journal.observe(dict(event='scope_start', **node_scope))
    attempts = ledger()
    if terminal:
        attempts['attempts'][-1].update(outcome='failed', execution='possibly_executed', status=503, error_kind='server')
    journal.record_call(visit_id=node, call_id=call, outcome='failed' if terminal else 'completed', transport_attempts=attempts)
    journal.observe(dict(event='node', scope_id=node, node='N'))
    journal.observe(dict(event='scope_end', stopped_ms=3, outcome='passed', **node_scope))
    journal.observe(dict(event='edge', owner='GRAPH', source='N', target='END', implicit=True, visit_id=node))
    journal.observe(dict(event='scope_end', stopped_ms=4, outcome='passed', **graph_scope))
    canonical = journal.finalize()
    legacy = {'events': [dict(event='scope_start', **graph_scope), dict(event='scope_start', **node_scope),
                         dict(event='scope_end', stopped_ms=3, outcome='passed', **node_scope),
                         dict(event='scope_end', stopped_ms=4, outcome='passed', **graph_scope)]}
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for payload in ([canonical, legacy] if journal_first else [legacy, canonical]):
            hooks['on_event'](RunnerEvent('workflow_evidence', 'base', selected, text=json.dumps(payload)))
        hooks['on_result'](result)
        execution = IntegrationRunResult(1 if terminal else 0, (result,), '')
        hooks['after_run'](execution)
        return execution
    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')
    recorded = run_recorded_integration(context, [selected],
        dict(TUI_MODE='integration', ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results'),
        accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    rendered = json.loads(next(recorded.directory.glob('*-result.json')).read_text())
    calls = [step for root in rendered['steps'] for step in flatten(root) if step['name'] == 'call ' + call]
    assert len(calls) == 1
    assert 'start' not in calls[0] and 'stop' not in calls[0]
    assert calls[0]['status'] == ('failed' if terminal else 'broken')
    assert [step['status'] for step in calls[0]['steps']] == ['broken', 'failed' if terminal else 'passed']
    assert recorded.persisted
