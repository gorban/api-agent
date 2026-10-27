"""Generated-response declarations stay separate from complete native outputs."""
import asyncio
import json
import shutil

import pytest

from api_agent.providers.llm import GenerationOptions, Message, TextContent
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeContext, NodeFunction, Workflow, WorkflowError
from api_agent.workflows.layout import LayoutError, load_layout
from test_workflow_definition import make_graph


RESPONSE = {'type': 'object', 'properties': {'answer': {'type': 'boolean'}},
            'required': ['answer'], 'additionalProperties': False}
HEADER = '| ID | Registry | Input | Output | Response schema | Output mode | Fallback |'


def response_graph(root, *, mode='structured', fallback='reject', response=RESPONSE, kind='llm'):
    entry = make_graph(root)
    (root / 'schemas/response.json').write_text(json.dumps(response))
    node = root / 'workflows/inspect.md.html'
    source = node.read_text().replace('| inspect | deterministic |', f'| inspect | {kind} |')
    source = source.replace('| ID | Registry | Input | Output |', HEADER)
    source = source.replace('| --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- |')
    source = source.replace('[output](../schemas/output.json) |',
                            f'[output](../schemas/output.json) | [response](../schemas/response.json) | {mode} | {fallback} |')
    node.write_text(source)
    return entry


