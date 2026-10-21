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
