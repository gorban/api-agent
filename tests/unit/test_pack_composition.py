"""Ordered composition exercises actual launcher and configuration boundaries."""
from dataclasses import replace
from pathlib import Path

import pytest

from api_agent.config import load_shared_schema, resolve_config
from api_agent.runtime.bootstrap import _extract_option
from api_agent.runtime.pack_composition import load_context_schema, validate_replacements
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context, load_selected_definition

BASE = Path(__file__).resolve().parents[2]


def pack(tmp_path, name, *, manifest='', config='version=1\n'):
    root = tmp_path / name
    root.mkdir()
    (root / 'config.toml').write_text(config)
    (root / 'pack.toml').write_text(
        f'version=1\nid="{name}"\nconfig="config.toml"\n' + manifest)
    return root


def test_repeatable_bootstrap_options_preserve_order_and_separator():
    rest, selected = _extract_option(
        ['--resource-pack-dir', 'first', '--debug', '--resource-pack-dir=second', '--', '--resource-pack-dir=ignored'],
        {'--resource-pack-dir'}, multiple=True)
    assert selected == ['first', 'second']
    assert rest == ['--debug', '--', '--resource-pack-dir=ignored']


@pytest.mark.parametrize('declaration', [
    {}, 1, {'version': True, 'replace': []}, {'version': 2, 'replace': []},
    {'version': 1, 'replace': 'ui'}, {'version': 1, 'replace': [1]},
    {'version': 1, 'replace': ['unknown']}, {'version': 1, 'replace': ['ui', 'ui']},
    {'version': 1, 'replace': [], 'extra': 1},
])
def test_invalid_replacement_declarations_fail_closed(declaration):
    with pytest.raises(ResourcePackError, match='composition'):
        validate_replacements(declaration)


def test_valid_replacement_declarations_are_inert():
    assert validate_replacements(None) == ()
    assert validate_replacements({'version': 1, 'replace': []}) == ()
    assert validate_replacements({'version': 1, 'replace': ['ui', 'api', 'workflow']}) == ('ui', 'api', 'workflow')


def test_empty_duplicate_and_conflicting_identity_pack_selection_rejected(tmp_path):
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    with pytest.raises(ResourcePackError, match='empty'):
        load_launch_context(BASE, [])
    with pytest.raises(ResourcePackError, match='physical roots'):
        load_launch_context(BASE, [first, first])
    (second / 'pack.toml').write_text((first / 'pack.toml').read_text())
    with pytest.raises(ResourcePackError, match='unique ids'):
        load_launch_context(BASE, [first, second])


def test_singleton_selection_preserves_existing_contract(tmp_path):
    first = pack(tmp_path, 'first')
    context = load_launch_context(BASE, [first])
    assert context == load_launch_context(BASE, first)
    assert load_context_schema(context, load_shared_schema(BASE)) == load_context_schema(
        load_launch_context(BASE, first), load_shared_schema(BASE))