def admit(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def context_for(definition):
    return NodeContext('inspect', (), (), {}, response_contract=definition.layout.tables['Functions'][0])


def test_generated_contract_is_captured_and_bound_separately_from_complete_output(tmp_path):
    definition = admit(response_graph(tmp_path))
    context = context_for(definition)
    generation = GenerationOptions(max_output_tokens=100)

    request = context.response_request((Message('user', (TextContent('Decide.'),)),), generation=generation)

    assert request.structured is None
    assert request.generation == generation
    assert request.response.mode == 'structured'
    assert request.response.fallback == 'reject'
    assert json.loads(request.response.schema.schema) == RESPONSE
    assert 'seen' not in request.response.schema.schema
    assert definition.contents[tmp_path / 'schemas/response.json'] == json.dumps(RESPONSE).encode()
    assert definition.layout.tables['Functions'][0]['Output schema']['properties']['seen']['type'] == 'boolean'


def test_response_resource_identity_survives_relocation_and_changes_with_schema(tmp_path):
    first = tmp_path / 'one'
    first.mkdir()
    entry = response_graph(first)
    original = admit(entry)
    second = tmp_path / 'two'
    shutil.copytree(first, second)
    relocated = admit(second / 'flow.md.html')
    assert original.definition_id == relocated.definition_id
    (second / 'schemas/response.json').write_text(json.dumps({**RESPONSE, 'description': 'Another contract'}))
    assert admit(second / 'flow.md.html').definition_id != original.definition_id


def test_viewer_has_response_schema_reference(tmp_path):
    from api_agent.workflows.viewer_projection import project_view
    definition = admit(response_graph(tmp_path))
    projection = project_view(definition)
    references = [row for row in projection.references if dict(row.attributes).get('field') == 'Response schema']
    assert len(references) == 1
    assert references[0].target_sha256
    assert references[0].target_path == 'base/schemas/response.json'


@pytest.mark.parametrize('mode,fallback', [('structured', 'allow'), ('json', 'reject'), ('prompted_json', 'allow')])
def test_json_modes_keep_explicit_policy(tmp_path, mode, fallback):
    policy = context_for(admit(response_graph(tmp_path, mode=mode, fallback=fallback))).response_request(
        (Message('user', (TextContent('Decide'),)),)).response
    assert (policy.mode, policy.fallback) == (mode, fallback)


def test_optional_composed_response_preserves_explicit_non_strict_semantics(tmp_path):
    schema = {'allOf': [{'type': 'object', 'properties': {'optional': {'type': 'string'}}}]}
    context = context_for(admit(response_graph(tmp_path, response=schema)))
    policy = context.response_request((Message('user', (TextContent('Decide'),)),), strict=False).response
    assert policy.schema.strict is False
    assert json.loads(policy.schema.schema) == schema


@pytest.mark.parametrize('mode,fallback,response', [('unknown', 'reject', 'link'), ('structured', 'yes', 'link'),
    ('text', 'reject', 'link'), ('structured', 'reject', '-'), ('-', 'reject', '-'), ('-', '-', 'link')])
def test_bad_response_cells_reject_before_native_execution(tmp_path, mode, fallback, response):
    entry = response_graph(tmp_path, mode=mode, fallback=fallback)
    node = tmp_path / 'workflows/inspect.md.html'
    if response == '-':
        node.write_text(node.read_text().replace('[response](../schemas/response.json)', '-'))
    with pytest.raises(LayoutError):
        admit(entry)


@pytest.mark.parametrize('bad_header', [HEADER.replace('Response schema | ', ''), HEADER.replace('Fallback', 'fallback'),
                                       HEADER.replace('Output mode | Fallback', 'Fallback | Output mode')])
def test_partial_or_misplaced_columns_are_not_a_new_grammar(tmp_path, bad_header):
    entry = response_graph(tmp_path)
    node = tmp_path / 'workflows/inspect.md.html'
    node.write_text(node.read_text().replace(HEADER, bad_header))
    with pytest.raises(LayoutError):
        admit(entry)


def test_text_has_no_schema_and_deterministic_rows_use_three_dashes(tmp_path):
    entry = response_graph(tmp_path, mode='text')
    node = tmp_path / 'workflows/inspect.md.html'
    node.write_text(node.read_text().replace('[response](../schemas/response.json)', '-'))
    from api_agent.workflows.viewer_projection import project_view
    definition = admit(entry)
    assert not [row for row in project_view(definition).references if dict(row.attributes).get('field') == 'Response schema']
    context = context_for(definition)
    assert context.response_request((Message('user', (TextContent('Hi'),)),)).response.schema is None
    with pytest.raises(WorkflowError, match='strictness'):
        context.response_request((Message('user', (TextContent('Hi'),)),), strict=False)
    node.write_text(node.read_text().replace('| inspect | llm |', '| inspect | deterministic |').replace('| text | reject |', '| - | - |'))
    definition = admit(entry)
    with pytest.raises(WorkflowError, match='no declared'):
        context_for(definition).response_request(())


@pytest.mark.parametrize('row', [None, {}])
def test_legacy_context_does_not_implicitly_enable_policy(row):
    with pytest.raises(WorkflowError, match='no declared'):
        NodeContext('inspect', (), (), {}, response_contract=row).response_request(())


def test_helper_rejects_non_boolean_strictness(tmp_path):
    context = context_for(admit(response_graph(tmp_path)))
    with pytest.raises(WorkflowError, match='strictness'):
        context.response_request((), strict='false')


@pytest.mark.parametrize('kind', ['deterministic', 'llm'])
def test_native_kind_response_pair_rejects_during_definition_admission(tmp_path, kind):
    entry = response_graph(tmp_path, kind=kind)
    node = tmp_path / 'workflows/inspect.md.html'
    if kind == 'llm':
        node.write_text(node.read_text().replace(
            '[response](../schemas/response.json) | structured | reject |', '- | - | - |'))
    (tmp_path / 'native.py').write_text('raise AssertionError("Native source imported during admission")\n')
    with pytest.raises(LayoutError, match='response declaration.*native node kind'):
        admit(entry)


@pytest.mark.parametrize('kind', ['deterministic', 'llm'])
def test_four_column_native_kind_still_admits_without_importing_source(tmp_path, kind):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows/inspect.md.html'
    node.write_text(node.read_text().replace('| inspect | deterministic |', f'| inspect | {kind} |'))
    (tmp_path / 'native.py').write_text('raise AssertionError("Native source imported during admission")\n')
    assert admit(entry).layout.tables['Functions'][0]['Kind'] == kind


def test_multiple_explicit_calls_keep_one_schema_and_runtime_assembles_metadata(tmp_path):
    entry = response_graph(tmp_path)
    (tmp_path / 'native.py').write_text('''from api_agent.providers.llm import Message, TextContent
async def inspect(inputs, context):
    first = context.response_request((Message('user', (TextContent('first'),)),))
    second = context.response_request((Message('user', (TextContent('second'),)),))
    context.services['calls'].extend((first, second))
    return {'seen': True, 'usage': {'input_tokens': 2}}
''')
    definition = admit(entry)
    catalog = load_native_catalog(definition, tmp_path / 'native.py')
    calls = []
    graph = Workflow(definition, {'inspect': NodeFunction(catalog['inspect'], 'llm')}, services={'calls': calls})
    result = asyncio.run(graph.arun({}))
    assert result['inspect'] == {'seen': True, 'usage': {'input_tokens': 2}}
    assert len(calls) == 2
    assert calls[0].response == calls[1].response
    assert 'usage' not in calls[0].response.schema.schema


def test_complete_output_still_rejects_invalid_runtime_value(tmp_path):
    entry = response_graph(tmp_path)
    (tmp_path / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": "invented"}\n')
    definition = admit(entry)
    catalog = load_native_catalog(definition, tmp_path / 'native.py')
    graph = Workflow(definition, {'inspect': NodeFunction(catalog['inspect'], 'llm')})
    with pytest.raises(WorkflowError, match='output'):
        asyncio.run(graph.arun({}))


def test_legacy_layout_accepts_extended_columns_and_captures_response_link(tmp_path):
    from test_workflow_embedded_dialects import declaration
    entry = declaration(tmp_path, {'type': 'object'}, {})
    (tmp_path / 'response.json').write_text(json.dumps(RESPONSE))
    source = entry.read_text().replace('| ID | Registry | Input | Output |', HEADER)
    source = source.replace('| --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- |')
    source = source.replace('[output](output.json) |', '[output](output.json) | [response](response.json) | structured | reject |')
    entry.write_text(source)
    layout = load_layout(entry, base_root=tmp_path, pack_root=tmp_path)
    assert tmp_path / 'response.json' in layout.files
    assert layout.tables['Functions'][0]['Response schema value'] == RESPONSE


@pytest.mark.parametrize('schema', [{'type': 'object', '$schema': 'https://example.invalid/dialect'},
                                   {'$ref': 'https://example.invalid/schema'}, {'type': 'invalid'}])
def test_generated_schema_rejection_is_independent_of_complete_output(tmp_path, schema):
    with pytest.raises(LayoutError):
        admit(response_graph(tmp_path, response=schema))


def test_bundled_planner_and_reviewer_use_distinct_generated_contracts():
    from test_demo_application import LLM, Owner, application, request
    from api_agent.providers.control import OperationContext
    llm = LLM(['{"use_tool":false,"arguments":{},"answer":"Hello"}',
               '{"correct":true,"grounded":true,"relevant":true,"preference":true}', 'Hello'])
    asyncio.run(application(Owner()).complete(request(), llm=llm, context=OperationContext('turn')))
    planner, reviewer, renderer = [item.payload for item in llm.requests]
    assert (planner.response.mode, planner.response.fallback) == ('structured', 'allow')
    assert json.loads(planner.response.schema.schema)['properties']['arguments'] == {'type': 'object'}
    assert (reviewer.response.mode, reviewer.response.fallback) == ('structured', 'reject')
    assert set(json.loads(reviewer.response.schema.schema)['properties']) == {'correct', 'grounded', 'relevant', 'preference'}
    assert 'accepted' not in reviewer.response.schema.schema
    assert renderer.response is None and renderer.structured is None


def test_chat_runtime_envelope_does_not_absorb_transport_response_evidence():
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.control import OperationContext
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, ResponseEvidence, Usage
    from api_agent.workflows.local_application import LocalChatApplication
    root = Path(__file__).resolve().parents[2]
    app = LocalChatApplication(load_definition(root / 'flow.md.html', base_root=root, pack_root=root))
    result = LLMResult(Message('assistant', (TextContent('Hello'),)), Usage(2, 3),
                       response=ResponseEvidence('text', 'reject', 'text', None, None, None))
    provider = SimpleNamespace(complete=AsyncMock(return_value=result))
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('Hi'),)),)))
    actual = asyncio.run(app.complete(request, llm=provider, context=OperationContext('turn')))
    assert actual is result
    provider.complete.assert_awaited_once()


