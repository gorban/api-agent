"""Selected tool aliases can intentionally reuse compatible base declarations."""
from dataclasses import replace
from pathlib import Path

import pytest

from api_agent.config.declarations import ConfigSchema, schema_from_dict
from api_agent.config import compose_schema, load_declarations, load_shared_schema
from api_agent.providers.loading import bootstrap_schema
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.tooling.models import OperationSelection, ToolSelectionError
from api_agent.tooling.selection import compose_tools
from test_tool_selection import selection_fixture


def test_bundled_provider_headers_reuse_preserves_provider_declaration():
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)
    shared = load_shared_schema(root)
    base = compose_schema(shared, load_declarations(context.config_path, base_schema=shared,
                          source='pack_default'), overrides=context.defaults)
    provider = root / 'src/api_agent/api_providers/graphql_mcp/factory.py'
    schema = bootstrap_schema(base, (), {'API_PROVIDER': str(provider) + ':factory'}, {},
        owner_root=context.owner_root, configuration_paths=context.provider_config_paths)

    plan = compose_tools(context, schema, environ={}, dotenv={})
    collection = compose_tools(context, schema, selection=OperationSelection('collection'), environ={}, dotenv={})

    assert plan.schema.fields['API_HEADERS'] == schema.fields['API_HEADERS']
    assert plan.tools[0].bindings['HEADERS'] == 'API_HEADERS'
    assert collection.schema.fields['API_HEADERS'] == schema.fields['API_HEADERS']
    assert not collection.tools


def test_compatible_base_alias_preserves_original_metadata(selection_fixture):
    context, schema = selection_fixture
    owned = schema_from_dict({'version': 1, 'fields': {'API_HEADERS': {
        'type': 'jsonobject', 'default': {}, 'acknowledge_default': False}}}).fields['API_HEADERS']
    owned = replace(owned, help='Existing owner help', default_source='pack_default',
                    contribution_origin=context.base_root / 'owner.toml')
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=owned))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_HEADERS'] == owned
    assert 'API_HEADERS' in plan.tool_fields


@pytest.mark.parametrize('change', [
    {'type': 'str', 'default': 'secret-value'}, {'default': {'token': 'secret-value'}},
    {'required': True}, {'review_optional': True}, {'acknowledge_default': True},
    {'pack_only': True}, {'cost_affecting': True},
    {'applies_when': {'SHARED': 'shared'}}, {'required_when': {'SHARED': 'shared'}},
    {'conditional_defaults': ({'when': {'SHARED': 'shared'}, 'value': {}},)},
    {'enum': ({},)},
])
def test_incompatible_base_binding_rejects_without_disclosing_values(selection_fixture, change):
    context, schema = selection_fixture
    owned = schema_from_dict({'version': 1, 'fields': {'API_HEADERS': {
        'type': 'jsonobject', 'default': {}, 'acknowledge_default': False}}}).fields['API_HEADERS']
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=replace(owned, **change)))

    with pytest.raises(ToolSelectionError, match='API_HEADERS') as caught:
        compose_tools(context, schema, environ={}, dotenv={})

    assert 'secret-value' not in str(caught.value)


def test_compatible_base_alias_does_not_allow_two_selected_owners(selection_fixture):
    context, schema = selection_fixture
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('API_2_HEADERS', 'API_HEADERS'))
    owned = schema_from_dict({'version': 1, 'fields': {'API_HEADERS': {
        'type': 'jsonobject', 'default': {}, 'acknowledge_default': False}}}).fields['API_HEADERS']
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=owned))

    with pytest.raises(ToolSelectionError, match='API_HEADERS'):
        compose_tools(context, schema, selection=('base::tests/test_flow.py::test_dual',), environ={}, dotenv={})


@pytest.mark.parametrize('change', [
    {'sensitive': True}, {'validation': 'regex'}, {'anchor': 'owner_root'},
    {'default': ''}, {'required': False},
])
def test_base_endpoint_contract_and_unset_default(selection_fixture, change):
    context, schema = selection_fixture
    endpoint = schema_from_dict({'version': 1, 'fields': {'API_ENDPOINT': {
        'type': 'str', 'required': True}}}).fields['API_ENDPOINT']
    owned = replace(endpoint, **change)
    schema = ConfigSchema(dict(schema.fields, API_ENDPOINT=owned))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        compose_tools(context, schema, environ={}, dotenv={})


def test_contract_comparison_accepts_frozen_existing_defaults(selection_fixture):
    context, schema = selection_fixture
    first = compose_tools(context, schema, environ={}, dotenv={})
    headers = first.schema.fields['API_HEADERS']
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=headers))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_HEADERS'] == headers


def test_base_endpoint_compatible_unset_default(selection_fixture):
    context, schema = selection_fixture
    endpoint = schema_from_dict({'version': 1, 'fields': {'API_ENDPOINT': {
        'type': 'str', 'required': True}}}).fields['API_ENDPOINT']
    schema = ConfigSchema(dict(schema.fields, API_ENDPOINT=endpoint))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_ENDPOINT'] == endpoint