def test_ordered_configuration_and_resources_keep_each_owner(tmp_path):
    first = pack(tmp_path, 'first', config='''version=1
[fields.FIRST_SOURCE]
type="path"
anchor="pack_root"
default="assets"
[fields.EXTRA_FLAG]
type="bool"
default=true
''', manifest='''production=["src"]
[providers]
configs=["provider.toml"]
[configuration]
dotenv=["shared.env"]
[tests]
unit=["tests/unit"]
e2e=["tests/e2e"]
[defaults]
UI_NX_APP="agent"
UI_PROVIDER="ui.py:create_provider"
API_PROVIDER="api.py:create_provider"
''')
    (first / 'src').mkdir()
    (first / 'provider.toml').write_text('version=1\n')
    second = pack(tmp_path, 'second', config='''version=1
[fields.EXTRA_FLAG]
type="bool"
override=true
default=false
''', manifest='''[composition]
version=1
replace=["ui","api"]
[defaults]
UI_NX_APP="shell"
UI_PROVIDER="shell.py:create_provider"
API_PROVIDER="product.py:create_provider"
[tests]
unit=["tests/unit"]
integration=["tests/integration"]
''')
    context = load_launch_context(BASE, [first, second])
    schema = load_context_schema(context, load_shared_schema(BASE))
    assert schema.fields['FIRST_SOURCE'].default == str(first / 'assets')
    assert schema.fields['EXTRA_FLAG'].default is False
    assert schema.fields['UI_PROVIDER'].default == str(second / 'shell.py') + ':create_provider'
    assert schema.fields['API_PROVIDER'].default == str(second / 'product.py') + ':create_provider'
    assert schema.fields['UI_NX_APP'].default == 'shell'
    assert context.provider_config_paths == (first / 'provider.toml',)
    assert context.production_paths == (first / 'src',)
    assert context.shared_dotenv_paths == (first / 'shared.env', first / '.env', second / '.env')
    assert context.suite_paths('unit') == (BASE / 'tests/unit', first / 'tests/unit', second / 'tests/unit')
    assert context.suite_paths('e2e') == (first / 'tests/e2e',)
    assert context.suite_paths('integration') == (second / 'tests/integration',)
    assert context.infrastructure_sources == ((first, ()), (second, ()))
    assert context.defaults['UI_NX_APP'] == 'shell'
    result = resolve_config(schema, argv=['--extra-flag=True'], environ={}, dotenv={}, anchors=context.anchors, interactive=True)
    assert result.values['EXTRA_FLAG'] is True


@pytest.mark.parametrize('field,responsibility', [('UI_PROVIDER', 'ui'), ('API_PROVIDER', 'api')])
@pytest.mark.parametrize('source', ['defaults', 'fields'])
def test_competing_responsibility_requires_explicit_replacement(tmp_path, field, responsibility, source):
    first = pack(tmp_path, 'first', manifest=f'[defaults]\n{field}="first.py:factory"\n')
    if source == 'defaults':
        second = pack(tmp_path, 'second', manifest=f'[defaults]\n{field}="second.py:factory"\n')
    else:
        second = pack(tmp_path, 'second', config=f'version=1\n[fields.{field}]\ntype="str"\noverride=true\ndefault="second.py:factory"\n')
    with pytest.raises(ResourcePackError, match=f'{responsibility} replacement must be explicit'):
        load_launch_context(BASE, [first, second])


@pytest.mark.parametrize('catalog_count', [0, 1, 2])
def test_catalog_owner_is_preserved_or_rejected(tmp_path, catalog_count):
    first = pack(tmp_path, 'first', manifest='[tooling]\nversion=1\ncatalog="tools.toml"\n' if catalog_count else '')
    second = pack(tmp_path, 'second', manifest='[tooling]\nversion=1\ncatalog="tools.toml"\n' if catalog_count == 2 else '')
    for root in (first, second):
        (root / 'tools.toml').write_text('version=1\n')
    if catalog_count == 2:
        with pytest.raises(ResourcePackError, match='Multiple tool catalog'):
            load_launch_context(BASE, [first, second])
    else:
        context = load_launch_context(BASE, [first, second])
        assert context.tool_catalog_path == (first / 'tools.toml' if catalog_count else None)


def workflow(root):
    (root / 'flow.md.html').write_text((BASE / 'flow.md.html').read_text())
    with (root / 'pack.toml').open('a') as stream:
        stream.write('[workflow]\nversion=2\nroot="base"\nentry="flow.md.html"\n')


