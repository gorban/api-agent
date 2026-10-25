"""Selected graphs alone contribute named tool inputs and frozen admissions."""
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from api_agent.config.declarations import schema_from_dict
from api_agent.config.resolve import resolve_config
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.tooling.models import OperationSelection, ToolSelectionError
from api_agent.tooling.selection import compose_tools, discover


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def graph(root, name, instances):
    ports = ''.join(f'| {port} | [instance]({path}) |\n' for port, path in instances)
    return write(root, name + '.md.html', '# Workflow v3\n\n## Graph ID\n\n| ID |\n| --- |\n| '
        + name + ' |\n\n## State\n\n| Registry | Source |\n| --- | --- |\n| State | [native](native.py) |\n'
        '\n## Flow\n\n1.  [node inspect](node.md.html)\n\n## Edges\n\n'
        '| From | To | Router | Source |\n| --- | --- | --- | --- |\n| inspect | END | - | - |\n'
        '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n' + ports)


@pytest.fixture
def selection_fixture(tmp_path):
    root = tmp_path
    write(root, 'native.py', 'raise AssertionError("workflow native imported during discovery")\n')
    write(root, 'node.md.html', '# Node v3: inspect\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| inspect | deterministic | [native](native.py) |\n')
    write(root, 'package/factory.py', 'raise AssertionError("tool imported during discovery")\ndef create_tool(config, services): pass\n')
    write(root, 'graphql.toml', 'version=1\ninterface="verified-read@1"\nfactory="package/factory.py:create_tool"\n'
        '[inputs.ENDPOINT]\ntype="str"\nrequired=true\n'
        '[inputs.HEADERS]\ntype="jsonobject"\ndefault={}\nacknowledge_default=false\n')
    write(root, 'rest.toml', 'version=1\ninterface="verified-read@1"\nfactory="package/factory.py:create_tool"\n'
        '[inputs.ENDPOINT]\ntype="str"\nrequired=true\n'
        '[inputs.PATH]\ntype="str"\ndefault="/records"\n')
    for name, endpoint in [('primary', 'API_ENDPOINT'), ('secondary', 'API_2_ENDPOINT')]:
        write(root, name + '.toml', f'version=1\nid="sample/{name}"\ninterface="verified-read@1"\n'
            f'selector="{name.upper()}_IMPLEMENTATION"\nimplementation="graphql.toml"\n'
            f'[bindings]\nENDPOINT="{endpoint}"\nHEADERS="{endpoint.replace("ENDPOINT", "HEADERS")}"\n'
            f'PATH="{endpoint.replace("ENDPOINT", "PATH")}"\n')
    primary = graph(root, 'primary_graph', [('primary', 'primary.toml')])
    graph(root, 'dual_graph', [('primary', 'primary.toml'), ('secondary', 'secondary.toml')])
    catalog = write(root, 'catalog.toml', 'version=1\n'
        '[graphs.primary]\nroot="base"\nentry="primary_graph.md.html"\n'
        '[graphs.dual]\nroot="base"\nentry="dual_graph.md.html"\n'
        '[test_cases]\n"base::tests/test_flow.py::test_primary"=["primary"]\n'
        '"base::tests/test_flow.py::test_dual"=["dual"]\n'
        '"base::tests/test_flow.py::test_graphless"=[]\n')
    context = LaunchContext(root, root, root, 'sample', 'sample', root / 'config.toml',
        MappingProxyType({}), MappingProxyType({}), workflow_entry=primary,
        tool_catalog_path=catalog)
    base = schema_from_dict({'version': 1, 'fields': {'SHARED': {'type': 'str', 'default': 'shared'}}})
    return context, base


def plan(fixture, selection=None, **kwargs):
    context, base = fixture
    return compose_tools(context, base, selection=selection, environ={}, **kwargs)


def test_application_and_exact_test_unions_control_review_membership(selection_fixture):
    application = plan(selection_fixture)
    primary = plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))
    dual = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))
    empty = plan(selection_fixture, ())
    graphless = plan(selection_fixture, ('base::tests/test_flow.py::test_graphless',))

    assert 'API_ENDPOINT' in application.schema.fields
    assert 'API_2_ENDPOINT' not in application.schema.fields
    assert tuple(primary.schema.fields) == tuple(application.schema.fields)
    assert 'API_2_ENDPOINT' in dual.schema.fields
    assert len(dual.tools) == 2
    assert len(dual.definitions) == 1 and dual.definitions[0].graph_id == 'dual_graph'
    assert not empty.tools and not graphless.tools
    assert set(empty.schema.fields) == {'SHARED'}
    assert 'API_2_ENDPOINT' in application.inactive_fields


def test_selected_implementation_adds_only_consumed_slots(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',),
        overrides={'SECONDARY_IMPLEMENTATION': 'rest.toml'})

    assert {'API_ENDPOINT', 'API_HEADERS', 'API_2_ENDPOINT', 'API_2_PATH'} <= selected.schema.fields.keys()
    assert 'API_2_HEADERS' not in selected.schema.fields
    assert selected.schema.fields['API_2_PATH'].default == '/records'
    assert 'SECONDARY_IMPLEMENTATION' not in selected.review_fields
    assert selected.tools[0].factory == selected.tools[1].factory
    assert selected.tools[0].id != selected.tools[1].id
    assert selected.tools[0].admission.sources['factory.py'].startswith(b'raise AssertionError')


def test_original_explicit_input_errors_survive_composition(selection_fixture):
    argv = ('--secondary-implementation', 'rest.toml', '--api-2-path', '/one', '--api-2-path=/two', '--unknown')
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',), argv=argv)

    result = resolve_config(selected.schema, argv=argv, environ={})

    assert 'Duplicate option --api-2-path' in result.errors
    assert 'Unknown option --unknown' in result.errors
    assert result.values['API_2_PATH'] == '/one'


def test_duplicate_selector_fails_before_factory_capture(selection_fixture):
    with pytest.raises(ToolSelectionError, match='Duplicate option'):
        plan(selection_fixture, argv=('--primary-implementation=graphql.toml', '--primary-implementation=rest.toml'))


def test_configured_values_do_not_enter_descriptor_identity(selection_fixture):
    first = plan(selection_fixture, overrides={'API_ENDPOINT': 'https://one.invalid', 'API_HEADERS': '{"token":"private-secret-one"}'})
    second = plan(selection_fixture, overrides={'API_ENDPOINT': 'https://two.invalid', 'API_HEADERS': '{"token":"private-secret-two"}'})

    assert first.descriptor_id == second.descriptor_id
    assert first.operation_id != second.operation_id
    assert 'private-secret-one' not in repr(first)
    assert 'private-secret-two' not in repr(second)


