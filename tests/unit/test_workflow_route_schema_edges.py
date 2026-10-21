"""Captured declarations exercise exact singleton route types and right operands."""
import asyncio
import json
from pathlib import Path

import pytest

from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.predicates import PredicateError, parse_predicate, predicate_fields


def _definition(root: Path, *, accepted_type='boolean', detail_type='object', when='not .accepted'):
    (root / 'workflows').mkdir()
    (root / 'schemas').mkdir()
    (root / 'native.py').write_text(
        'def inspect(inputs, context):\n'
        '    return {"accepted": False, "detail": {"ready": True}, "score": 2}\n')
    (root / 'schemas/input.json').write_text('{"type":"object","properties":{}}')
    (root / 'schemas/output.json').write_text(json.dumps({
        'type': 'object', 'properties': {
            'accepted': {'type': accepted_type},
            'detail': {'type': detail_type, 'properties': {'ready': {'type': ['boolean']}}},
            'score': {'type': 'integer'}},
        'required': ['accepted', 'detail', 'score']}))
    (root / 'workflows/inspect.md.html').write_text(
        '# Node v2: inspect\n\n## Native\n\n'
        '| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| inspect | deterministic | [source](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| inspect | inspect | [input](../schemas/input.json) | [output](../schemas/output.json) |\n')
    entry = root / 'flow.md.html'
    entry.write_text(
        '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| sample_graph |\n\n'
        '## Flow\n\n1.  [node inspect](workflows/inspect.md.html)\n\n'
        '## Edges\n\n| From | To | When |\n| --- | --- | --- |\n'
        f'| inspect | END | {when} |\n| inspect | END | otherwise |\n')
    return entry


@pytest.mark.parametrize('when', ['.accepted == False', 'not .accepted',
                                  'inspect.detail.ready', '.detail.ready',
                                  '1 < .score', '2 == .score', 'True == .detail.ready'])
def test_captured_singleton_contract_routes_at_runtime(tmp_path, when):
    entry = _definition(tmp_path, accepted_type=['boolean'], detail_type=['object'], when=when)
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    properties = definition.layout.tables['Functions'][0]['Output schema']['properties']
    assert properties['accepted']['type'] == ('boolean',)
    assert properties['detail']['type'] == ('object',)
    namespace = load_native_catalog(definition, tmp_path / 'native.py')
    events = []
    workflow = Workflow(definition, {'inspect': NodeFunction(namespace['inspect'])},
                        services={'observe': events.append})

    values = asyncio.run(workflow.arun({}))

    assert values['inspect'] == {'accepted': False, 'detail': {'ready': True}, 'score': 2}
    assert any(event['event'] == 'edge' and event.get('condition') == when
               for event in events)
    assert definition.contents[tmp_path / 'schemas/output.json'] == (
        tmp_path / 'schemas/output.json').read_bytes()


@pytest.mark.parametrize('accepted_type,detail_type,when', [
    (['boolean', 'null'], 'object', 'not .accepted'),
    (['boolean', 'string'], 'object', '.accepted'),
    (['boolean'], ['object', 'null'], '.detail.ready'),
    ('boolean', ['object', 'array'], 'inspect.detail.ready'),
])
def test_captured_mixed_contract_rejects_shortcut(tmp_path, accepted_type, detail_type, when):
    entry = _definition(tmp_path, accepted_type=accepted_type, detail_type=detail_type,
                        when=when)

    with pytest.raises(LayoutError, match='declared'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_quoted_dot_and_decimal_are_not_rewritten(tmp_path):
    entry = _definition(tmp_path, accepted_type=['boolean'], detail_type=['object'],
                        when='2.0 == .score and inspect["accepted"] == False')
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    fields = predicate_fields(definition.layout, 'inspect')

    assert parse_predicate('inspect["accepted"] == False and ".score" == ".score"',
                           fields).evaluate({'inspect': {'accepted': False}}) is True
    assert parse_predicate('2.0 == .score', fields).evaluate({'inspect': {'score': 2}}) is True


def test_undeclared_right_operand_fails_before_runtime(tmp_path):
    entry = _definition(tmp_path, when='1 < .missing')

    with pytest.raises(LayoutError, match='one declared output'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('when', ['1 < ..score', '1 < .score()',
                                  '1 < .score[0]', '1 < .__class__',
                                  'inspect.unknown.ready'])
def test_malformed_or_unsafe_right_operand_is_rejected(tmp_path, when):
    entry = _definition(tmp_path, when=when)

    with pytest.raises(LayoutError):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
