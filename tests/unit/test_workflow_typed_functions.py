"""Typed (State-declared) nodes reuse the Functions schema contract of the binding grammar."""
import asyncio
import json

import pytest

from api_agent.providers.llm import Message, TextContent
from api_agent.workflows.artifact_compile import ArtifactCompileError, compile_definition
from api_agent.workflows.definition import load_definition
from api_agent.workflows.execution import CandidateCorrection, Workflow, WorkflowError
from api_agent.workflows.identity import declaration_identity
from api_agent.workflows.model import LayoutError
from api_agent.workflows.native import load_native_bindings
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source

NATIVE = '''from typing import TypedDict

from api_agent.providers.llm import Message, TextContent


class State(TypedDict):
    topic: str
    count: int
    answer: dict


SEEN = []


def intake(state, context):
    return {'count': state.get('count', 0) + 1}


def decide(state, context):
    SEEN.append(context.response_request((Message('user', (TextContent('Decide.'),)),), strict=True))
    SEEN.append(type(context.services).__name__)
    return {'answer': 'invented' if state['topic'] == 'bad' else {'ok': True}}
'''
SCHEMAS = {
    'intake_input.json': {'type': 'object', 'properties': {'topic': {'type': 'string'}}, 'required': ['topic']},
    'intake_output.json': {'type': 'object', 'properties': {'count': {'type': 'integer'}}, 'required': ['count']},
    'decide_input.json': {'type': 'object', 'properties': {'topic': {'type': 'string'}}},
    'decide_output.json': {'type': 'object', 'properties': {'answer': {'type': 'object'}}, 'required': ['answer']},
    'decide_response.json': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                             'required': ['ok'], 'additionalProperties': False},
}
PLAIN = '''| ID | Registry | Input | Output |
| --- | --- | --- | --- |
| intake | intake | [input](intake_input.json) | [output](intake_output.json) |
'''
RESPONSE = '''| ID | Registry | Input | Output | Response schema | Output mode | Fallback |
| --- | --- | --- | --- | --- | --- | --- |
| decide | decide | [input](decide_input.json) | [output](decide_output.json) | [response](decide_response.json) | structured | reject |
'''
SUMMARY = '''| ID | Registry | Input | Output | Response schema | Output mode | Fallback | Display summary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| decide | decide | [input](decide_input.json) | [output](decide_output.json) | [response](decide_response.json) | structured | reject | native |
'''
INTAKE = '''# Node: intake

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| intake | deterministic | [Python](native.py) |

## Functions

''' + PLAIN
DECIDE = '''# Node: decide

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| decide | llm | [Python](native.py) |

## Functions

''' + RESPONSE
FLOW = '''# Workflow

## Graph ID

| ID |
| --- |
| typed_functions |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

1.  [node intake](intake.md.html)
2.  [node decide](decide.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
| intake | decide | - | - |
| decide | END | - | - |
'''


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def build(root, *, intake=INTAKE, decide=DECIDE, flow=FLOW, schemas=None):
    write(root, 'native.py', NATIVE)
    for name, schema in {**SCHEMAS, **(schemas or {})}.items():
        write(root, name, schema if isinstance(schema, str) else json.dumps(schema))
    write(root, 'intake.md.html', intake)
    write(root, 'decide.md.html', decide)
    return write(root, 'flow.md.html', flow)