def test_bound_values_preserve_local_slot_and_source_isolation(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))
    result = resolve_config(selected.schema, environ={}, overrides={
        'API_ENDPOINT': 'https://one.invalid', 'API_2_ENDPOINT': 'https://two.invalid'})

    bound = selected.bind_values(result)

    assert [tool.local_values['ENDPOINT'] for tool in bound.tools] == ['https://one.invalid', 'https://two.invalid']
    assert bound.tools[1].value_sources['ENDPOINT']['field'] == 'API_2_ENDPOINT'
    with pytest.raises(TypeError):
        bound.tools[0].local_values['ENDPOINT'] = 'changed'


def test_malformed_unselected_graph_is_diagnostic_not_application_blocker(selection_fixture):
    context, _ = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text()
        + '\n[graphs.broken]\nroot="base"\nentry="missing.md.html"\n')

    catalog = discover(context)
    application = plan(selection_fixture)

    assert 'broken' in catalog.diagnostics
    assert 'API_ENDPOINT' in application.schema.fields


def test_selected_bad_graph_blocks_before_tool_import(selection_fixture):
    context, _ = selection_fixture
    (context.base_root / 'dual_graph.md.html').unlink()

    with pytest.raises(ToolSelectionError, match='Cannot admit catalog graph: dual'):
        plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))


def test_alias_reuse_between_distinct_instances_is_rejected(selection_fixture):
    context, _ = selection_fixture
    path = context.base_root / 'secondary.toml'
    path.write_text(path.read_text().replace('API_2_ENDPOINT', 'API_ENDPOINT'))

    with pytest.raises(ToolSelectionError, match='binding ownership'):
        plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))


def test_shared_primary_profile_derives_secondary_and_rejects_conflicts(selection_fixture):
    context, _ = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="dual_graph.md.html"', 'entry="dual_graph.md.html"\nprofile="shared"') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))
    result = resolve_config(selected.schema, environ={}, overrides={'API_ENDPOINT': 'https://one.invalid'})

    bound = selected.bind_values(result)

    assert not result.missing
    assert bound.tools[1].local_values['ENDPOINT'] == 'https://one.invalid'
    assert bound.tools[1].value_sources['ENDPOINT']['source'] == 'derived'
    assert bound.shared_primary == {'sample/secondary': 'sample/primary'}
    conflicting = resolve_config(selected.schema, environ={}, overrides={
        'API_ENDPOINT': 'https://one.invalid', 'API_2_ENDPOINT': 'https://other.invalid'})
    with pytest.raises(ToolSelectionError, match='conflicts with shared primary'):
        selected.bind_values(conflicting)


def test_exact_parameterized_case_does_not_select_its_sibling(selection_fixture):
    context, _ = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'test_primary"', 'test_primary[first]"'))

    sibling = plan(selection_fixture, OperationSelection('tests',
        ('base::tests/test_flow.py::test_primary[second]',),
        graphless=('base::tests/test_flow.py::test_primary[second]',)))
    exact = plan(selection_fixture, ('base::tests/test_flow.py::test_primary[first]',))

    assert not sibling.tools
    assert len(exact.tools) == 1


def test_worker_wire_roundtrip_uses_only_selected_captured_sources(selection_fixture, monkeypatch):
    import json
    from api_agent.tooling.models import ToolPlan
    selected = plan(selection_fixture)
    bound = selected.bind_values(resolve_config(selected.schema, environ={}, overrides={'API_ENDPOINT': 'https://one.invalid'}))
    packet = json.loads(json.dumps(bound.to_wire()))
    def no_filesystem(*args, **kwargs):
        raise AssertionError('worker attempted discovery')
    monkeypatch.setattr(Path, 'read_bytes', no_filesystem)
    monkeypatch.setattr(Path, 'read_text', no_filesystem)

    restored = ToolPlan.from_wire(packet)

    assert restored.descriptor_id == bound.descriptor_id
    assert restored.operation_id == bound.operation_id
    assert restored.bindings == bound.bindings
    assert restored.tools[0].admission.sources == bound.tools[0].admission.sources
    assert restored.tools[0].local_values == bound.tools[0].local_values
    assert restored.catalog is None
    assert len(restored.definitions) == 1
    with pytest.raises(TypeError):
        restored.definitions[0].contents[Path('invented')] = b'new graph'


def test_worker_wire_rejects_unknown_executable_model(selection_fixture):
    from api_agent.tooling.models import ToolPlan
    packet = plan(selection_fixture).to_wire()
    packet['plan']['class'] = 'UserSuppliedExecutable'

    with pytest.raises(ToolSelectionError, match='captured tool model'):
        ToolPlan.from_wire(packet)


def test_collection_option_inventory_preserves_inactive_boolean_arity(selection_fixture, monkeypatch):
    from api_agent.config.options import parse_options
    from api_agent.tooling import admission
    context, _ = selection_fixture
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('[bindings]', '[bindings]\nENABLED="SECONDARY_ENABLED"'))
    rest = context.base_root / 'rest.toml'
    rest.write_text(rest.read_text() + '\n[inputs.ENABLED]\ntype="bool"\ndefault=false\n')
    def no_factory(*args, **kwargs):
        raise AssertionError('collection attempted factory admission')
    monkeypatch.setattr(admission, 'capture_tool_factory', no_factory)
    argv = ('--secondary-implementation=rest.toml', '--secondary-enabled', 'base::tests/test_flow.py::test_dual')

    selected = plan(selection_fixture, (), argv=argv)
    parsed = parse_options(selected.option_schema, argv)

    assert not selected.tools
    assert 'SECONDARY_ENABLED' not in selected.schema.fields
    assert parsed.values['SECONDARY_ENABLED'] == 'True'
    assert parsed.positionals == ('base::tests/test_flow.py::test_dual',)
    assert not parsed.errors


def test_worker_wire_preserves_exact_case_selection(selection_fixture):
    from api_agent.tooling.models import ToolPlan
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))

    restored = ToolPlan.from_wire(selected.to_wire())

    assert restored.selection.test_ids == ('base::tests/test_flow.py::test_dual',)


def test_predeclared_selector_conditional_default_and_explicit_precedence(selection_fixture):
    context, base = selection_fixture
    extension = schema_from_dict({'version': 1, 'fields': {
        'USE_REST': {'type': 'bool', 'default': True},
        'PRIMARY_IMPLEMENTATION': {'type': 'str', 'acknowledge_default': False,
            'conditional_defaults': [{'when': {'USE_REST': True}, 'value': 'rest.toml'}]}}}, base_schema=base)
    base = type(base)(dict(base.fields, **extension.fields))

    conditional = plan((context, base))
    explicit = plan((context, base), argv=('--primary-implementation=graphql.toml',))
    fallback = plan((context, base), overrides={'USE_REST': 'False'})

    assert 'API_PATH' in conditional.schema.fields
    assert 'API_HEADERS' not in conditional.schema.fields
    assert 'API_HEADERS' in explicit.schema.fields
    assert 'API_PATH' not in explicit.schema.fields
    assert 'API_HEADERS' in fallback.schema.fields