def test_workflow_owner_inherits_and_explicit_replacement_uses_existing_compiler(tmp_path):
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    workflow(first)
    inherited = load_launch_context(BASE, [first, second])
    assert inherited.workflow_pack_root == first
    assert inherited.workflow_entry == BASE / 'flow.md.html'
    assert load_selected_definition(inherited).roots['pack'] == first
    workflow(second)
    with pytest.raises(ResourcePackError, match='workflow replacement must be explicit'):
        load_launch_context(BASE, [first, second])
    with (second / 'pack.toml').open('a') as stream:
        stream.write('[composition]\nversion=1\nreplace=["workflow"]\n')
    selected = load_launch_context(BASE, [first, second])
    assert selected.workflow_pack_root == second
    assert load_selected_definition(selected).roots['pack'] == second


def test_cross_pack_tool_and_workflow_ownership_is_rejected(tmp_path):
    first = pack(tmp_path, 'first', manifest='[tooling]\nversion=1\ncatalog="tools.toml"\n')
    (first / 'tools.toml').write_text('version=1\n')
    second = pack(tmp_path, 'second')
    workflow(second)
    with pytest.raises(ResourcePackError, match='share one pack owner'):
        load_launch_context(BASE, [first, second])


def test_conditional_defaults_keep_original_owner(tmp_path):
    first = pack(tmp_path, 'first', config='''version=1
[fields.ROOTED]
type="path"
anchor="pack_root"
conditional_defaults=[{when={DEBUG=true},value="conditional"}]
[fields.API_PROVIDER]
type="str"
override=true
conditional_defaults=[{when={DEBUG=true},value="api.py:factory"}]
''')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    schema = load_context_schema(context, load_shared_schema(BASE))
    assert schema.fields['ROOTED'].conditional_defaults[0]['value'] == str(first / 'conditional')
    assert schema.fields['API_PROVIDER'].conditional_defaults[0]['value'] == str(first / 'api.py') + ':factory'


def test_provider_configuration_replacement_cannot_bypass_composition_gate(tmp_path):
    first = pack(tmp_path, 'first', manifest='[defaults]\nUI_PROVIDER="first.py:factory"\n')
    second = pack(tmp_path, 'second', manifest='[providers]\nconfigs=["ui.toml"]\n')
    (second / 'ui.toml').write_text('version=1\n[fields.UI_PROVIDER]\ntype="str"\noverride=true\ndefault="second.py:factory"\n')
    with pytest.raises(ResourcePackError, match='ui replacement must be explicit'):
        load_launch_context(BASE, [first, second])


@pytest.mark.parametrize('replace_identity', [False, True])
def test_inherited_identity_load_uses_declaring_pack(tmp_path, replace_identity):
    from api_agent.auth.extensions import load_identity_adapter
    identity_manifest = '''production=["src"]
[authentication.identity]
version=1
path="src/identity.py"
factory="factory"
'''
    first = pack(tmp_path, 'first', manifest=identity_manifest)
    second = pack(tmp_path, 'second', manifest=identity_manifest if replace_identity else '')
    source = '''class Adapter:
    version=1
    async def resolve_request(self, *, authorization, cookies): pass
    async def login_hint(self, principal): pass
    async def match_llm_identity(self, principal, llm_auth_context): pass
def factory(config):
    return Adapter()
'''
    for root in (first, second):
        (root / 'src').mkdir()
        (root / 'src/identity.py').write_text(source)
    context = load_launch_context(BASE, [first, second])
    owner = second if replace_identity else first
    assert context.identity_factory_path == owner / 'src/identity.py'
    assert context.identity_pack_root == owner
    assert load_identity_adapter(context, {}).version == 1


def test_pack_dependencies_resolve_local_sources_without_registry_lookup(tmp_path):
    from api_agent.runtime.dependencies import requirements
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    (first / 'pyproject.toml').write_text('''[project]
name="example-foundation"
version="1.2.0"
dependencies=["external-a>=1"]
[project.optional-dependencies]
extra=["external-b>=2"]
''')
    (second / 'pyproject.toml').write_text('''[project]
name="example-application"
version="1.0.0"
dependencies=["example-foundation[extra]>=1", "external-c>=3"]
''')
    context = load_launch_context(BASE, [first, second])
    wanted = requirements(context)
    assert 'external-a>=1' in wanted
    assert 'external-b>=2' in wanted
    assert 'external-c>=3' in wanted
    assert not any(item.startswith('example-foundation') for item in wanted)