def admit(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def workflow(definition, **options):
    return Workflow(definition, catalog=load_native_bindings(definition), reducers={}, **options)


def run(definition, state, **options):
    return asyncio.run(workflow(definition, **options).arun(state, thread_id='typed'))


def plain(value):
    return json.loads(json.dumps(value, default=dict))


def test_typed_functions_are_captured_with_schema_resources(tmp_path):
    definition = admit(build(tmp_path))
    rows = {row['ID']: row for row in definition.functions}
    assert set(rows) == {'intake', 'decide'}
    assert rows['intake']['Kind'] == 'deterministic' and 'Output mode' not in rows['intake']
    assert rows['decide']['Kind'] == 'llm' and rows['decide']['Output mode'] == 'structured'
    assert plain(rows['decide']['Response schema value']) == SCHEMAS['decide_response.json']
    assert plain(rows['intake']['Input schema']) == SCHEMAS['intake_input.json']
    schemas = {item.logical_path for item in definition.resources if item.kind == 'schema'}
    assert schemas == {'base/' + name for name in SCHEMAS}
    assert definition.contents[tmp_path.resolve() / 'decide_response.json'] == json.dumps(
        SCHEMAS['decide_response.json']).encode()


def test_graphs_without_functions_are_unchanged(tmp_path):
    entry = build(tmp_path, intake=INTAKE.split('## Functions')[0], decide=DECIDE.split('## Functions')[0])
    write(tmp_path, 'native.py', NATIVE.replace('    SEEN.append(context.response_request((Message(\'user\', (TextContent(\'Decide.\'),)),), strict=True))\n    SEEN.append(type(context.services).__name__)\n', ''))
    definition = admit(entry)
    assert definition.functions == ()
    assert run(definition, {'topic': 'good'}) == {'topic': 'good', 'count': 1, 'answer': {'ok': True}}


def test_display_summary_variant_wraps_node_services(tmp_path):
    definition = admit(build(tmp_path, decide=DECIDE.replace(RESPONSE, SUMMARY)))
    flow = workflow(definition)
    asyncio.run(flow.arun({'topic': 'good'}, thread_id='summary'))
    seen = next(space for space in flow.catalog.namespaces.values() if 'SEEN' in space)['SEEN']
    assert seen[1] == 'SummaryServices'
    assert next(row for row in definition.functions if row['ID'] == 'decide')['Display summary'] == 'native'


def test_inline_node_declaration_may_declare_functions(tmp_path):
    entry = build(tmp_path)
    write(tmp_path, 'group.md.html', '# Component: group\n\n## Flow\n\n1.  [node intake](#node-intake)\n'
          '\n## Node: intake\n\n### Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
          '| intake | deterministic | [Python](native.py) |\n\n### Functions\n\n' + PLAIN)
    entry.write_text(FLOW.replace('1.  [node intake](intake.md.html)', '1.  [component group](group.md.html)').replace(
        '| intake | decide | - | - |', '| group | decide | - | - |\n| intake | END | - | - |'))
    definition = admit(entry)
    assert [row['ID'] for row in definition.functions] == ['intake', 'decide']
    owner = next(row for row in definition.functions if row['ID'] == 'intake')['_owner']
    assert owner == tmp_path.resolve() / 'group.md.html'
    assert run(definition, {'topic': 'good'})['count'] == 1


@pytest.mark.parametrize('mutation,message', [
    (lambda text: text.replace('| intake | intake | [input]', '| other | intake | [input]'), 'Functions'),
    (lambda text: text.replace('| intake | intake | [input]', '| intake | decide | [input]'), 'Functions'),
    (lambda text: text + PLAIN.split('\n', 2)[2], 'Functions'),
    (lambda text: text.replace('(intake_output.json)', '(missing.json)'), 'missing'),
    (lambda text: text.replace('## Functions\n\n' + PLAIN, '## Functions\n\n| ID | Registry | Input |\n| --- | --- | --- |\n'), 'column'),
    (lambda text: text.replace(PLAIN, ''), 'Functions'),
])
def test_deterministic_function_declaration_rejections(tmp_path, mutation, message):
    entry = build(tmp_path, intake=mutation(INTAKE))
    with pytest.raises((LayoutError, OSError), match=message):
        admit(entry)


@pytest.mark.parametrize('mutation', [
    lambda text: text.replace('structured | reject', '- | -').replace('[response](decide_response.json)', '-'),
    lambda text: text.replace('| structured |', '| nonsense |'),
    lambda text: text.replace('| reject |', '| maybe |'),
    lambda text: text.replace('[response](decide_response.json)', '-'),
    lambda text: text.replace('| llm |', '| deterministic |'),
])
def test_llm_function_declaration_rejections(tmp_path, mutation):
    with pytest.raises(LayoutError):
        admit(build(tmp_path, decide=mutation(DECIDE)))


def test_four_column_functions_admit_any_native_kind(tmp_path):
    decided = DECIDE.replace(RESPONSE, PLAIN.replace('intake', 'decide'))
    definition = admit(build(tmp_path, decide=decided))
    assert [row['Kind'] for row in definition.functions] == ['deterministic', 'llm']


def test_deterministic_node_with_response_columns_is_rejected(tmp_path):
    decided = DECIDE.replace('| llm |', '| deterministic |')
    with pytest.raises(LayoutError, match='response declaration'):
        admit(build(tmp_path, decide=decided))


@pytest.mark.parametrize('schema', [
    {'type': 'array'}, {'type': 'object', 'properties': {'x': {'$ref': '#/$defs/y'}}},
    {'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object'}, 'not json'])
def test_invalid_schemas_are_rejected_by_the_shared_validator(tmp_path, schema):
    with pytest.raises(LayoutError):
        admit(build(tmp_path, schemas={'intake_input.json': schema}))


def test_schema_link_cannot_escape_or_change_resource_kind(tmp_path):
    with pytest.raises(LayoutError, match='source outside'):
        admit(build(tmp_path, intake=INTAKE.replace('(intake_input.json)', '(../outside.json)')))
    flow = FLOW.replace('## Edges', '## Resources\n\n| ID | Kind | Source |\n| --- | --- | --- |\n'
                        '| shared | data | [data](intake_input.json) |\n\n## Edges')
    with pytest.raises(LayoutError, match='conflicting'):
        admit(build(tmp_path, flow=flow))
    schema_flow = FLOW.replace('## Edges', '## Resources\n\n| ID | Kind | Source |\n| --- | --- | --- |\n'
                               '| shared | schema | [data](intake_input.json) |\n\n## Edges')
    assert admit(build(tmp_path, flow=schema_flow)).declared_resources['shared'].kind == 'schema'


def test_input_schema_validates_the_state_projection_the_node_reads(tmp_path):
    definition = admit(build(tmp_path))
    result = run(definition, {'topic': 'good', 'count': 4, 'answer': {'extra': True}})
    assert result['count'] == 5
    with pytest.raises(WorkflowError, match='intake returned invalid input structure'):
        run(definition, {'count': 4})


def test_output_schema_validates_the_partial_update_for_deterministic_nodes(tmp_path):
    native = NATIVE.replace("return {'count': state.get('count', 0) + 1}", "return {'count': 'many'}")
    entry = build(tmp_path)
    write(tmp_path, 'native.py', native)
    with pytest.raises(WorkflowError, match='intake returned invalid output structure'):
        run(admit(entry), {'topic': 'good'})


def test_state_annotation_key_check_remains(tmp_path):
    entry = build(tmp_path)
    write(tmp_path, 'native.py', NATIVE.replace("return {'count': state.get('count', 0) + 1}", "return {'other': 1}"))
    with pytest.raises(ValueError, match='partial state update'):
        run(admit(entry), {'topic': 'good'})


def test_non_json_projection_is_rejected(tmp_path):
    entry = build(tmp_path)
    write(tmp_path, 'native.py', NATIVE.replace("return {'count': state.get('count', 0) + 1}", "return {'count': 1}"))
    schema = {'type': 'object', 'properties': {'topic': {}}}
    write(tmp_path, 'intake_input.json', json.dumps(schema))
    with pytest.raises(WorkflowError, match='non-JSON input'):
        run(admit(entry), {'topic': {1, 2}})


def test_llm_node_builds_the_declared_response_request(tmp_path):
    definition = admit(build(tmp_path))
    flow = workflow(definition)
    asyncio.run(flow.arun({'topic': 'good'}, thread_id='response'))
    seen = next(space for space in flow.catalog.namespaces.values() if 'SEEN' in space)['SEEN']
    request = seen[0]
    assert (request.response.mode, request.response.fallback) == ('structured', 'reject')
    assert json.loads(request.response.schema.schema) == SCHEMAS['decide_response.json']
    assert request.response.schema.name == 'decide'
    assert seen[1] != 'SummaryServices'


def test_typed_node_without_functions_has_no_response_contract(tmp_path):
    entry = build(tmp_path, decide=DECIDE.split('## Functions')[0])
    with pytest.raises(WorkflowError, match='no declared generated-response contract'):
        run(admit(entry), {'topic': 'good'})


def test_llm_output_schema_failure_is_a_candidate_correction_without_a_guard(tmp_path):
    definition = admit(build(tmp_path))
    with pytest.raises(CandidateCorrection) as caught:
        run(definition, {'topic': 'bad'})
    assert caught.value.feedback['reason'] == 'output_schema'
    assert caught.value.rejected == {'answer': 'invented'}


def test_deterministic_output_failure_is_not_a_candidate_correction(tmp_path):
    entry = build(tmp_path)
    write(tmp_path, 'native.py', NATIVE.replace("return {'count': state.get('count', 0) + 1}", "return {'count': 'x'}"))
    with pytest.raises(WorkflowError) as caught:
        run(admit(entry), {'topic': 'good'})
    assert not isinstance(caught.value, CandidateCorrection)


def test_non_json_llm_output_is_a_plain_failure(tmp_path):
    entry = build(tmp_path)
    write(tmp_path, 'native.py', NATIVE.replace("{'ok': True}", "{'ok': {1}}").replace(
        "'invented' if state['topic'] == 'bad' else ", ''))
    write(tmp_path, 'decide_output.json', json.dumps({'type': 'object'}))
    with pytest.raises(WorkflowError, match='non-JSON output'):
        run(admit(entry), {'topic': 'good'})


def test_function_properties_must_name_state_fields(tmp_path):
    entry = build(tmp_path, schemas={'intake_input.json': {
        'type': 'object', 'properties': {'typo': {'type': 'string'}}}})
    with pytest.raises(LayoutError, match='typo'):
        workflow(admit(entry))
    entry = build(tmp_path, schemas={'intake_output.json': {
        'type': 'object', 'properties': {'typo': {'type': 'string'}}}})
    with pytest.raises(LayoutError, match='typo'):
        workflow(admit(entry))


def test_viewer_projects_typed_function_references(tmp_path):
    definition = admit(build(tmp_path))
    projection = project_view(definition)
    refs = [ref for ref in projection.references if ref.kind == 'functions']
    assert len(refs) == 5
    fields = sorted((ref.owner_id, dict(ref.attributes)['field']) for ref in refs)
    assert fields == [('decide', 'Input'), ('decide', 'Output'), ('decide', 'Response schema'),
                      ('intake', 'Input'), ('intake', 'Output')]
    response = next(ref for ref in refs if dict(ref.attributes)['field'] == 'Response schema')
    assert response.target_path == 'base/decide_response.json'
    assert response.owner_path == 'base/decide.md.html'
    assert dict(response.attributes)['Output mode'] == 'structured'
    expected = {'base/intake.md.html': (b'Input: intake&#95;input&#46;json', b'Output: intake&#95;output&#46;json'),
                'base/decide.md.html': (b'Input: decide&#95;input&#46;json', b'Output: decide&#95;output&#46;json',
                                        b'Response schema: decide&#95;response&#46;json')}
    for path, labels in expected.items():
        document = next(item for item in projection.documents if item.logical_path == path)
        source, links = _projection_source(projection, document)
        assert all(label in source for label in labels)
        assert len([link for link in links if link.endswith('.json')]) == len(labels)


def test_typed_reference_attributes_match_the_binding_grammar(tmp_path):
    typed = project_view(admit(build(tmp_path)))
    row = next(ref for ref in typed.references if ref.kind == 'functions'
               and ref.owner_id == 'decide' and dict(ref.attributes)['field'] == 'Input')
    assert dict(row.attributes) == {
        'ID': 'decide', 'Registry': 'decide', 'Output mode': 'structured', 'Fallback': 'reject',
        'Kind': 'llm', '_owner_id': 'decide', 'field': 'Input'}


def test_identity_includes_schema_bytes_and_response_columns(tmp_path):
    entry = build(tmp_path)
    original = declaration_identity(admit(entry))
    assert declaration_identity(admit(entry)) == original
    write(tmp_path, 'decide_response.json', json.dumps({**SCHEMAS['decide_response.json'], 'description': 'changed'}))
    changed = declaration_identity(admit(entry))
    assert changed != original
    write(tmp_path, 'decide_response.json', json.dumps(SCHEMAS['decide_response.json']))
    assert declaration_identity(admit(entry)) == original
    write(tmp_path, 'decide.md.html', DECIDE.replace('| reject |', '| allow |'))
    assert declaration_identity(admit(entry)) not in (original, changed)
    write(tmp_path, 'decide.md.html', DECIDE.replace('| structured |', '| json |'))
    assert declaration_identity(admit(entry)) != original


def test_identity_of_graphs_without_functions_has_no_functions_entry(tmp_path):
    from api_agent.workflows.identity import _Snapshot
    entry = build(tmp_path, intake=INTAKE.split('## Functions')[0], decide=DECIDE.split('## Functions')[0])
    assert 'functions' not in _Snapshot(admit(entry)).declaration()
    assert 'functions' in _Snapshot(admit(build(tmp_path))).declaration()


def test_compiled_plans_reject_typed_functions(tmp_path):
    with pytest.raises(ArtifactCompileError, match='typed-node Functions'):
        compile_definition(admit(build(tmp_path)), [])