@pytest.mark.parametrize('cycle', [False, True], ids=['missing', 'cycle'])
def test_unavailable_selector_controller_is_rejected_before_admission(selection_fixture, monkeypatch, cycle):
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from api_agent.tooling import admission
    context, base = selection_fixture
    field = FieldDeclaration('PRIMARY_IMPLEMENTATION', 'str', conditional_defaults=(
        {'when': {'PRIMARY_IMPLEMENTATION' if cycle else 'MISSING': 'rest.toml'}, 'value': 'graphql.toml'},))
    base = ConfigSchema(dict(base.fields, PRIMARY_IMPLEMENTATION=field))
    def no_factory(*args, **kwargs):
        raise AssertionError('admitted factory before controller validation')
    monkeypatch.setattr(admission, 'capture_tool_factory', no_factory)

    with pytest.raises(ToolSelectionError, match='controller'):
        plan((context, base))


def test_same_factory_closure_is_captured_once_for_distinct_instances(selection_fixture, monkeypatch):
    from api_agent.tooling import admission
    original = admission.capture_tool_factory
    calls = []
    def counted(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(admission, 'capture_tool_factory', counted)

    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))

    assert len(selected.tools) == 2
    assert len(calls) == 1
    assert selected.tools[0].admission is selected.tools[1].admission


def test_inherited_port_cannot_be_rebound_to_a_different_instance(selection_fixture):
    context, _ = selection_fixture
    node = context.base_root / 'node.md.html'
    node.write_text(node.read_text() + '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n'
        '| primary | [secondary](secondary.toml) |\n')

    with pytest.raises(ToolSelectionError, match='Inherited tool port conflicts'):
        plan(selection_fixture)


def test_long_controller_chain_hits_bound_before_python_recursion():
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from api_agent.tooling.selection import _controller_closure
    fields = {f'FIELD_{index}': FieldDeclaration(f'FIELD_{index}', 'bool',
        applies_when={f'FIELD_{index + 1}': True} if index < 1199 else {}) for index in range(1200)}

    with pytest.raises(ToolSelectionError, match='closure exceeds budget'):
        _controller_closure(ConfigSchema(fields), ('FIELD_0',))


def test_wire_paths_consume_aggregate_byte_budget():
    from api_agent.tooling.models import _wire_decode

    with pytest.raises(ToolSelectionError, match='exceeds byte budget'):
        _wire_decode({'path': 'sample'}, [0, 256 * 1024 * 1024])


def test_active_input_can_depend_on_hidden_controller(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text() + '\n[inputs.ENABLED]\ntype="bool"\ndefault=true\n'
        'applies_when={SHARED="different"}\n[inputs.DETAIL]\ntype="str"\ndefault="value"\napplies_when={ENABLED=true}\n')
    instance = context.base_root / 'primary.toml'
    instance.write_text(instance.read_text().replace('[bindings]', '[bindings]\nENABLED="PRIMARY_ENABLED"\nDETAIL="PRIMARY_DETAIL"'))

    selected = plan(selection_fixture)

    assert 'PRIMARY_ENABLED' not in selected.active_fields
    assert 'PRIMARY_DETAIL' in selected.active_fields
    assert 'PRIMARY_DETAIL' in selected.review_fields


def test_no_input_tool_adds_no_universal_endpoint_requirement(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text('version=1\ninterface="verified-read@1"\nfactory="package/factory.py:create_tool"\n')

    selected = plan(selection_fixture)

    assert len(selected.tools) == 1
    assert not selected.tools[0].bindings
    assert 'API_ENDPOINT' not in selected.schema.fields
    assert 'API_HEADERS' not in selected.schema.fields


def test_frozen_schema_metadata_cannot_change_after_admission(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('default={}', 'default={nested={key="value"}}')
        + '\n[inputs.DETAIL]\ntype="str"\ndefault="value"\napplies_when={SHARED=["shared","other"]}\n')
    instance = context.base_root / 'primary.toml'
    instance.write_text(instance.read_text().replace('[bindings]', '[bindings]\nDETAIL="PRIMARY_DETAIL"'))
    selected = plan(selection_fixture)

    with pytest.raises(TypeError):
        selected.schema.fields['API_HEADERS'].default['nested']['key'] = 'changed'
    with pytest.raises(TypeError):
        selected.schema.fields['PRIMARY_DETAIL'].applies_when['SHARED'] = ('different',)
    with pytest.raises(TypeError):
        selected.option_schema.fields['API_HEADERS'].default['nested']['key'] = 'changed'
    result = resolve_config(selected.schema, environ={}, overrides={'API_ENDPOINT': 'https://one.invalid'})
    assert not result.errors
    assert result.values['API_HEADERS'] == {'nested': {'key': 'value'}}
    assert 'PRIMARY_DETAIL' in result.active_fields
    result.values['API_HEADERS']['nested']['key'] = 'mutable resolved copy'
    assert selected.schema.fields['API_HEADERS'].default['nested']['key'] == 'value'


def test_case_slice_cannot_borrow_another_selected_case_graph_or_tool(selection_fixture):
    context, _ = selection_fixture
    write(context.base_root, 'secondary_graph.md.html', (context.base_root / 'primary_graph.md.html').read_text()
        .replace('primary_graph', 'secondary_graph').replace('primary.toml', 'secondary.toml'))
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="dual_graph.md.html"', 'entry="secondary_graph.md.html"'))
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_primary', 'base::tests/test_flow.py::test_dual'))

    primary = selected.for_case('base::tests/test_flow.py::test_primary')
    secondary = selected.for_case('base::tests/test_flow.py::test_dual')

    assert [tool.id for tool in primary.tools] == ['sample/primary']
    assert [tool.id for tool in secondary.tools] == ['sample/secondary']
    assert [definition.graph_id for definition in primary.definitions] == ['primary_graph']
    assert [definition.graph_id for definition in secondary.definitions] == ['secondary_graph']
    assert primary.operation_id == secondary.operation_id == selected.operation_id
    assert primary.descriptor_id == selected.descriptor_id
    assert primary.definition_for_case('base::tests/test_flow.py::test_primary') == primary.definitions[0]
    with pytest.raises(ToolSelectionError, match='outside the admitted'):
        primary.for_case('base::tests/test_flow.py::test_dual')
    with pytest.raises(ToolSelectionError, match='outside the admitted'):
        selected.for_case('base::tests/test_flow.py::test_forged')


def test_case_definitions_survive_worker_wire(selection_fixture):
    from api_agent.tooling.models import ToolPlan
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))

    restored = ToolPlan.from_wire(selected.to_wire())

    assert restored.case_definitions == selected.case_definitions
    assert restored.for_case('base::tests/test_flow.py::test_primary').tools[0].id == 'sample/primary'


