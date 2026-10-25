"""Compatible aliases retain independent owners across disjoint exact cases."""
from dataclasses import replace

import pytest

from api_agent.config.resolve import resolve_config
from api_agent.tooling.models import OperationSelection, ToolSelectionError
from api_agent.tooling.selection import compose_tools
from test_tool_selection import selection_fixture, graph, write

PRIMARY = 'base::tests/test_flow.py::test_primary'
SECONDARY = 'base::tests/test_flow.py::test_secondary'
DUAL = 'base::tests/test_flow.py::test_dual'


@pytest.fixture
def separate_cases(selection_fixture):
    context, schema = selection_fixture
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('API_2_ENDPOINT', 'API_ENDPOINT')
                         .replace('API_2_HEADERS', 'API_HEADERS'))
    graph(context.base_root, 'secondary_graph', [('secondary', 'secondary.toml')])
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        '[test_cases]', '[graphs.secondary]\nroot="base"\nentry="secondary_graph.md.html"\n[test_cases]') +
        '\n"' + SECONDARY + '"=["secondary"]\n')
    return context, schema


def selected(fixture, cases=(PRIMARY, SECONDARY), **kwargs):
    context, schema = fixture
    return compose_tools(context, schema, selection=cases, environ={}, dotenv={}, **kwargs)


def test_separate_exact_cases_reuse_aliases_without_merging_owners(separate_cases):
    plan = selected(separate_cases)
    bound = plan.bind_values(resolve_config(plan.schema, environ={}, overrides={'API_ENDPOINT': 'https://example.invalid'}))

    assert tuple(tool.id for tool in bound.tools) == ('sample/primary', 'sample/secondary')
    assert len({tool.descriptor_path for tool in bound.tools}) == 2
    assert tuple(tool.bindings['ENDPOINT'] for tool in bound.tools) == ('API_ENDPOINT', 'API_ENDPOINT')
    assert bound.for_case(PRIMARY).tools[0].id == 'sample/primary'
    assert bound.for_case(SECONDARY).tools[0].id == 'sample/secondary'
    assert not bound.for_case(PRIMARY).tools[0].derived_from


@pytest.mark.parametrize('cases', [(DUAL,), (PRIMARY, DUAL), (PRIMARY, SECONDARY, DUAL)])
def test_any_case_co_consuming_alias_owners_rejects(separate_cases, cases):
    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases, cases)


def test_application_alias_owners_still_reject(separate_cases):
    context, schema = separate_cases
    context = replace(context, workflow_entry=context.base_root / 'dual_graph.md.html')

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        compose_tools(context, schema, environ={}, dotenv={})


@pytest.mark.parametrize('replacement', [
    'required=false', 'required=true\ndefault="different"',
    'required=true\napplies_when={SHARED="shared"}',
    'required=true\nnonempty=true', 'required=true\nacknowledge_default=false',
])
def test_separate_cases_require_identical_effective_contracts(separate_cases, replacement):
    context, _ = separate_cases
    alternate = context.base_root / 'alternate.toml'
    alternate.write_text((context.base_root / 'graphql.toml').read_text().replace('required=true', replacement))
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('implementation="graphql.toml"', 'implementation="alternate.toml"'))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases)


def test_two_slots_in_one_instance_cannot_reuse_an_alias(separate_cases):
    context, _ = separate_cases
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('type="jsonobject"\ndefault={}\nacknowledge_default=false',
        'type="str"\nrequired=true'))
    primary = context.base_root / 'primary.toml'
    primary.write_text(primary.read_text().replace('HEADERS="API_HEADERS"', 'HEADERS="API_ENDPOINT"'))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases, (PRIMARY,))


def test_cross_case_alias_cannot_claim_an_implementation_selector(separate_cases):
    context, _ = separate_cases
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('ENDPOINT="API_ENDPOINT"', 'ENDPOINT="PRIMARY_IMPLEMENTATION"')
                         .replace('implementation="graphql.toml"', 'implementation="selector_input.toml"'))
    write(context.base_root, 'selector_input.toml', (context.base_root / 'graphql.toml').read_text().replace(
        'required=true', 'default="graphql.toml"\nacknowledge_default=false'))

    with pytest.raises(ToolSelectionError, match='PRIMARY_IMPLEMENTATION'):
        selected(separate_cases)


def test_unscoped_variant_alias_owner_is_not_an_exact_case(separate_cases):
    context, schema = separate_cases
    text = context.tool_catalog_path.read_text()
    context.tool_catalog_path.write_text(text + '\n[variants.unscoped]\ngraph="secondary"\nreplacements=[]\n')

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        compose_tools(context, schema, selection=OperationSelection('tests', (PRIMARY,), ('unscoped',)), environ={}, dotenv={})


def test_invalid_later_owner_declaration_cannot_hide_behind_first_alias(separate_cases):
    context, _ = separate_cases
    alternate = context.base_root / 'alternate.toml'
    alternate.write_text((context.base_root / 'graphql.toml').read_text().replace('required=true', 'required=true\ndefault=123'))
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('implementation="graphql.toml"', 'implementation="alternate.toml"'))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT: Invalid selected tool input declaration'):
        selected(separate_cases)