def test_launcher_help_consumes_all_pack_configuration_layers(tmp_path, monkeypatch, capsys):
    from api_agent.runtime.bootstrap import main
    first = pack(tmp_path, 'first', config='version=1\n[fields.FIRST_OPTION]\ntype="bool"\ndefault=false\n')
    second = pack(tmp_path, 'second')
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', lambda *args: None)
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *args, **kwargs: None)
    monkeypatch.setattr('os.environ', {})
    result = main(['--resource-pack-dir', str(first), '--resource-pack-dir', str(second),
                   '--first-option=True', '--help'], base_root=BASE)
    assert result == 0
    output = capsys.readouterr()
    assert not output.err
    assert str(first.name + '/config.toml') in output.out
    assert str(second.name + '/config.toml') in output.out


def test_pack_launcher_and_interpreter_handoff_preserve_order(tmp_path, monkeypatch):
    from api_agent.runtime.bootstrap import resource_pack_main
    from api_agent.runtime.dependencies import _relaunch
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    calls = []
    monkeypatch.setattr('os.environ', {'API_AGENT_DIR': str(BASE)})
    monkeypatch.setattr('subprocess.call', lambda args, **kwargs: calls.append(args) or 0)
    assert resource_pack_main(['--resource-pack-dir', str(first), '--resource-pack-dir', str(second), '--help'], launcher_root=second) == 0
    expected = ['--resource-pack-dir', str(first), '--resource-pack-dir', str(second)]
    assert calls[0][2:6] == expected
    context = load_launch_context(BASE, [first, second], launcher_root=second)
    assert _relaunch(context, '/synthetic/python', ['--help']) == 0
    command = calls[-1]
    assert command[command.index('--resource-pack-dir'):command.index('--resource-pack-dir') + 4] == expected


def test_cost_identity_includes_every_pack_manifest(tmp_path):
    from api_agent.testing.cost_context import cost_config
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    before = cost_config(context, {})['_COST_PACK_IDENTITY']
    assert before
    with (first / 'pack.toml').open('a') as stream:
        stream.write('# changed contribution\n')
    assert cost_config(context, {})['_COST_PACK_IDENTITY'] != before
    (first / 'pack.toml').unlink()
    assert cost_config(context, {})['_COST_PACK_IDENTITY'] is None


def test_composed_infrastructure_and_inventory_keep_original_roots(tmp_path):
    from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions, InfrastructureDeclarationError
    from api_agent.runtime.inventory import inventory_authority
    first = pack(tmp_path, 'first', manifest='[infrastructure]\nversion=1\ndeclarations=["infra.toml"]\n')
    second = pack(tmp_path, 'second', manifest='[infrastructure]\nversion=1\ndeclarations=["infra.toml"]\n')
    for root in (first, second):
        (root / 'infra.toml').write_text(f'version=1\nowner="{root.name}"\n[[services]]\nid="api"\ncompose="compose.json"\nreadiness={{kind="health"}}\n')
        (root / 'compose.json').write_text('{"image":"example/service:1"}')
    context = load_launch_context(BASE, [first, second])
    services = load_infrastructure_contributions(context)
    assert [service.id for service in services] == ['first/api', 'second/api']
    assert [service.source for service in services] == [first / 'infra.toml', second / 'infra.toml']
    assert inventory_authority(context) == [
        {'owner': 'first', 'root': str(first), 'path': 'infra.toml'},
        {'owner': 'second', 'root': str(second), 'path': 'infra.toml'}]
    (first / 'infra.toml').write_text((first / 'infra.toml').read_text().replace('owner="first"', 'owner="other"'))
    with pytest.raises(InfrastructureDeclarationError, match='owner differs'):
        load_infrastructure_contributions(context)