def test_unknown_string_case_rejects_but_trusted_collected_graphless_case_is_admitted(selection_fixture):
    from api_agent.testing.collection import TestIdentity
    identity = 'base::tests/provider/test_extra.py::test_case'
    with pytest.raises(ToolSelectionError, match='no static graph association'):
        plan(selection_fixture, (identity,))

    collected = TestIdentity(identity, 'base', 'tests/provider/test_extra.py', 'tests/provider/test_extra.py::test_case')
    selected = plan(selection_fixture, (collected,))

    assert selected.case_definitions == {identity: ()}
    assert not selected.for_case(identity).tools
    with pytest.raises(ToolSelectionError, match='exactly one admitted'):
        selected.definition_for_case(identity)


def test_shared_profile_cannot_change_an_independent_case_using_same_instances(selection_fixture):
    context, _ = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        '"base::tests/test_flow.py::test_dual"=["dual"]',
        '"base::tests/test_flow.py::test_dual"={graphs=["dual"],profile="shared"}\n'
        '"base::tests/test_flow.py::test_independent"=["dual"]') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')

    with pytest.raises(ToolSelectionError, match='disagree about shared-primary'):
        plan(selection_fixture, ('base::tests/test_flow.py::test_dual', 'base::tests/test_flow.py::test_independent'))


def test_case_slice_retains_nested_declaration_and_root_relative_infrastructure_assets(selection_fixture):
    context, base = selection_fixture
    extra = schema_from_dict({'version': 1, 'fields': {'LOCAL_ENABLED': {'type': 'bool', 'default': False}}})
    selection_fixture = (context, type(base)(dict(base.fields, **extra.fields)))
    write(context.base_root, 'infra/compose.json', '{"image":"example:1"}')
    write(context.base_root, 'infra/seed.txt', 'seed')
    write(context.base_root, 'cfg/infrastructure.toml', 'version=1\nowner="sample"\n[[services]]\n'
        'id="target"\ncompose="infra/compose.json"\nassets=["infra/seed.txt"]\ndependencies=[]\n'
        'when={field="LOCAL_ENABLED",equals=true}\nreadiness={kind="health",timeout=1}\n'
        'seed={asset="infra/seed.txt",completion={kind="health"}}\n'
        'endpoints=[{name="graphql",host="http://127.0.0.1:5000",in_project="http://self:5000"}]\n')
    instance = context.base_root / 'primary.toml'
    instance.write_text(instance.read_text() + '\n[defaults]\nENDPOINT=""\n'
        '[local]\nslot="ENDPOINT"\noutput="sample/target/graphql"\npath="/graphql"\ndeclaration="cfg/infrastructure.toml"\n'
        '[local.activation]\nLOCAL_ENABLED=true\n'
        '[infrastructure]\nowner="sample"\nroot="."\ndeclarations=["cfg/infrastructure.toml"]\n')
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))
    selected = selected.bind_values(resolve_config(selected.schema, environ={}))

    scoped = selected.for_case('base::tests/test_flow.py::test_primary')

    assert len(scoped.infrastructure_contributions) == 1
    assert {context.base_root / name for name in ('cfg/infrastructure.toml', 'infra/compose.json', 'infra/seed.txt')} <= scoped.captured_assets.keys()


def test_catalog_index_shares_bodies_and_diagnoses_over_budget_unselected_graph(selection_fixture, monkeypatch):
    from api_agent.tooling import selection
    context, _ = selection_fixture
    base_catalog = discover(context)
    shared = base_catalog.definitions['primary'].contents[context.base_root / 'native.py']
    assert base_catalog.definitions['dual'].contents[context.base_root / 'native.py'] is shared
    app_size = sum(len(body) for body in base_catalog.definitions['@application'].contents.values())
    monkeypatch.setattr(selection, 'MAX_INDEX_CAPTURE_BYTES', app_size)

    bounded = discover(context)

    assert bounded.selected_graphs == ('@application',)
    assert 'dual' in bounded.diagnostics
    assert 'dual' not in bounded.definitions


@pytest.mark.parametrize('kwargs, message', [
    ({'mode': 'unknown'}, 'Unknown'),
    ({'mode': 'tests', 'test_ids': ('',)}, 'nonempty'),
    ({'mode': 'tests', 'variants': (1,)}, 'nonempty'),
    ({'mode': 'tests', 'test_ids': ('case', 'case')}, 'Duplicate'),
    ({'mode': 'tests', 'variants': ('variant', 'variant')}, 'Duplicate'),
    ({'mode': 'tests', 'graphless': ('outside',)}, 'Graphless'),
    ({'mode': 'tests', 'test_ids': ('case',), 'graphless': ('case', 'case')}, 'Graphless'),
    ({'test_ids': ('case',)}, 'Application'),
])
def test_operation_selection_rejects_ambiguous_identities(kwargs, message):
    with pytest.raises(ToolSelectionError, match=message):
        OperationSelection(**kwargs)


@pytest.mark.parametrize('mode', ['run-local', 'menu', 'application', 'unit'])
def test_string_operation_selection_preserves_mode_aliases(mode):
    from api_agent.tooling.models import operation_selection

    selected = operation_selection(mode)

    assert selected.mode == ('unit' if mode == 'unit' else 'application')


@pytest.mark.parametrize('packet', [None, {}, {'version': True, 'plan': None},
    {'version': 2, 'plan': None}, {'version': 1, 'plan': None, 'extra': 1}])
def test_worker_packet_rejects_invalid_envelope(packet):
    from api_agent.tooling.models import ToolPlan

    with pytest.raises(ToolSelectionError, match='Invalid captured tool plan packet'):
        ToolPlan.from_wire(packet)


def test_worker_packet_requires_a_tool_plan():
    from api_agent.tooling.models import ToolPlan

    with pytest.raises(ToolSelectionError, match='does not contain a tool plan'):
        ToolPlan.from_wire({'version': 1, 'plan': None})


