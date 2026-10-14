"""References are rejected at schema positions while instance data executes."""
import pytest
from jsonschema import Draft202012Validator

from api_agent.workflows import LayoutError, load_layout
from test_workflow_embedded_dialects import POSITIONS, contract, declaration, execute


@pytest.mark.parametrize('keyword', ['const', 'default', 'enum', 'examples', 'customAnnotation', 'property_names'])
def test_reference_like_data_executes_source_matched_graph(tmp_path, keyword):
    value = {'$schema': 'https://example.invalid/data', '$ref': 'literal', '$dynamicRef': 'another literal'}
    annotation = [value] if keyword in ('enum', 'examples') else value
    schema = {'type': 'object', keyword: annotation}
    if keyword == 'property_names':
        schema = {'type': 'object', 'properties': {'$ref': {'type': 'string'}, '$dynamicRef': {'type': 'string'}}}
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    path = declaration(tmp_path, schema, value)
    functions, inference = [], []

    result = execute(path, functions, inference)

    assert result['example'] == value
    assert functions == ['produce'] and inference == ['infer']


@pytest.mark.parametrize('keyword', POSITIONS)
@pytest.mark.parametrize('reference', ['$ref', '$dynamicRef'])
def test_schema_position_reference_rejects_before_effects(tmp_path, keyword, reference):
    schema = contract(keyword, {'properties': {'nested': {reference: 'https://example.invalid/schema'}}})
    Draft202012Validator.check_schema(schema)
    path = declaration(tmp_path, schema, {})
    functions, inference = [], []

    with pytest.raises(LayoutError, match='without remote references'):
        execute(path, functions, inference)

    assert functions == [] and inference == []


@pytest.mark.parametrize('reference', ['$ref', '$dynamicRef'])
def test_root_reference_rejects_before_effects(tmp_path, reference):
    path = declaration(tmp_path, {'type': 'object', reference: '#local'}, {})
    functions, inference = [], []

    with pytest.raises(LayoutError, match='without remote references'):
        execute(path, functions, inference)

    assert functions == [] and inference == []


def test_dependency_property_names_are_ordinary_data(tmp_path):
    schema = {'type': 'object', 'dependencies': {'trigger': ['$ref', '$dynamicRef']}}
    path = declaration(tmp_path, schema, {'trigger': True})
    functions, inference = [], []

    assert execute(path, functions, inference)['example'] == {'trigger': True}
    assert functions == ['produce'] and inference == ['infer']


@pytest.mark.parametrize('keyword', POSITIONS)
def test_boolean_schema_children_are_reference_free(tmp_path, keyword):
    path = declaration(tmp_path, contract(keyword, True), {})

    assert load_layout(path, base_root=tmp_path, pack_root=tmp_path)