def add_third_case(context):
    third = (context.base_root / 'secondary.toml').read_text().replace('sample/secondary', 'sample/third')
    third = third.replace('SECONDARY_IMPLEMENTATION', 'THIRD_IMPLEMENTATION')
    write(context.base_root, 'third.toml', third)
    graph(context.base_root, 'third_graph', [('third', 'third.toml')])
    case = 'base::tests/test_flow.py::test_third'
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        '[test_cases]', '[graphs.third]\nroot="base"\nentry="third_graph.md.html"\n[test_cases]') +
        '\n"' + case + '"=["third"]\n')
    return case


def test_three_disjoint_owners_are_pairwise_compatible(separate_cases):
    context, _ = separate_cases
    third = add_third_case(context)

    plan = selected(separate_cases, (PRIMARY, SECONDARY, third))

    assert len(plan.tools) == 3
    assert {plan.for_case(case).tools[0].id for case in (PRIMARY, SECONDARY, third)} == {
        'sample/primary', 'sample/secondary', 'sample/third'}


def test_third_owner_pair_co_consumed_in_one_case_rejects(separate_cases):
    context, _ = separate_cases
    third = add_third_case(context)
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        '"' + third + '"=["third"]', '"' + third + '"=["secondary", "third"]'))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases, (PRIMARY, SECONDARY, third))


def test_single_case_with_two_graphs_cannot_share_independent_alias_owners(separate_cases):
    context, _ = separate_cases
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        '"' + PRIMARY + '"=["primary"]', '"' + PRIMARY + '"=["primary", "secondary"]'))

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases, (PRIMARY,))


def test_cross_case_alias_keeps_distinct_local_outputs_through_wire(separate_cases):
    from api_agent.tooling.models import ToolPlan
    context, _ = separate_cases
    write(context.base_root, 'compose.json', '{"image":"example:1"}')
    write(context.base_root, 'infrastructure.toml', 'version=1\nowner="sample"\n' + ''.join(
        '[[services]]\nid="' + name + '"\ncompose="compose.json"\ndependencies=[]\n'
        'readiness={kind="health",timeout=1}\n'
        'endpoints=[{name="graphql",host="http://127.0.0.1:5000",in_project="http://self:5000"}]\n'
        for name in ('primary', 'secondary')))
    for name in ('primary', 'secondary'):
        instance = context.base_root / (name + '.toml')
        instance.write_text(instance.read_text() + '\n[defaults]\nENDPOINT=""\n'
            '[local]\nslot="ENDPOINT"\noutput="sample/' + name + '/graphql"\n'
            'path="/graphql"\ndeclaration="infrastructure.toml"\n'
            '[infrastructure]\nowner="sample"\nroot="."\ndeclarations=["infrastructure.toml"]\n')
    plan = selected(separate_cases)
    bound = plan.bind_values(resolve_config(plan.schema, environ={}))

    restored = ToolPlan.from_wire(bound.to_wire()).bind_values(resolve_config(bound.schema, environ={}))

    assert tuple(tool.pending['ENDPOINT']['output'] for tool in restored.tools) == (
        'sample/primary/graphql', 'sample/secondary/graphql')
    assert restored.for_case(PRIMARY).tools[0].pending['ENDPOINT']['output'] == 'sample/primary/graphql'
    assert restored.for_case(SECONDARY).tools[0].pending['ENDPOINT']['output'] == 'sample/secondary/graphql'
    assert all(tool.value_sources['ENDPOINT']['source'] == 'shared_default' for tool in restored.tools)
    assert all(tool.configured_values['ENDPOINT'] == '' for tool in restored.tools)


def test_shared_derived_alias_cannot_weaken_an_independent_case(selection_fixture):
    context, schema = selection_fixture
    third = add_third_case(context)
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="dual_graph.md.html"', 'entry="dual_graph.md.html"\nprofile="shared"') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\n'
        'primary="sample/primary"\nsecondary=["sample/secondary"]\n')

    with pytest.raises(ToolSelectionError, match='API_2_ENDPOINT: Conflicting derived'):
        selected((context, schema), (DUAL, third))


def test_unscoped_variant_reusing_covered_definition_still_rejects(separate_cases):
    context, schema = separate_cases
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text() +
        '\n[variants.unscoped]\ngraph="secondary"\nreplacements=[]\n')

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        compose_tools(context, schema, selection=OperationSelection('tests', (PRIMARY, SECONDARY), ('unscoped',)),
                      environ={}, dotenv={})


def test_inherited_graph_and_node_owners_cannot_share_config_aliases(separate_cases):
    context, _ = separate_cases
    node = context.base_root / 'node.md.html'
    node.write_text(node.read_text() + '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n'
        '| secondary | [instance](secondary.toml) |\n')

    with pytest.raises(ToolSelectionError, match='API_ENDPOINT'):
        selected(separate_cases, (PRIMARY,))