@pytest.mark.parametrize('value, message', [
    ([], 'packet value'),
    ({'path': ''}, 'tool path'),
    ({'path': 'a/../b\n'}, 'tool path'),
    ({'path': './a'}, 'tool path'),
    ({'bytes': '!'}, 'tool bytes'),
    ({'map': [['only-key']]}, 'tool mapping'),
    ({'map': [[{'map': []}, 1]]}, 'mapping key'),
    ({'map': [['same', 1], ['same', 2]]}, 'Duplicate'),
    ({'set': [{'map': []}]}, 'set member'),
    ({'unset': False}, 'packet tag'),
    ({'class': 'OperationSelection', 'fields': {}}, 'tool model'),
    ({'class': 'OperationSelection', 'fields': {'mode': 'bad', 'test_ids': {'tuple': []},
        'variants': {'tuple': []}, 'graphless': {'tuple': []}}}, 'model fields'),
])
def test_worker_packet_rejects_malformed_typed_values(value, message):
    from api_agent.tooling.models import _wire_decode

    with pytest.raises(ToolSelectionError, match=message):
        _wire_decode(value, [0, 0])


@pytest.mark.parametrize('value, budget, depth, message', [
    (None, [300000, 0], 0, 'structural budget'),
    (None, [0, 0], 129, 'structural budget'),
    ('x', [0, 256 * 1024 * 1024], 0, 'byte budget'),
    ({'bytes': 'eA=='}, [0, 256 * 1024 * 1024], 0, 'byte budget'),
])
def test_worker_packet_enforces_aggregate_budgets(value, budget, depth, message):
    from api_agent.tooling.models import _wire_decode

    with pytest.raises(ToolSelectionError, match=message):
        _wire_decode(value, budget, depth)


def test_worker_wire_roundtrips_set_and_unset_metadata():
    from api_agent.config.declarations import UNSET
    from api_agent.tooling.models import _wire_decode, _wire_encode

    value = {'choices': frozenset({'first', 'second'}), 'default': UNSET}
    restored = _wire_decode(_wire_encode(value), [0, 0])

    assert restored['choices'] == value['choices']
    assert restored['default'] is UNSET


def test_worker_wire_rejects_arbitrary_objects():
    from api_agent.tooling.models import _wire_encode

    with pytest.raises(ToolSelectionError, match='Unsupported'):
        _wire_encode(object())


@pytest.mark.parametrize('field', [
    {'type': 'bool', 'default': True},
    {'type': 'str', 'sensitive': True},
])
def test_selector_requires_nonsecret_string_configuration(selection_fixture, field):
    context, base = selection_fixture
    extension = schema_from_dict({'version': 1, 'fields': {'PRIMARY_IMPLEMENTATION': field}})
    schema = type(base)(dict(base.fields, **extension.fields))

    with pytest.raises(ToolSelectionError, match='nonsecret string'):
        plan((context, schema))


def test_predeclared_selector_without_default_uses_instance_implementation(selection_fixture):
    context, base = selection_fixture
    extension = schema_from_dict({'version': 1, 'fields': {'PRIMARY_IMPLEMENTATION': {'type': 'str'}}})
    schema = type(base)(dict(base.fields, **extension.fields))

    selected = plan((context, schema))

    assert selected.tools[0].implementation_path == context.base_root / 'graphql.toml'


@pytest.mark.parametrize('change, message', [
    ('selector="SECONDARY_IMPLEMENTATION"', 'distinct ownership'),
    ('id="sample/secondary"', 'Conflicting named tool identity'),
])
def test_distinct_instances_cannot_share_identity_or_selector(selection_fixture, change, message):
    context, _ = selection_fixture
    path = context.base_root / 'primary.toml'
    original = 'selector="PRIMARY_IMPLEMENTATION"' if change.startswith('selector') else 'id="sample/primary"'
    path.write_text(path.read_text().replace(original, change))

    with pytest.raises(ToolSelectionError, match=message):
        plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))


@pytest.mark.parametrize('replacement, message', [
    ('interface="other@1"', 'interface is incompatible'),
    ('[inputs.ABSENT]\ntype="str"', 'Missing selected tool local input binding'),
    ('[services]\nlookup="search"', 'service capability binding'),
    ('[inputs.EXTRA]\ntype="str"\nrequired="invalid"', 'Invalid selected tool input'),
])
def test_selected_implementation_requires_compatible_bound_inputs(selection_fixture, replacement, message):
    context, _ = selection_fixture
    path = context.base_root / 'graphql.toml'
    body = path.read_text()
    if replacement.startswith('interface'):
        body = body.replace('interface="verified-read@1"', replacement)
    else:
        body += '\n' + replacement + '\n'
    path.write_text(body)
    if 'EXTRA' in replacement:
        instance = context.base_root / 'primary.toml'
        instance.write_text(instance.read_text().replace('[bindings]', '[bindings]\nEXTRA="API_EXTRA"'))

    with pytest.raises(ToolSelectionError, match=message):
        plan(selection_fixture)


def test_case_slice_rejects_profile_outside_its_admitted_tools(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))
    selected = replace(selected, case_shared_primary={
        'base::tests/test_flow.py::test_primary': {'sample/secondary': 'sample/primary'}})

    with pytest.raises(ToolSelectionError, match='crosses its admitted graph closure'):
        selected.for_case('base::tests/test_flow.py::test_primary')


@pytest.mark.parametrize('selector_field', [
    {'type': 'str', 'required': True, 'applies_when': {'SHARED': 'other'}},
    {'type': 'str', 'default': 'rest.toml', 'enum': ['rest.toml']},
])
def test_selector_must_resolve_to_an_active_valid_value(selection_fixture, selector_field):
    context, base = selection_fixture
    extension = schema_from_dict({'version': 1, 'fields': {'PRIMARY_IMPLEMENTATION': selector_field}}, base_schema=base)
    schema = type(base)(dict(base.fields, **extension.fields))

    with pytest.raises(ToolSelectionError, match='controller'):
        plan((context, schema), overrides={'PRIMARY_IMPLEMENTATION': 'graphql.toml'})


def test_selector_cannot_depend_on_sensitive_controller(selection_fixture):
    context, base = selection_fixture
    extension = schema_from_dict({'version': 1, 'fields': {
        'SECRET': {'type': 'str', 'sensitive': True},
        'PRIMARY_IMPLEMENTATION': {'type': 'str', 'applies_when': {'SECRET': 'value'}}}}, base_schema=base)

    with pytest.raises(ToolSelectionError, match='Sensitive configuration'):
        plan((context, type(base)(dict(base.fields, **extension.fields))))


@pytest.mark.parametrize('predicate', [
    {'any': [{} for _ in range(4096)]},
])
def test_selector_predicate_rejects_excessive_branch_count(predicate):
    from api_agent.tooling.selection import _predicate_fields

    with pytest.raises(ToolSelectionError, match='structural budget'):
        _predicate_fields(predicate)


