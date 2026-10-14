"""Declared dialects are checked at schema positions before graph effects."""
import asyncio
import json
import runpy

import pytest

from api_agent.workflows import LayoutError, NodeFunction, Workflow, load_layout

CANONICAL = 'https://json-schema.org/draft/2020-12/schema'
POSITIONS = ('definitions', 'dependencies', '$defs', 'properties', 'patternProperties', 'dependentSchemas',
             'allOf', 'anyOf', 'oneOf', 'prefixItems', 'additionalProperties',
             'unevaluatedProperties', 'propertyNames', 'items', 'contains',
             'unevaluatedItems', 'not', 'if', 'then', 'else', 'contentSchema')


def contract(keyword, child):
    if keyword in ('definitions', 'dependencies', '$defs', 'properties', 'patternProperties', 'dependentSchemas'):
        value = {'payload': child}
    elif keyword in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
        value = [child]
    else:
        value = child
    return {'type': 'object', keyword: value}


def declaration(root, schema, result):
    (root / 'input.json').write_text('{"type":"object"}')
    (root / 'output.json').write_text(json.dumps(schema))
    (root / 'functions.py').write_text(
        'async def produce(inputs, context):\n'
        '    context.services["functions"].append("produce")\n'
        '    await context.services["infer"]()\n'
        f'    return {result!r}\n')
    path = root / 'layout.md'
    path.write_text('''# LangGraph Layout v1

## Flow

1.  [node example](functions.py)

## Components

## Functions

| ID | Registry | Input | Output |
| --- | --- | --- | --- |
| example | produce | [input](input.json) | [output](output.json) |

## Bindings

## Edges

## Prompts

## Timeouts

## Notes
''')
    return path


def execute(path, functions, inference):
    async def infer():
        inference.append('infer')
    layout = load_layout(path, base_root=path.parent, pack_root=path.parent)
    source = runpy.run_path(str(path.parent / 'functions.py'))['produce']
    graph = Workflow(layout, {'produce': NodeFunction(source)},
                     services={'functions': functions, 'infer': infer})
    return asyncio.run(graph.arun({}))


@pytest.mark.parametrize('keyword', POSITIONS)
@pytest.mark.parametrize('dialect', ['http://json-schema.org/draft-07/schema#', 'https://example.invalid/unknown'])
def test_embedded_resource_dialect_rejects_before_real_function_and_inference(tmp_path, keyword, dialect):
    child = {'$id': 'https://example.invalid/embedded', '$schema': dialect,
             'dependencies': {'trigger': ['required_peer']}}
    path = declaration(tmp_path, contract(keyword, child), {'payload': {'trigger': True}})
    functions, inference = [], []
    with pytest.raises(LayoutError, match='only JSON Schema draft 2020-12') as observed:
        execute(path, functions, inference)
    assert 'layout.md' in str(observed.value)
    assert functions == [] and inference == []


@pytest.mark.parametrize('keyword', ['properties', 'definitions', 'dependencies'])
@pytest.mark.parametrize('dialect', [None, CANONICAL])
def test_supported_embedded_resource_executes(tmp_path, keyword, dialect):
    child = {'$id': 'https://example.invalid/embedded', 'type': 'object'}
    if dialect is not None:
        child['$schema'] = dialect
    value = {'payload': {}}
    path = declaration(tmp_path, contract(keyword, child), value)
    functions, inference = [], []
    assert execute(path, functions, inference)['example'] == value
    assert functions == ['produce'] and inference == ['infer']


@pytest.mark.parametrize('keyword', ['const', 'default', 'examples', 'enum', 'customAnnotation'])
def test_dialect_like_instance_data_is_preserved(tmp_path, keyword):
    value = {'$schema': 'https://example.invalid/unknown', '$id': 'https://example.invalid/data'}
    annotation = [value] if keyword in ('examples', 'enum') else value
    schema = {'type': 'object', keyword: annotation,
              'properties': {'$schema': {'type': 'string'}, '$id': {'type': 'string'}}}
    path = declaration(tmp_path, schema, value)
    functions, inference = [], []
    assert execute(path, functions, inference)['example'] == value
    assert functions == ['produce'] and inference == ['infer']


@pytest.mark.parametrize('keyword', POSITIONS)
def test_boolean_subschemas_have_no_declared_dialect(tmp_path, keyword):
    path = declaration(tmp_path, contract(keyword, True), {})
    assert load_layout(path, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('keyword,value', [('definitions', []), ('dependencies', []), ('items', 7), ('$defs', []), ('allOf', {}), ('properties', []), ('prefixItems', {})])
def test_malformed_schema_containers_still_reject(tmp_path, keyword, value):
    path = declaration(tmp_path, {'type': 'object', keyword: value}, {})
    with pytest.raises(LayoutError):
        load_layout(path, base_root=tmp_path, pack_root=tmp_path)


def test_dependency_property_names_remain_data(tmp_path):
    schema = {'type': 'object', 'dependencies': {'trigger': ['$schema', 'definitions']}}
    path = declaration(tmp_path, schema, {'trigger': True})
    functions, inference = [], []
    assert execute(path, functions, inference)['example'] == {'trigger': True}
    assert functions == ['produce'] and inference == ['infer']


@pytest.mark.parametrize('keyword', ['definitions', 'dependencies'])
def test_deprecated_schema_position_recurses(tmp_path, keyword):
    child = {'properties': {'nested': {'$schema': 'https://example.invalid/unknown'}}}
    path = declaration(tmp_path, contract(keyword, child), {})
    functions, inference = [], []
    with pytest.raises(LayoutError, match='only JSON Schema draft 2020-12'):
        execute(path, functions, inference)
    assert functions == [] and inference == []