@pytest.mark.parametrize('kind', ['local', 'useful'])
def test_existing_chat_applications_reject_new_caller_policy_before_inference(kind):
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.control import OperationContext
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest, ResponsePolicy
    from api_agent.workflows.local_application import LocalChatApplication
    from test_demo_application import Owner, application
    root = Path(__file__).resolve().parents[2]
    app = LocalChatApplication(load_definition(root / 'flow.md.html', base_root=root, pack_root=root)) if kind == 'local' else application(Owner())
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('Hi'),)),),
                         response=ResponsePolicy('text', 'reject')))
    provider = SimpleNamespace(complete=AsyncMock())
    with pytest.raises(ContractError, match='support|accept'):
        asyncio.run(app.complete(request, llm=provider, context=OperationContext('turn')))
    provider.complete.assert_not_called()


def test_legacy_layout_rejects_misplaced_header_columns(tmp_path):
    from test_workflow_embedded_dialects import declaration
    entry = declaration(tmp_path, {'type': 'object'}, {})
    entry.write_text(entry.read_text().replace('| ID | Registry | Input | Output |',
                                             '| ID | Registry | Output | Input |'))
    with pytest.raises(LayoutError, match='misplaced columns'):
        load_layout(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('schema,value,invalid', [
    ({'type': 'array', 'items': {'type': 'string'}}, ['a'], [1]),
    ({'type': 'string', 'minLength': 2}, 'ok', 'x'),
    ({'type': 'number', 'minimum': 1}, 2, 0),
    ({'type': 'boolean'}, True, 1),
    ({'type': 'null'}, None, 'null'),
])
def test_general_json_response_roots_preserve_schema_and_reject_invalid_generated_value(tmp_path, schema, value, invalid):
    from hashlib import sha256
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import LLMRequest, LLMResult, ResponseEvidence, Usage
    from api_agent.providers.rich_inference import async_validate_result
    definition = admit(response_graph(tmp_path, mode='prompted_json', response=schema))
    payload = context_for(definition).response_request((Message('user', (TextContent('Generate'),)),))
    request = LLMRequest('synthetic', payload)
    assert json.loads(payload.response.schema.schema) == schema
    assert definition.layout.tables['Functions'][0]['Output schema']['type'] == 'object'
    evidence = ResponseEvidence('prompted_json', 'reject', 'prompted_json',
        sha256(payload.response.schema.schema.encode()).hexdigest(), True, None)
    valid = LLMResult(Message('assistant', (TextContent(json.dumps(value)),)), Usage(2, 3), response=evidence)
    assert asyncio.run(async_validate_result(request, valid)) is valid
    rejected = LLMResult(Message('assistant', (TextContent(json.dumps(invalid)),)), Usage(2, 3), response=evidence)
    with pytest.raises(ProviderAccessError, match='original schema') as error:
        asyncio.run(async_validate_result(request, rejected))
    assert error.value.usage is rejected.usage
    assert error.value.response is evidence


@pytest.fixture
def legacy_demo_root(tmp_path):
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'demo'
    shutil.copytree(source, tmp_path / 'demo')
    for path in (tmp_path / 'demo').rglob('*.md.html'):
        source = path.read_text()
        if HEADER not in source:
            continue
        lines = []
        for line in source.splitlines():
            if line.startswith('|') and len(line.split('|')) == 9:
                line = '|' + '|'.join(line.split('|')[1:5]) + '|'
            lines.append(line)
        path.write_text('\n'.join(lines) + '\n')
    return tmp_path


def test_legacy_contrived_build_keeps_captured_schema_and_native_request(legacy_demo_root):
    from api_agent.providers.llm import LLMResult
    from api_agent.workflows.contrived import build
    from test_workflow_trial_decisions import trial_uuid
    requests = []

    async def complete(request):
        requests.append(request)
        metadata = json.loads(request.payload.messages[-1].content[0].text)
        value = {'route': metadata['decisions']['route'], 'ready': metadata['decisions']['ready'],
                 'original_input': 'hello', 'final_output': 'hello'}
        return LLMResult(Message('assistant', (TextContent(json.dumps(value)),)))

    graph = build(legacy_demo_root, llm=complete, selection='synthetic', trial=trial_uuid())
    schema_path = legacy_demo_root / 'demo/contrived/schemas/draft.json'
    captured_schema = json.loads(schema_path.read_text())
    schema_path.write_text('{"type":"string"}')

    result = asyncio.run(graph.arun({'user_input': 'hello'}))

    assert result['first_draft']['final_output'] == 'hello'
    assert len(requests) == 1
    assert requests[0].payload.response is None
    assert json.loads(requests[0].payload.structured.schema) == captured_schema


def test_legacy_useful_application_keeps_plain_native_calls(legacy_demo_root):
    from api_agent.providers.control import OperationContext
    from api_agent.workflows.demo_application import UsefulDemoApplication
    from test_demo_application import LLM, Owner, request
    definition = load_definition(legacy_demo_root / 'demo/simple/flow.md.html',
        base_root=legacy_demo_root, pack_root=legacy_demo_root / 'demo', entry_root='base')
    app = UsefulDemoApplication(definition, tool_owner=Owner())
    llm = LLM(['{"use_tool":false,"arguments":{},"answer":"hello"}',
               '{"correct":true,"grounded":true,"relevant":true,"preference":true}', 'hello'])

    result = asyncio.run(app.complete(request(), llm=llm, context=OperationContext('legacy')))

    assert result.payload.content == (TextContent('hello'),)
    assert len(llm.requests) == 3
    assert all(item.payload.response is None and item.payload.structured is None for item in llm.requests)


@pytest.mark.parametrize('field,stem', [('Input', 'input'), ('Output', 'output'), ('Response schema', 'response')])
def test_non_json_schema_suffix_survives_admission_projection_and_rendering(tmp_path, field, stem):
    from api_agent.workflows.viewer_projection import project_view
    from api_agent.workflows.viewer_render import _projection_source, _reference_sources
    entry = response_graph(tmp_path)
    (tmp_path / f'schemas/{stem}.json').rename(tmp_path / f'schemas/{stem}.schema')
    node = tmp_path / 'workflows/inspect.md.html'
    node.write_text(node.read_text().replace(f'{stem}.json', f'{stem}.schema'))
    projection = project_view(admit(entry))
    document = next(item for item in projection.documents if item.logical_path == projection.entry_path)

    diagram, links = _projection_source(projection, document)

    assert f'{field}: {stem}&#46;schema'.encode() in diagram
    assert f'../../schemas/{stem}.schema' in links
    assert f'r/base/schemas/{stem}.schema' in _reference_sources(projection)


def manual_response_layout(root, *, response, mode, fallback):
    """The supported Layout entrypoint has no admitted Native Kind metadata."""
    from test_workflow_embedded_dialects import declaration
    entry = declaration(root, {'type': 'object'}, {})
    (root / 'response.json').write_text(json.dumps(RESPONSE))
    source = entry.read_text().replace('| ID | Registry | Input | Output |', HEADER)
    source = source.replace('| --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- |')
    entry.write_text(source.replace('[output](output.json) |',
                                   f'[output](output.json) | {response} | {mode} | {fallback} |'))
    return load_layout(entry, base_root=root, pack_root=root)


@pytest.mark.parametrize('response,fallback', [('-', 'reject'), ('[response](response.json)', '-')])
def test_manual_layout_rejects_partial_deterministic_response_dashes(tmp_path, response, fallback):
    with pytest.raises(LayoutError, match='deterministic response columns must all be -'):
        manual_response_layout(tmp_path, response=response, mode='-', fallback=fallback)


@pytest.mark.parametrize('mode,response,fallback,expected_kind,invalid_kind', [
    ('structured', '[response](response.json)', 'reject', 'llm', 'deterministic'),
    ('-', '-', '-', 'deterministic', 'llm'),
])
def test_manual_layout_runtime_rejects_registered_kind_policy_mismatch(
        tmp_path, mode, response, fallback, expected_kind, invalid_kind):
    import runpy
    layout = manual_response_layout(tmp_path, response=response, mode=mode, fallback=fallback)
    native = runpy.run_path(str(tmp_path / 'functions.py'))['produce']
    Workflow(layout, {'produce': NodeFunction(native, expected_kind)})

    with pytest.raises(LayoutError, match='response declaration must match registered node kind'):
        Workflow(layout, {'produce': NodeFunction(native, invalid_kind)})