def test_explicit_view_selection_keeps_inherited_workflow_pack_root(tmp_path):
    from api_agent.runtime.view_flow import _view_context
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    with (first / 'pack.toml').open('a') as stream:
        stream.write('[workflow]\nversion=2\nroot="pack"\nentry="selected.md.html"\n')
    (first / 'selected.md.html').write_text('# Workflow v3\n')
    (first / 'other.md.html').write_text('# Workflow v3\n')
    context = load_launch_context(BASE, [first, second])
    assert _view_context(context, first / 'selected.md.html') == context
    other = _view_context(context, first / 'other.md.html')
    assert other.workflow_root == 'pack'
    assert other.workflow_pack_root == first
    assert other.workflow_entry == first / 'other.md.html'


def test_distinct_source_roots_cannot_claim_same_distribution(tmp_path):
    from api_agent.runtime.dependencies import requirements
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    for root in (first, second):
        (root / 'pyproject.toml').write_text('[project]\nname="example-common"\nversion="1.0.0"\n')
    with pytest.raises(ValueError, match='conflicting distribution identities'):
        requirements(load_launch_context(BASE, [first, second]))


def test_configuration_only_layers_add_no_distribution_dependencies(tmp_path):
    from api_agent.runtime.dependencies import requirements

    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    (first / 'pyproject.toml').write_text('[tool.example]\nenabled=true\n')

    composed = requirements(load_launch_context(BASE, [first, second]))
    final_only = requirements(load_launch_context(BASE, second))

    assert composed == final_only


def test_separate_provider_suite_keeps_its_repository_label(tmp_path):
    from api_agent.testing.collection import repository_label

    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    provider = tmp_path / 'independent-provider'
    provider.mkdir()
    context = load_launch_context(BASE, [first, second])

    assert repository_label(context, first) == 'pack:0:first'
    assert repository_label(context, provider) == 'independent-provider'


@pytest.mark.parametrize('field,spec', [
    ('ASSET_ROOT', 'type="path"\nanchor="owner_root"\ndefault="assets"'),
    ('ASSET_ROOT', 'type="path"\nanchor="pack_root"\nconditional_defaults=[{when={DEBUG=true},value="assets"}]'),
    ('LLM_PROVIDER', 'type="str"\ndefault="provider.py:factory"'),
    ('ACCESS_PROVIDER', 'type="str"\nconditional_defaults=[{when={DEBUG=true},value="provider.py:factory"}]'),
])
def test_ambiguous_inherited_auxiliary_defaults_fail_before_startup(tmp_path, field, spec):
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text(f'version=1\n[fields.{field}]\n{spec}\n')
    with pytest.raises(ResourcePackError, match='Inherited auxiliary configuration has an owner-relative default'):
        load_launch_context(BASE, [first, second])


def test_owner_independent_auxiliary_defaults_are_retained(tmp_path):
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('''version=1
[fields.ABSOLUTE_ROOT]
type="path"
anchor="owner_root"
default="/example/assets"
[fields.EMPTY_ROOT]
type="path"
anchor="owner_root"
allow_empty=true
default=""
[fields.BASE_ROOT]
type="path"
anchor="base_root"
default="assets"
[fields.FLAG]
type="bool"
default=true
[fields.TEXT]
type="str"
default="literal"
[fields.API_PROVIDER]
type="str"
default="/example/provider.py:factory"
''')
    context = load_launch_context(BASE, [first, second])
    assert context.provider_config_paths == (first / 'provider.toml',)


@pytest.mark.parametrize('declaration', [
    'fields=1', 'fields={FIELD=1}',
    'fields={FIELD={type="str",conditional_defaults=1}}',
    'fields={FIELD={type="str",conditional_defaults=[1]}}',
    'fields={FIELD={type="str",conditional_defaults=[{when={DEBUG=true}}]}}',
])
def test_malformed_inherited_auxiliary_metadata_is_rejected(tmp_path, declaration):
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('version=1\n' + declaration + '\n')
    with pytest.raises(ResourcePackError, match='Auxiliary'):
        load_launch_context(BASE, [first, second])