def test_nested_selector_predicates_include_all_controllers():
    from api_agent.tooling.selection import _predicate_fields, _project_input
    predicate = {'any': [{'FIRST': True}, {'SECOND': False}], 'operation': 'tests'}

    assert _predicate_fields(predicate) == {'FIRST', 'SECOND'}
    assert _project_input({'applies_when': predicate, 'conditional_defaults': [
        {'when': {'any': [{'FIRST': True}]}, 'value': 'chosen'}]}, {'FIRST': 'API_FIRST'}) == {
        'applies_when': {'any': [{'API_FIRST': True}, {'SECOND': False}], 'operation': 'tests'},
        'conditional_defaults': [{'when': {'any': [{'API_FIRST': True}]}, 'value': 'chosen'}]}


def test_without_catalog_only_trusted_graphless_or_unit_cases_are_admitted(selection_fixture):
    context, _ = selection_fixture
    context = replace(context, tool_catalog_path=None)
    identity = 'base::tests/test_flow.py::test_primary'

    with pytest.raises(ToolSelectionError, match='no static graph association'):
        discover(context, (identity,))
    assert discover(context, OperationSelection('unit', (identity,))).case_graphs == {identity: ()}
    assert discover(context, OperationSelection('tests', (identity,), graphless=(identity,))).case_graphs == {identity: ()}


def test_application_graph_failure_is_reported_before_tool_admission(selection_fixture):
    context, _ = selection_fixture
    (context.base_root / 'node.md.html').unlink()

    with pytest.raises(ToolSelectionError, match='selected application graph'):
        discover(context)


def test_absent_application_entry_selects_no_tools(selection_fixture):
    context, base = selection_fixture
    context = replace(context, workflow_entry=context.base_root / 'absent.md.html')

    selected = plan((context, base))

    assert not selected.tools
    assert not selected.definitions


@pytest.mark.parametrize('field, activation, message', [
    ({'type': 'bool', 'default': False}, '"wrong"', 'incompatible controller value'),
    ({'type': 'jsonobject', 'default': {}}, '"value"', 'nonsecret scalar controller'),
    ({'type': 'str', 'sensitive': True}, '"value"', 'nonsecret scalar controller'),
])
def test_local_activation_requires_typed_nonsecret_controller(selection_fixture, field, activation, message):
    context, base = selection_fixture
    extra = schema_from_dict({'version': 1, 'fields': {'LOCAL_ENABLED': field}})
    schema = type(base)(dict(base.fields, **extra.fields))
    instance = context.base_root / 'primary.toml'
    instance.write_text(instance.read_text() + '\n[local]\nslot="ENDPOINT"\n'
        'output="sample/target/graphql"\npath="/graphql"\ndeclaration="infrastructure.toml"\n'
        '[local.activation]\nLOCAL_ENABLED=' + activation + '\n')

    with pytest.raises(ToolSelectionError, match=message):
        plan((context, schema))


def test_shared_profile_requires_selected_primary(selection_fixture):
    context, _ = selection_fixture
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="primary_graph.md.html"', 'entry="primary_graph.md.html"\nprofile="shared"') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')

    with pytest.raises(ToolSelectionError, match='unselected tool'):
        plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))


def test_nested_selector_predicate_hits_depth_budget():
    from api_agent.tooling.selection import _predicate_fields
    predicate = {'FIELD': True}
    for _ in range(65):
        predicate = {'any': [predicate]}

    with pytest.raises(ToolSelectionError, match='structural budget'):
        _predicate_fields(predicate)


def test_bind_values_rejects_explicit_controller_that_disables_local_output(selection_fixture):
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    selected = plan(selection_fixture)
    pending = {'ENDPOINT': {'activation': {'LOCAL_ENABLED': True}}}
    selected = replace(selected, tools=(replace(selected.tools[0], pending=pending),),
        schema=ConfigSchema(dict(selected.schema.fields,
            LOCAL_ENABLED=FieldDeclaration('LOCAL_ENABLED', 'bool', default=True))))
    result = resolve_config(selected.schema, environ={}, overrides={
        'API_ENDPOINT': '', 'LOCAL_ENABLED': 'False'})

    with pytest.raises(ToolSelectionError, match='Explicit controller disables'):
        selected.bind_values(result)


def test_shared_profile_projects_only_slots_present_on_both_tools(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',),
        overrides={'SECONDARY_IMPLEMENTATION': 'rest.toml'})
    selected = replace(selected, shared_primary={'sample/secondary': 'sample/primary'})
    result = resolve_config(selected.schema, environ={}, overrides={'API_ENDPOINT': 'https://one.invalid'})

    bound = selected.bind_values(result)

    assert bound.tools[1].local_values == {'ENDPOINT': 'https://one.invalid', 'PATH': '/records'}
    assert bound.tools[1].derived_from == {'ENDPOINT': 'sample/primary'}


def test_shared_profile_carries_primary_pending_output(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))
    pending = {'ENDPOINT': {'declaration': Path('infrastructure.toml')}}
    selected = replace(selected, shared_primary={'sample/secondary': 'sample/primary'},
        tools=(replace(selected.tools[0], pending=pending), selected.tools[1]))
    result = resolve_config(selected.schema, environ={}, overrides={'API_ENDPOINT': ''})

    bound = selected.bind_values(result)

    assert bound.tools[1].pending == pending
    assert bound.tools[1].local_values['ENDPOINT'] == ''


def test_shared_profile_leaves_unresolved_primary_slot_unset(selection_fixture):
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',))
    selected = replace(selected, shared_primary={'sample/secondary': 'sample/primary'})
    result = resolve_config(selected.schema, environ={})
    result = replace(result, fields={key: value for key, value in result.fields.items() if key != 'API_ENDPOINT'})

    bound = selected.bind_values(result)

    assert 'ENDPOINT' not in bound.tools[1].derived_from


def test_tool_plan_supports_worker_packet_without_option_inventory(selection_fixture):
    from api_agent.tooling.models import ToolPlan
    selected = replace(plan(selection_fixture), option_schema=None)

    restored = ToolPlan.from_wire(selected.to_wire())

    assert restored.option_schema is None
    assert restored.tools[0].id == 'sample/primary'


def test_selector_controller_dependency_can_be_shared():
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from api_agent.tooling.selection import _controller_closure
    schema = ConfigSchema({
        'COMMON': FieldDeclaration('COMMON', 'bool', default=True),
        'FIRST': FieldDeclaration('FIRST', 'str', applies_when={'COMMON': True}),
        'SECOND': FieldDeclaration('SECOND', 'str', required_when={'COMMON': True}),
    })

    assert _controller_closure(schema, ('FIRST', 'SECOND')) == {'FIRST', 'SECOND', 'COMMON'}


