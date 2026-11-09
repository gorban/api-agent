"""Catalog-only libraries do not select or relocate the application workflow."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.resource_pack import load_launch_context, ResourcePackError
from api_agent.tooling.selection import discover
from api_agent.tooling.models import OperationSelection
from api_agent.testing.workflow_admission import capture_workflow_admissions
from test_tool_catalog_pack_guards import pack_at
from test_tool_selection import graph, write


@pytest.fixture
def catalog_library(tmp_path):
    pack = pack_at(tmp_path / 'pack', '[tooling]\nversion=1\ncatalog="catalog.toml"\n'
        '[tooling.library]\nsource="../library"\ndistribution="synthetic-library"\nresource="workflows"\n')
    library = tmp_path / 'library'
    library.mkdir()
    for root in (tmp_path / 'base', library):
        write(root, 'native.py', 'raise AssertionError("discovery imported native")\n')
        write(root, 'node.md.html', '# Node: inspect\n\n## Native\n\n| Registry | Kind | Source |\n'
            '| --- | --- | --- |\n| inspect | deterministic | [native](native.py) |\n')
    graph(library, 'catalog_graph', [])
    (pack / 'catalog.toml').write_text('version=1\n[graphs.private]\nroot="base"\nentry="catalog_graph.md.html"\n'
        '[test_cases]\n"pack::test.py::test_owned"=["private"]\n')
    return tmp_path / 'base', pack, library


@pytest.mark.parametrize('application_exists', [False, True])
def test_catalog_library_preserves_application_and_supplies_exact_case_admission(catalog_library, application_exists):
    base, pack, library = catalog_library
    if application_exists:
        graph(base, 'flow', [])
    context = load_launch_context(base, pack)
    assert context.workflow_entry is None and context.workflow_base_root is None
    assert context.tooling_base_root == library
    assert context.tooling_library_files is None
    application = discover(context, OperationSelection('application'))
    assert application.selected_graphs == (('@application',) if application_exists else ())
    case = 'pack::test.py::test_owned'
    catalog = discover(context, OperationSelection('tests', (case,)))
    assert catalog.roots['base'] == library
    definition = catalog.definitions['private']
    assert definition.graph_id == 'catalog_graph'
    plan = SimpleNamespace(definitions=(definition,), case_definitions={case: (definition.definition_id,)})
    admissions = capture_workflow_admissions(context, tool_plan=plan, profiles={case: {'config': {}}})
    assert set(admissions) == {(case, definition.definition_id)}
    assert admissions[(case, definition.definition_id)]['graph_id'] == 'catalog_graph'


@pytest.mark.parametrize('change', ['shape', 'escape', 'symlink'])
def test_catalog_library_reuses_existing_ownership_validation(catalog_library, change):
    base, pack, library = catalog_library
    manifest = pack / 'pack.toml'
    if change == 'shape':
        manifest.write_text(manifest.read_text().replace('distribution="synthetic-library"', 'unexpected="synthetic-library"'))
    elif change == 'escape':
        manifest.write_text(manifest.read_text().replace('source="../library"', 'source="../../outside"'))
    else:
        library.rename(library.with_name('real-library'))
        library.symlink_to(library.with_name('real-library'), target_is_directory=True)
    with pytest.raises(ResourcePackError, match='library'):
        load_launch_context(base, pack)


@pytest.mark.parametrize('application_exists', [False, True])
def test_ordered_packs_keep_library_with_catalog_owner(catalog_library, application_exists):
    base, pack, library = catalog_library
    if application_exists:
        graph(base, 'flow', [])
    other = pack_at(pack.parent / 'outer', '')
    manifest = other / 'pack.toml'
    manifest.write_text(manifest.read_text().replace('id="synthetic"', 'id="outer"'))
    context = load_launch_context(base, [pack, other])
    assert context.tooling_base_root == library
    assert context.tool_catalog_path == pack / 'catalog.toml'
    assert context.workflow_entry is None and context.workflow_base_root is None
    from api_agent.config.declarations import ConfigSchema
    from api_agent.tooling.selection import compose_tools
    case = 'pack::test.py::test_owned'
    catalog = discover(context, OperationSelection('tests', (case,)))
    assert catalog.roots['pack'] == pack
    plan = compose_tools(context, ConfigSchema({}), selection=(case,), environ={})
    assert plan.definitions[0].graph_id == 'catalog_graph'
    application = discover(context, OperationSelection('application'))
    assert application.selected_graphs == (('@application',) if application_exists else ())
    application_plan = compose_tools(context, ConfigSchema({}), environ={})
    assert [item.graph_id for item in application_plan.definitions] == (['flow'] if application_exists else [])


def test_catalog_and_application_libraries_have_independent_owners(catalog_library):
    base, pack, library = catalog_library
    application = base.parent / 'application'
    application.mkdir()
    write(application, 'native.py', 'raise AssertionError("discovery imported native")\n')
    write(application, 'node.md.html', (library / 'node.md.html').read_text())
    graph(application, 'flow', [])
    with (pack / 'pack.toml').open('a') as stream:
        stream.write('[workflow]\nversion = 1\nroot="base"\nentry="flow.md.html"\n'
            '[workflow.library]\nsource="../application"\ndistribution="synthetic-app"\nresource="workflows"\n')
    context = load_launch_context(base, pack)
    assert context.workflow_base_root == application
    assert context.tooling_base_root == library
    assert discover(context, OperationSelection('application')).definitions['@application'].graph_id == 'flow'
    catalog = discover(context, OperationSelection('tests', ('pack::test.py::test_owned',)))
    assert catalog.definitions['private'].graph_id == 'catalog_graph'


@pytest.mark.parametrize('omitted', [None, 'graphql.toml', 'package/factory.py'])
def test_installed_catalog_inventory_owns_complete_tool_closure(catalog_library, monkeypatch, omitted):
    from dataclasses import replace
    from api_agent.config.declarations import ConfigSchema
    from api_agent.runtime import resource_pack
    from api_agent.tooling.selection import compose_tools
    from api_agent.tooling.models import ToolSelectionError
    base, pack, library = catalog_library
    graph(library, 'catalog_graph', [('primary', 'primary.toml')])
    write(library, 'primary.toml', 'version=1\nid="sample/primary"\ninterface="verified-read@1"\nimplementation="graphql.toml"\n')
    write(library, 'graphql.toml', 'version=1\ninterface="verified-read@1"\nfactory="package/factory.py:create_tool"\n')
    factory = write(library, 'package/factory.py', 'def create_tool(config, services): pass\n')
    owned = frozenset(path.relative_to(library).as_posix() for path in library.rglob('*') if path.is_file())
    if omitted:
        owned -= {omitted}
    monkeypatch.setattr(resource_pack, '_workflow_library', lambda *args: (library, owned))
    context = load_launch_context(base, pack)
    # Application file ownership must never constrain this catalog's closure.
    context = replace(context, workflow_library_files=frozenset() if omitted is None else None)
    selected = ('pack::test.py::test_owned',)
    if omitted:
        with pytest.raises(ToolSelectionError, match='not owned'):
            compose_tools(context, ConfigSchema({}), selection=selected, environ={})
    else:
        first = compose_tools(context, ConfigSchema({}), selection=selected, environ={})
        assert first.captured_assets[factory] == factory.read_bytes()
        factory.write_text('def create_tool(config, services): return None\n')
        second = compose_tools(context, ConfigSchema({}), selection=selected, environ={})
        assert first.descriptor_id != second.descriptor_id
        assert first.captured_assets[factory] != second.captured_assets[factory]


@pytest.mark.parametrize('owned', [False, True])
def test_dormant_option_type_requires_owned_implementation(tmp_path, owned):
    from api_agent.config.declarations import ConfigSchema
    from api_agent.tooling.models import ToolCatalog
    from api_agent.tooling.selection import _option_inventory
    instance = write(tmp_path, 'instance.toml', 'version=1\nid="sample/primary"\ninterface="verified-read@1"\n'
        'implementation="implementation.toml"\n[bindings]\nLIMIT="ITEM_LIMIT"\n')
    write(tmp_path, 'implementation.toml', 'version=1\ninterface="verified-read@1"\nfactory="factory.py:create_tool"\n'
        '[inputs.LIMIT]\ntype="int"\n')
    definition = SimpleNamespace(tool_usages=(SimpleNamespace(instance=instance),),
        contents={instance: instance.read_bytes()})
    inventory = frozenset({'instance.toml', 'implementation.toml'} if owned else {'instance.toml'})
    catalog = ToolCatalog({'base': tmp_path, 'pack': tmp_path}, {'graph': definition}, (),
        OperationSelection('collection'), library_files=inventory)
    options = _option_inventory(catalog, ConfigSchema({}), (), {}, {}, {})
    assert options.fields['ITEM_LIMIT'].type == ('int' if owned else 'str')


@pytest.mark.parametrize('ordered', [False, True])
def test_catalog_library_declared_dependencies_and_groups_are_collected(catalog_library, ordered):
    from api_agent.runtime.dependencies import requirements
    base, pack, library = catalog_library
    (library / 'pyproject.toml').write_text('[project]\nname="shared-workflows"\nversion="2.0"\n'
        'dependencies=["catalog-tool>=2"]\n[project.optional-dependencies]\nbrowser=["browser-tool>=1"]\n'
        '[dependency-groups]\nlint=["lint-tool>=3"]\n')
    (pack / 'pyproject.toml').write_text('[project]\nname="private-pack"\nversion="1.0"\n'
        'dependencies=["shared-workflows[browser]>=2"]\n')
    directories = pack
    if ordered:
        outer = pack_at(pack.parent / 'outer', '')
        path = outer / 'pack.toml'
        path.write_text(path.read_text().replace('id="synthetic"', 'id="outer"'))
        directories = [pack, outer]
    context = load_launch_context(base, directories)
    expected = (('catalog-tool>=2', 'lint-tool>=3', 'browser-tool>=1') if ordered
                else ('browser-tool>=1', 'catalog-tool>=2', 'lint-tool>=3'))
    assert requirements(context, groups=('lint',)) == expected
    if ordered:
        contribution = next(item for item in context.project_contributions if item.pack_root == library)
        assert contribution.declarations[0].relative_path == 'pyproject.toml'
        (library / 'pyproject.toml').write_text('[project]\nname="changed"\n')
        with pytest.raises(ResourcePackError):
            requirements(context)


@pytest.mark.parametrize('ordered', [False, True])
def test_installed_catalog_project_requires_owned_manifest(catalog_library, monkeypatch, ordered):
    from api_agent.runtime import resource_pack
    base, pack, library = catalog_library
    (library / 'pyproject.toml').write_text('[project]\nname="shared-workflows"\nversion="2.0"\n')
    monkeypatch.setattr(resource_pack, '_workflow_library', lambda *args: (library, frozenset()))
    outer = pack_at(pack.parent / 'outer', '')
    path = outer / 'pack.toml'
    path.write_text(path.read_text().replace('id="synthetic"', 'id="outer"'))
    with pytest.raises(ResourcePackError, match='not owned'):
        context = load_launch_context(base, [pack, outer] if ordered else pack)
        from api_agent.runtime.dependencies import requirements
        requirements(context)


def test_installed_catalog_requirement_retains_distribution_install(catalog_library, monkeypatch):
    from api_agent.runtime import resource_pack
    from api_agent.runtime.dependencies import requirements
    base, pack, library = catalog_library
    (library / 'pyproject.toml').write_text('[project]\nname="shared-workflows"\nversion="2.0"\ndependencies=["catalog-tool>=2"]\n')
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["shared-workflows>=999"]\n')
    monkeypatch.setattr(resource_pack, '_workflow_library', lambda *args: (library, frozenset({'pyproject.toml'})))
    assert requirements(load_launch_context(base, pack)) == ('shared-workflows>=999', 'catalog-tool>=2')


def test_distinct_source_libraries_cannot_share_distribution_identity(catalog_library):
    from dataclasses import replace
    from api_agent.runtime.dependencies import requirements
    base, pack, library = catalog_library
    application = base.parent / 'application'
    application.mkdir()
    for root in (application, library):
        (root / 'pyproject.toml').write_text('[project]\nname="shared-workflows"\nversion="2.0"\n')
    context = replace(load_launch_context(base, pack), workflow_base_root=application)
    with pytest.raises(ValueError, match='conflicting distribution identities'):
        requirements(context)