def test_ordered_source_paths_are_admitted_and_deduplicated(tmp_path):
    from api_agent.testing.pack_sources import composition_roots, composition_pythonpath
    import os
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    assert composition_roots(context) == (BASE, first, second)
    paths = composition_pythonpath(context, '/external/source').split(os.pathsep)
    assert paths == [str(BASE / 'src'), str(BASE), str(first), str(first / 'src'),
                     str(second), str(second / 'src'), '/external/source']
    assert len(paths) == len(set(paths))


def test_canonical_runner_collects_and_executes_all_ordered_source_owners(tmp_path, monkeypatch):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.runner import run_unit_tests, _production_sources
    import sys
    base = tmp_path / 'base'
    (base / 'tests/unit').mkdir(parents=True)
    (base / 'tests/unit/test_base.py').write_text('def test_base():\n    assert True\n')
    first = pack(tmp_path, 'first', manifest='production=["example_earlier_flat"]\n[tests]\nunit=["tests/unit"]\n')
    second = pack(tmp_path, 'second', manifest='production=["example_later_flat"]\n[tests]\nunit=["tests/unit"]\n')
    for root, package_name, value in ((first, 'example_earlier_package', 1), (second, 'example_later_package', 2)):
        (root / 'src' / package_name).mkdir(parents=True)
        (root / 'src' / package_name / '__init__.py').write_text(f'VALUE={value}\n')
        (root / 'tests/unit').mkdir(parents=True)
        (root / 'tests/unit' / f'test_{root.name}.py').write_text('''from example_earlier_package import VALUE as EARLIER
from example_later_package import VALUE as LATER
from example_earlier_flat import VALUE as EARLIER_FLAT
from example_later_flat import VALUE as LATER_FLAT
def test_composed_imports():
    assert (EARLIER, LATER, EARLIER_FLAT, LATER_FLAT) == (1, 2, 3, 4)
''')
    (first / 'example_earlier_flat').mkdir()
    (first / 'example_earlier_flat/__init__.py').write_text('VALUE=3\n')
    (second / 'example_later_flat').mkdir()
    (second / 'example_later_flat/__init__.py').write_text('VALUE=4\n')
    monkeypatch.setenv('PYTHONPATH', str(BASE / 'src'))
    original_paths = list(sys.path)
    context = load_launch_context(base, [first, second])
    identities = collect_tests(context, 'unit')
    assert {identity.repo for identity in identities} == {'base', 'pack:0:first', 'second'}
    assert set(_production_sources(context, first)) == {first / 'src', first / 'example_earlier_flat'}
    assert set(_production_sources(context, second)) == {second / 'src', second / 'example_later_flat'}
    assert set(_production_sources(context, first)).isdisjoint(_production_sources(context, second))
    result = run_unit_tests(context, identities)
    assert result.exit_code == 0, result.output
    assert len(result.tests) == 3
    assert all(test.status == 'passed' for test in result.tests)
    assert all(area.complete for area in result.coverage)
    assert {area.repo for area in result.coverage} == {'pack:0:first', 'second'}
    assert sys.path == original_paths