def test_instance_default_overrides_implementation_default(selection_fixture):
    context, _ = selection_fixture
    path = context.base_root / 'primary.toml'
    path.write_text(path.read_text() + '\n[defaults]\nHEADERS={example="value"}\n')

    selected = plan(selection_fixture)

    assert selected.schema.fields['API_HEADERS'].default == {'example': 'value'}


def test_catalog_variant_uses_declared_profile(selection_fixture):
    context, _ = selection_fixture
    path = context.tool_catalog_path
    path.write_text(path.read_text() + '\n[variants.shared]\ngraph="dual"\nreplacements=[]\nprofile="shared"\n'
        '[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')

    selected = plan(selection_fixture, OperationSelection('tests', variants=('shared',)))

    assert selected.shared_primary == {'sample/secondary': 'sample/primary'}
    assert len(selected.tools) == 2


def test_selected_profiles_cannot_derive_from_a_derived_instance(selection_fixture):
    context, _ = selection_fixture
    path = context.tool_catalog_path
    path.write_text(path.read_text().replace('entry="dual_graph.md.html"', 'entry="dual_graph.md.html"\nprofile="forward"') +
        '\n[variants.forward]\ngraph="dual"\nreplacements=[]\n[variants.reverse]\ngraph="dual"\nreplacements=[]\nprofile="reverse"\n'
        '[profiles.forward]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n'
        '[profiles.reverse]\nversion=1\nkind="shared-primary"\nprimary="sample/secondary"\nsecondary=["sample/primary"]\n')

    with pytest.raises(ToolSelectionError, match='derived chains'):
        plan(selection_fixture, OperationSelection('tests', variants=('forward', 'reverse')))


def test_selected_profiles_cannot_give_secondary_two_primaries(selection_fixture):
    context, _ = selection_fixture
    primary = context.base_root / 'primary.toml'
    write(context.base_root, 'third.toml', primary.read_text().replace('sample/primary', 'sample/third')
        .replace('PRIMARY_IMPLEMENTATION', 'THIRD_IMPLEMENTATION').replace('API_', 'THIRD_'))
    graph(context.base_root, 'triple_graph', [('primary', 'primary.toml'), ('secondary', 'secondary.toml'), ('third', 'third.toml')])
    context.tool_catalog_path.write_text('version=1\n[graphs.triple]\nroot="base"\nentry="triple_graph.md.html"\n'
        '[variants.first]\ngraph="triple"\nreplacements=[]\nprofile="first"\n'
        '[variants.second]\ngraph="triple"\nreplacements=[]\nprofile="second"\n'
        '[profiles.first]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n'
        '[profiles.second]\nversion=1\nkind="shared-primary"\nprimary="sample/third"\nsecondary=["sample/secondary"]\n')

    with pytest.raises(ToolSelectionError, match='Conflicting shared-primary profile'):
        plan(selection_fixture, OperationSelection('tests', variants=('first', 'second')))


def test_case_profile_cannot_borrow_primary_from_another_selected_case(selection_fixture):
    context, _ = selection_fixture
    graph(context.base_root, 'secondary_graph', [('secondary', 'secondary.toml')])
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text().replace(
        'entry="dual_graph.md.html"', 'entry="secondary_graph.md.html"').replace(
        '"base::tests/test_flow.py::test_dual"=["dual"]',
        '"base::tests/test_flow.py::test_dual"={graphs=["dual"],profile="shared"}') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')

    with pytest.raises(ToolSelectionError, match='outside its admitted graph closure'):
        plan(selection_fixture, ('base::tests/test_flow.py::test_primary', 'base::tests/test_flow.py::test_dual'))


def test_shared_profile_allows_secondary_without_standard_endpoint_slot(selection_fixture):
    context, _ = selection_fixture
    write(context.base_root, 'noinput.toml', 'version=1\ninterface="verified-read@1"\nfactory="package/factory.py:create_tool"\n')
    catalog = context.tool_catalog_path
    catalog.write_text(catalog.read_text().replace('entry="dual_graph.md.html"', 'entry="dual_graph.md.html"\nprofile="shared"') +
        '\n[profiles.shared]\nversion=1\nkind="shared-primary"\nprimary="sample/primary"\nsecondary=["sample/secondary"]\n')

    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_dual',),
        overrides={'SECONDARY_IMPLEMENTATION': 'noinput.toml'})

    assert selected.shared_primary == {'sample/secondary': 'sample/primary'}
    assert not selected.tools[1].bindings
    assert 'API_2_ENDPOINT' not in selected.schema.fields


def test_instance_without_selector_uses_declared_implementation(selection_fixture):
    context, _ = selection_fixture
    path = context.base_root / 'primary.toml'
    path.write_text(path.read_text().replace('selector="PRIMARY_IMPLEMENTATION"\n', ''))

    selected = plan(selection_fixture)

    assert 'PRIMARY_IMPLEMENTATION' not in selected.schema.fields
    assert selected.tools[0].implementation_path == context.base_root / 'graphql.toml'


def test_repeated_tool_use_preserves_single_instance_admission(selection_fixture):
    context, _ = selection_fixture
    graph(context.base_root, 'primary_graph', [('primary', 'primary.toml'), ('other', 'primary.toml')])

    selected = plan(selection_fixture)

    assert len(selected.tools) == 1
    assert set(selected.bindings.values()) == {'sample/primary'}
    assert {key[-1] for key in selected.bindings} == {'primary', 'other'}


def test_base_schema_keeps_ownership_of_dormant_alias_type(selection_fixture):
    context, base = selection_fixture
    extra = schema_from_dict({'version': 1, 'fields': {'API_2_ENDPOINT': {'type': 'int', 'default': 7}}})

    selected = plan((context, type(base)(dict(base.fields, **extra.fields))), ())

    assert selected.option_schema.fields['API_2_ENDPOINT'].type == 'int'
    assert selected.schema.fields['API_2_ENDPOINT'].default == 7


def test_malformed_dormant_instance_does_not_block_selected_tool(selection_fixture):
    context, _ = selection_fixture
    (context.base_root / 'secondary.toml').write_text('version=1\nid="incomplete"\n')

    selected = plan(selection_fixture)

    assert [tool.id for tool in selected.tools] == ['sample/primary']
    assert 'SECONDARY_IMPLEMENTATION' not in selected.known_fields


def test_unavailable_dormant_implementation_keeps_alias_inventory(selection_fixture):
    selected = plan(selection_fixture, (), overrides={'SECONDARY_IMPLEMENTATION': 'missing.toml'})

    assert not selected.tools
    assert selected.option_schema.fields['API_2_ENDPOINT'].type == 'str'
    assert 'API_2_ENDPOINT' not in selected.schema.fields


