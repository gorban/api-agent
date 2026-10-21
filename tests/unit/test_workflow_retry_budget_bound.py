"""Admission and retained journals share the supported component retry budget."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from api_agent.workflows import LayoutError, NodeFunction, Workflow
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.evidence import publish
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.workflows.execution import resolve_component_passes
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.journal import projection


@pytest.fixture
def guarded_graph(tmp_path):
    (tmp_path / 'native.py').write_text(
        'def accept(inputs, context):\n'
        '    return {"accepted": True, "feedback": {}}\n')
    (tmp_path / 'input.json').write_text('{"type":"object"}')
    (tmp_path / 'output.json').write_text(json.dumps({
        'type': 'object', 'properties': {
            'accepted': {'type': 'boolean'}, 'feedback': {'type': 'object'}},
        'required': ['accepted', 'feedback']}))
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| bounded |\n\n'
                     '## Flow\n\n1.  [component group](group.md.html)\n')
    component = tmp_path / 'group.md.html'
    component.write_text('# Component: group\n\n## Flow\n\n1.  [node accept](#node-accept)\n\n'
                         '## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n'
                         '| group | 99 | accept |\n\n## Node: accept\n\n'
                         '### Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
                         '| accept | deterministic | [source](native.py) |\n\n'
                         '### Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
                         '| accept | accept | [input](input.json) | [output](output.json) |\n')
    return entry, component


@pytest.mark.parametrize("maximum", ["100", "9" * 5000], ids=["one-over", "oversized"])
def test_budget_above_supported_maximum_is_rejected_at_admission(guarded_graph, maximum):
    entry, component = guarded_graph
    component.write_text(component.read_text().replace('| group | 99 |', '| group | ' + maximum + ' |'))

    with pytest.raises(LayoutError, match='Max passes must be at most 99'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_maximum_budget_records_first_attempt_and_retained_journal_roundtrip(guarded_graph, monkeypatch):
    entry, _ = guarded_graph
    retained = entry.parent / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(retained))
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, {'accept': NodeFunction(native['accept'])}, services={'journal': journal})

    values = asyncio.run(workflow.arun({}))
    publish(journal.finalize())
    payload = json.loads(retained.read_text())['data']

    assert values['accept'] == {'accepted': True, 'feedback': {}}
    assert payload['completeness'] == 'complete'
    assert payload['projection']['component_passes'] == [{'id': 'group', 'max_passes': 99, 'guard': 'accept'}]
    assert [(event['attempt'], event['max_passes']) for event in payload['events']
            if event['event'] == 'attempt'] == [(1, 99)]
    assert validate_journal(payload, expected_definition=definition) == payload
    changed = deepcopy(payload)
    changed['projection']['component_passes'][0]['max_passes'] = 100
    unsigned = {key: value for key, value in changed.items() if key != 'sha256'}
    changed['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(ValueError, match='component pass'):
        validate_journal(changed)


def test_linked_effective_passes_drive_context_and_retained_journal(guarded_graph, monkeypatch):
    entry, component = guarded_graph
    (entry.parent / 'passes.toml').write_text(
        'version = 1\n[fields.WORKFLOW_MAX_PASSES]\ntype = "int"\ndefault = 2\n')
    component.write_text(component.read_text().replace(
        '| group | 99 |', '| group | [WORKFLOW_MAX_PASSES](passes.toml) |'))
    (entry.parent / 'native.py').write_text(
        'def accept(inputs, context):\n'
        '    return {"accepted": context.attempt == context.max_passes, "feedback": {}}\n')
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    config = {'WORKFLOW_MAX_PASSES': 3}
    passes, constants = resolve_component_passes(definition.layout, config)
    journal = WorkflowJournal(definition, component_passes=passes)
    workflow = Workflow(definition, {'accept': NodeFunction(native['accept'])},
                        config=config, services={'journal': journal})

    values = asyncio.run(workflow.arun({}))
    payload = journal.finalize()

    assert values['accept']['accepted'] is True
    assert constants == {'WORKFLOW_MAX_PASSES': 3}
    assert payload['projection']['component_passes'] == [
        {'id': 'group', 'max_passes': 3, 'guard': 'accept'}]
    assert [(event['attempt'], event['max_passes']) for event in payload['events']
            if event['event'] == 'attempt'] == [(1, 3), (2, 3), (3, 3)]
    assert validate_journal(payload, expected_definition=definition,
                            expected_component_passes=passes) == payload
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(payload, expected_definition=definition)
    changed = deepcopy(payload)
    changed['projection']['component_passes'][0]['max_passes'] = 2
    unsigned = {key: value for key, value in changed.items() if key != 'sha256'}
    changed['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(changed, expected_definition=definition,
                         expected_component_passes=passes)


@pytest.mark.parametrize('invalid', [0, True, 2.0, '3', 100])
def test_linked_pass_override_rejected_before_node_runs(guarded_graph, invalid):
    entry, component = guarded_graph
    (entry.parent / 'passes.toml').write_text(
        'version = 1\n[fields.WORKFLOW_MAX_PASSES]\ntype = "int"\ndefault = 2\n')
    component.write_text(component.read_text().replace(
        '| group | 99 |', '| group | [WORKFLOW_MAX_PASSES](passes.toml) |'))
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    native = load_native_catalog(definition, entry.parent / 'native.py')

    with pytest.raises(LayoutError, match='Max passes must be an integer'):
        Workflow(definition, {'accept': NodeFunction(native['accept'])},
                 config={'WORKFLOW_MAX_PASSES': invalid})


def test_malformed_pass_link_is_rejected_at_admission(guarded_graph):
    entry, component = guarded_graph
    component.write_text(component.read_text().replace('| group | 99 |', '| group | [WORKFLOW_MAX_PASSES |'))

    with pytest.raises(LayoutError, match='invalid Max passes link'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_unguarded_component_cannot_gain_retries_from_config(guarded_graph):
    entry, component = guarded_graph
    (entry.parent / 'passes.toml').write_text(
        'version = 1\n[fields.WORKFLOW_MAX_PASSES]\ntype = "int"\ndefault = 1\n')
    component.write_text(component.read_text().replace(
        '| group | 99 | accept |', '| group | [WORKFLOW_MAX_PASSES](passes.toml) | - |'))
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    with pytest.raises(LayoutError, match='unguarded component requires one pass'):
        resolve_component_passes(definition.layout, {'WORKFLOW_MAX_PASSES': 2})


@pytest.mark.parametrize('limits', [{}, {'group': 0}, {'group': 2}])
def test_journal_projection_rejects_missing_invalid_or_changed_literal_limits(guarded_graph, limits):
    entry, _ = guarded_graph
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    with pytest.raises(ValueError, match='component pass limit'):
        projection(definition, component_passes=limits)


def test_runtime_rejects_journal_bound_to_a_different_effective_limit(guarded_graph):
    entry, component = guarded_graph
    (entry.parent / 'passes.toml').write_text(
        'version = 1\n[fields.WORKFLOW_MAX_PASSES]\ntype = "int"\ndefault = 2\n')
    component.write_text(component.read_text().replace(
        '| group | 99 |', '| group | [WORKFLOW_MAX_PASSES](passes.toml) |'))
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, {'accept': NodeFunction(native['accept'])},
                        config={'WORKFLOW_MAX_PASSES': 3}, services={'journal': journal})

    with pytest.raises(WorkflowError, match='different effective component pass limit'):
        asyncio.run(workflow.arun({}))