def test_controlled_integration_workers_import_every_ordered_pack_owner(tmp_path, monkeypatch):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    import sys
    base = tmp_path / 'base'
    base.mkdir()
    first = pack(tmp_path, 'first', manifest='[tests]\nintegration=["tests/integration"]\n')
    middle = pack(tmp_path, 'middle', manifest='[tests]\nintegration=["tests/integration"]\n')
    second = pack(tmp_path, 'second', manifest='[tests]\nintegration=["tests/integration"]\n')
    for root, name, value in ((first, 'example_earlier_integration', 1), (middle, 'example_middle_integration', 2), (second, 'example_later_integration', 3)):
        (root / name).mkdir()
        (root / name / '__init__.py').write_text(f'VALUE={value}\n')
        (root / 'src' / (name + '_src')).mkdir(parents=True)
        (root / 'src' / (name + '_src') / '__init__.py').write_text(f'VALUE={value + 3}\n')
        (root / 'tests/integration').mkdir(parents=True)
        (root / 'tests/integration' / f'test_{root.name}.py').write_text('''from example_earlier_integration import VALUE as EARLIER
from example_middle_integration import VALUE as MIDDLE
from example_later_integration import VALUE as LATER
from example_earlier_integration_src import VALUE as EARLIER_SOURCE
from example_middle_integration_src import VALUE as MIDDLE_SOURCE
from example_later_integration_src import VALUE as LATER_SOURCE
def test_composed_integration_imports():
    assert (EARLIER, MIDDLE, LATER, EARLIER_SOURCE, MIDDLE_SOURCE, LATER_SOURCE) == (1, 2, 3, 4, 5, 6)
''')
    monkeypatch.setenv('PYTHONPATH', str(BASE / 'src'))
    original_paths = list(sys.path)
    context = load_launch_context(base, [first, middle, second])
    identities = collect_tests(context, 'integration')
    assert {identity.repo for identity in identities} == {'pack:0:first', 'pack:1:middle', 'second'}
    result = run_integration_tests(context, identities, accepted=True)
    assert result.exit_code == 0, result.output
    assert len(result.tests) == 3
    assert all(test.status == 'passed' for test in result.tests)
    assert sys.path == original_paths


def test_composed_home_relative_defaults_match_single_pack_resolution(tmp_path, monkeypatch):
    from api_agent.providers.loading import provider_location
    synthetic_home = tmp_path / 'synthetic-home'
    synthetic_home.mkdir()
    (synthetic_home / 'factory.py').write_text('def factory(config):\n    return None\n')
    monkeypatch.setattr('os.path.expanduser', lambda value: value.replace('~', str(synthetic_home), 1))
    first = pack(tmp_path, 'first', config='''version=1
[fields.HOME_ASSETS]
type="path"
anchor="pack_root"
default="~/assets"
[fields.HOME_CONDITIONAL]
type="path"
anchor="owner_root"
conditional_defaults=[{when={DEBUG=true},value="~/conditional"}]
[fields.API_PROVIDER]
type="str"
override=true
default="~/factory.py:factory"
[fields.ACCESS_PROVIDER]
type="str"
override=true
conditional_defaults=[{when={DEBUG=true},value="~/factory.py:factory"}]
''')
    second = pack(tmp_path, 'second')
    shared = load_shared_schema(BASE)
    singular = load_launch_context(BASE, first)
    composed = load_launch_context(BASE, [first, second])
    singular_schema = load_context_schema(singular, shared)
    composed_schema = load_context_schema(composed, shared)
    singular_values = resolve_config(singular_schema, argv=['--debug=True'], environ={}, dotenv={}, anchors=singular.anchors, interactive=True).values
    composed_values = resolve_config(composed_schema, argv=['--debug=True'], environ={}, dotenv={}, anchors=composed.anchors, interactive=True).values
    assert singular_values['HOME_ASSETS'] == composed_values['HOME_ASSETS'] == synthetic_home / 'assets'
    assert singular_values['HOME_CONDITIONAL'] == composed_values['HOME_CONDITIONAL'] == synthetic_home / 'conditional'
    assert provider_location(singular_values['API_PROVIDER'], owner_root=first) == provider_location(composed_values['API_PROVIDER'], owner_root=second) == (synthetic_home / 'factory.py', 'factory')
    assert provider_location(singular_values['ACCESS_PROVIDER'], owner_root=first) == provider_location(composed_values['ACCESS_PROVIDER'], owner_root=second) == (synthetic_home / 'factory.py', 'factory')