def test_shared_primary_keeps_preexisting_secondary_required_contract(selection_fixture):
    from api_agent.config.resolve import resolve_config
    context, schema = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="dual_graph.md.html"', 'entry="dual_graph.md.html"\nprofile="shared"') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\n'
        'primary="sample/primary"\nsecondary=["sample/secondary"]\n')
    endpoint = schema_from_dict({'version': 1, 'fields': {'API_2_ENDPOINT': {
        'type': 'str', 'required': True}}}).fields['API_2_ENDPOINT']
    schema = ConfigSchema(dict(schema.fields, API_2_ENDPOINT=endpoint))

    plan = compose_tools(context, schema, selection=('base::tests/test_flow.py::test_dual',), environ={}, dotenv={})
    result = resolve_config(plan.schema, environ={}, overrides={'API_ENDPOINT': 'https://one.invalid'})
    bound = plan.bind_values(result)

    assert plan.schema.fields['API_2_ENDPOINT'] == endpoint
    assert result.missing == ('API_2_ENDPOINT',)
    assert bound.config.missing == ('API_2_ENDPOINT',)
    assert bound.tools[1].local_values['ENDPOINT'] == 'https://one.invalid'


@pytest.mark.parametrize(('input_metadata', 'base_metadata'), [
    ('default=1.0', {'default': 1}),
    ('enum=[1.0]\ndefault=1.0', {'enum': [1], 'default': 1}),
    ('conditional_defaults=[{when={SHARED="shared"},value=1.0}]',
     {'conditional_defaults': [{'when': {'SHARED': 'shared'}, 'value': 1}]}),
    ('', {}),
])
def test_float_binding_compares_typed_effective_values(selection_fixture, input_metadata, base_metadata):
    context, schema = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace(
        '[inputs.ENDPOINT]\ntype="str"\nrequired=true',
        '[inputs.ENDPOINT]\ntype="float"\nrequired=true\n' + input_metadata))
    endpoint = schema_from_dict({'version': 1, 'fields': {'API_ENDPOINT': {
        'type': 'float', 'required': True, **base_metadata}}}, base_schema=schema).fields['API_ENDPOINT']
    schema = ConfigSchema(dict(schema.fields, API_ENDPOINT=endpoint))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_ENDPOINT'] == endpoint


def test_json_binding_keeps_nested_boolean_and_integer_defaults_distinct(selection_fixture):
    context, schema = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('default={}', 'default={flag=true}'))
    headers = schema_from_dict({'version': 1, 'fields': {'API_HEADERS': {
        'type': 'jsonobject', 'default': {'flag': 1}, 'acknowledge_default': False}}}).fields['API_HEADERS']
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=headers))

    with pytest.raises(ToolSelectionError, match='API_HEADERS'):
        compose_tools(context, schema, environ={}, dotenv={})


@pytest.mark.parametrize('authority', [
    {'override': True}, {'override': True, 'override_if_present': True},
])
def test_compatible_alias_retains_original_composition_authority(selection_fixture, authority):
    context, schema = selection_fixture
    owned = schema_from_dict({'version': 1, 'fields': {'API_HEADERS': {
        'type': 'jsonobject', 'default': {}, 'acknowledge_default': False}}}).fields['API_HEADERS']
    owned = replace(owned, **authority)
    schema = ConfigSchema(dict(schema.fields, API_HEADERS=owned))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_HEADERS'] == owned


@pytest.fixture
def composed_pack_binding(selection_fixture):
    root = Path(__file__).resolve().parents[2]
    bundled = load_launch_context(root)
    shared = load_shared_schema(root)
    composed = compose_schema(shared, load_declarations(bundled.config_path, base_schema=shared,
        source='pack_default'), overrides=bundled.defaults)
    owned = composed.fields['WORKFLOW_MAX_PASSES']
    assert owned.override and owned.type == 'int' and owned.default == 2
    assert owned.validation == 'positive' and owned.acknowledge_default is False
    context, schema = selection_fixture
    primary = context.base_root / 'primary.toml'
    primary.write_text(primary.read_text().replace('ENDPOINT="API_ENDPOINT"', 'ENDPOINT="WORKFLOW_MAX_PASSES"'))
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('type="str"\nrequired=true',
        'type="int"\ndefault=2\nvalidation="positive"\nacknowledge_default=false'))
    return context, ConfigSchema(dict(schema.fields, WORKFLOW_MAX_PASSES=owned)), owned


def test_actual_composed_pack_field_binds_compatible_input(composed_pack_binding):
    context, schema, owned = composed_pack_binding

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['WORKFLOW_MAX_PASSES'] == owned
    assert plan.tools[0].bindings['ENDPOINT'] == 'WORKFLOW_MAX_PASSES'
    assert plan.schema.fields['WORKFLOW_MAX_PASSES'].default_source == 'pack_default'


def test_actual_composed_pack_field_still_rejects_weaker_validation(composed_pack_binding):
    context, schema, _ = composed_pack_binding
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('validation="positive"', 'validation="nonnegative"'))

    with pytest.raises(ToolSelectionError, match='WORKFLOW_MAX_PASSES: Conflicting configuration binding ownership'):
        compose_tools(context, schema, environ={}, dotenv={})


@pytest.mark.parametrize('authority', ['override=true', 'override=true\noverride_if_present=true'])
def test_tool_authoring_flags_do_not_replace_existing_owner(selection_fixture, authority):
    context, schema = selection_fixture
    owned = schema_from_dict({'version': 1, 'fields': {'API_ENDPOINT': {
        'type': 'str', 'required': True}}}).fields['API_ENDPOINT']
    schema = ConfigSchema(dict(schema.fields, API_ENDPOINT=owned))
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('required=true', 'required=true\n' + authority))

    plan = compose_tools(context, schema, environ={}, dotenv={})

    assert plan.schema.fields['API_ENDPOINT'] == owned
    assert not plan.schema.fields['API_ENDPOINT'].override
    assert not plan.schema.fields['API_ENDPOINT'].override_if_present