def test_graph_local_binding_inherits_without_a_graph_port(selection_fixture):
    context, _ = selection_fixture
    graph_path = context.base_root / 'primary_graph.md.html'
    graph_path.write_text(graph_path.read_text().replace('| primary | [instance](primary.toml) |\n', ''))
    node = context.base_root / 'node.md.html'
    node.write_text(node.read_text() + '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n'
        '| primary | [instance](primary.toml) |\n')

    selected = plan(selection_fixture)

    assert selected.tools[0].id == 'sample/primary'
    assert all(key[1] != 'graph' for key in selected.bindings)


def test_local_output_for_inactive_implementation_slot_is_not_captured(selection_fixture):
    context, _ = selection_fixture
    instance = context.base_root / 'primary.toml'
    instance.write_text(instance.read_text() + '\n[local]\nslot="PATH"\n'
        'output="sample/target/graphql"\npath="/graphql"\ndeclaration="absent.toml"\n')

    selected = plan(selection_fixture)

    assert not selected.tools[0].pending
    assert context.base_root / 'absent.toml' not in selected.captured_assets


@pytest.mark.parametrize('owner', ['base', 'pack'])
def test_catalog_resource_root_applies_its_own_library_inventory(selection_fixture, tmp_path, owner):
    context, _ = selection_fixture
    pack_root = tmp_path.parent / (tmp_path.name + '-pack')
    pack_root.mkdir()
    root = context.base_root if owner == 'base' else pack_root
    nested = root / 'nested'
    for relative in ('native.py', 'node.md.html', 'primary_graph.md.html', 'primary.toml',
                     'graphql.toml', 'package/factory.py'):
        write(nested, relative, (context.base_root / relative).read_text())
    catalog = context.tool_catalog_path
    catalog.write_text(f'version=1\n[graphs.nested]\nroot="{owner}"\n'
        'resource_root="nested"\nentry="primary_graph.md.html"\n'
        '[test_cases]\n"base::tests/test_flow.py::test_primary"=["nested"]\n')
    inventory = frozenset(path.relative_to(context.base_root).as_posix()
        for path in context.base_root.rglob('*') if path.is_file())
    context = replace(context, pack_root=pack_root, workflow_library_files=inventory)

    selected = discover(context, ('base::tests/test_flow.py::test_primary',))

    assert selected.selected_graphs == ('nested',)
    assert nested / 'native.py' in selected.assets
    assert selected.definitions['nested'].graph_id == 'primary_graph'


def test_catalog_discovery_diagnoses_resource_changes_between_graphs(selection_fixture, monkeypatch):
    from api_agent.tooling import selection
    context, _ = selection_fixture
    original = selection.load_selected_definition
    calls = []

    def load_then_change_resource(current):
        definition = original(current)
        calls.append(definition)
        if len(calls) == 1:
            (context.base_root / 'native.py').write_text('raise AssertionError("changed workflow")\n')
        return definition

    monkeypatch.setattr(selection, 'load_selected_definition', load_then_change_resource)

    catalog = discover(context)

    assert catalog.selected_graphs == ('@application',)
    assert catalog.diagnostics['primary'] == 'Cannot admit catalog graph: primary'
    assert catalog.diagnostics['dual'] == 'Cannot admit catalog graph: dual'
    assert 'primary' not in catalog.definitions


def test_factory_license_asset_is_captured_with_selected_admission(selection_fixture):
    context, _ = selection_fixture
    license_path = write(context.base_root, 'package/LICENSE.txt', 'Synthetic fixture notice.\n')

    selected = plan(selection_fixture)

    assert selected.captured_assets[license_path] == b'Synthetic fixture notice.\n'
    assert selected.tools[0].admission.assets[license_path] == b'Synthetic fixture notice.\n'


def test_dormant_unbound_implementation_input_does_not_create_a_global_alias(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'rest.toml'
    implementation.write_text(implementation.read_text() + '\n[inputs.UNBOUND]\ntype="str"\n')

    selected = plan(selection_fixture, (), overrides={'SECONDARY_IMPLEMENTATION': 'rest.toml'})

    assert 'UNBOUND' not in selected.option_schema.fields
    assert 'API_2_PATH' in selected.option_schema.fields


def test_component_tool_port_is_inherited_by_child_node(selection_fixture):
    context, _ = selection_fixture
    write(context.base_root, 'group.md.html', '# Component v3: group\n\n## Flow\n\n'
        '1.  [node inspect](node.md.html)\n')
    path = context.base_root / 'primary_graph.md.html'
    path.write_text(path.read_text().replace('[node inspect](node.md.html)', '[component group](group.md.html)')
        .replace('| inspect | END | - | - |', '| group | END | - | - |\n| inspect | END | - | - |'))

    selected = plan(selection_fixture)

    assert selected.bindings[(selected.definitions[0].definition_id, 'component', 'group', 'primary')] == 'sample/primary'
    assert selected.bindings[(selected.definitions[0].definition_id, 'node', 'inspect', 'primary')] == 'sample/primary'


def test_dormant_malformed_enum_keeps_only_conservative_alias_arity(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'rest.toml'
    implementation.write_text(implementation.read_text().replace('default="/records"', 'enum=1'))

    selected = plan(selection_fixture, (), overrides={'SECONDARY_IMPLEMENTATION': 'rest.toml'})

    assert selected.option_schema.fields['API_2_PATH'].type == 'str'
    assert not selected.option_schema.fields['API_2_PATH'].enum
    assert 'API_2_PATH' not in selected.schema.fields


def test_inline_prompt_stays_in_definition_without_becoming_a_filesystem_asset(selection_fixture):
    from api_agent.workflows.definition import InlinePromptRef
    context, base = selection_fixture
    write(context.base_root, 'schema.json', '{"type":"object"}')
    write(context.base_root, 'node.md.html', '# Node v2: inspect\n\n## Native\n\n'
        '| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| inspect | deterministic | [source](native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| inspect | inspect | [input](schema.json) | [output](schema.json) |\n\n'
        '## Prompt: greeting\n\n```text\nHello.\n```\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n| inspect | @greeting | always |\n')
    entry = write(context.base_root, 'inline.md.html', '# Workflow v2\n\n## Graph ID\n\n'
        '| ID |\n| --- |\n| inline_graph |\n\n## Flow\n\n1.  [node inspect](node.md.html)\n')
    context = replace(context, workflow_entry=entry, tool_catalog_path=None)

    selected = plan((context, base))

    prompts = {key: body for key, body in selected.definitions[0].contents.items() if isinstance(key, InlinePromptRef)}
    assert list(prompts.values()) == [b'Hello.\n']
    assert not prompts.keys() & selected.captured_assets.keys()
