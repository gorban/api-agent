"""Application factories are explicit, source-admitted v3 bindings."""
import asyncio

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.runtime.local_tools import selected_application
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.definition import (load_definition)
from test_workflow_stateful import fixture_graph


@pytest.fixture
def application_graph(fixture_graph):
    root, entry = fixture_graph
    source = root / 'application.py'
    source.write_text('''from types import SimpleNamespace

def create_application(definition, *, config):
    async def complete(request, *, llm, context):
        return (request, llm, context)
    return SimpleNamespace(definition=definition, config=config, complete=complete,
                           timeouts={}, requires_api=False)
''')
    entry.write_text(entry.read_text().replace('## Flow', '''## Application

| Registry | Source |
| --- | --- |
| create_application | [Python](application.py) |

## Flow'''))
    return root, entry, source


def test_selected_stateful_application_uses_admitted_factory(application_graph):
    root, entry, source = application_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    config = {'WORKFLOW_GRAPH_TIMEOUT_SECONDS': 19}

    application = selected_application(definition, config=config)

    assert application.definition is definition
    assert application.config == config
    assert application.requires_api is False
    assert application.timeouts == {}
    assert definition.application_binding == ('create_application', source)
    assert source in definition.contents
    assert asyncio.run(application.complete('request', llm='selected', context='owned')) == (
        'request', 'selected', 'owned')


def test_factory_is_in_the_workflow_provenance(application_graph):
    from api_agent.workflows.viewer_projection import project_view
    root, entry, source = application_graph
    definition = load_definition(entry, base_root=root, pack_root=root)

    projection = project_view(definition)

    references = [row for row in projection.references if row.target_path.endswith('/application.py')]
    assert len(references) == 1
    assert dict(references[0].attributes)['registry'] == 'create_application'
    assert dict(references[0].attributes)['role'] == 'application'


def test_changed_application_source_is_rejected_before_factory_execution(application_graph):
    root, entry, source = application_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    source.write_text("raise RuntimeError('changed source was executed')\n")

    with pytest.raises(LayoutError, match='resource changed after admission'):
        selected_application(definition)


def test_stateful_run_local_requires_explicit_application_factory(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)

    with pytest.raises(ContractError, match='application binding'):
        selected_application(definition)


@pytest.mark.parametrize('row', [
    'bad-name | [Python](application.py)',
    'create_application | [Python](application.py) | extra',
    'create_application | [Python](application.py) |\n| second | [Python](application.py)',
])
def test_invalid_application_binding_is_rejected(application_graph, row):
    root, entry, source = application_graph
    entry.write_text(entry.read_text().replace('create_application | [Python](application.py)', row))

    with pytest.raises(LayoutError):
        load_definition(entry, base_root=root, pack_root=root)


def test_empty_application_binding_is_rejected(application_graph):
    root, entry, source = application_graph
    entry.write_text(entry.read_text().replace('| create_application | [Python](application.py) |\n', ''))

    with pytest.raises(LayoutError, match='application'):
        load_definition(entry, base_root=root, pack_root=root)


@pytest.mark.parametrize('replacement', [
    'create_application = None',
    'def create_application(definition, *, config):\n    return None',
    'def create_application(definition, *, config):\n    return type("Bad", (), {"definition": definition})()',
])
def test_invalid_factory_contract_fails_closed(application_graph, replacement):
    root, entry, source = application_graph
    source.write_text(replacement + '\n')
    definition = load_definition(entry, base_root=root, pack_root=root)

    with pytest.raises(ContractError, match='application'):
        selected_application(definition)


def with_dependency_manifest(application_graph, content):
    root, entry, source = application_graph
    manifest = root / 'dependencies.toml'
    manifest.write_text(content)
    entry.write_text(entry.read_text().replace('## Flow', '''## Native Dependency Manifests

| Source |
| --- |
| [TOML](dependencies.toml) |

## Flow'''))
    return root, entry, source, manifest


def test_application_dependency_manifest_is_captured_before_native_import(application_graph):
    root, entry, source, manifest = with_dependency_manifest(application_graph,
        'version = 1\n[modules]\nfixture_extension = "fixture_extension.py"\n')
    dependency = root / 'fixture_extension.py'
    dependency.write_text('value = 7\n')
    source.write_text(source.read_text().replace('from types import SimpleNamespace',
        'from types import SimpleNamespace\nfrom fixture_extension import value'))
    definition = load_definition(entry, base_root=root, pack_root=root)

    application = selected_application(definition)

    assert application.definition is definition
    assert definition.native_dependencies['fixture_extension'] == dependency
    assert manifest in definition.contents and dependency in definition.contents
    manifest.write_text('version = 1\n[modules]\n')
    with pytest.raises(LayoutError, match='resource changed after admission'):
        selected_application(definition)


@pytest.mark.parametrize('content', [
    'not valid toml = [',
    'version = true\n[modules]\n',
    'version = 2\n[modules]\n',
    'version = 1\nextra = true\n[modules]\n',
    'version = 1\nmodules = []\n',
    'version = 1\n[modules]\nbad-name = "native.py"\n',
    'version = 1\n[modules]\nmodule = 7\n',
    'version = 1\n[modules]\nmodule = "native.py"\n',
    'version = 1\n[modules]\nmodule = "../../../escaped.py"\n',
])
def test_invalid_dependency_manifest_fails_before_native_execution(application_graph, content):
    root, entry, source, manifest = with_dependency_manifest(application_graph, content)

    with pytest.raises(LayoutError):
        load_definition(entry, base_root=root, pack_root=root)


def test_duplicate_manifest_dependency_is_rejected(application_graph):
    root, entry, source, manifest = with_dependency_manifest(application_graph,
        'version = 1\n[modules]\nfixture_duplicate = "fixture_duplicate.py"\n')
    (root / 'fixture_duplicate.py').write_text('value = 1\n')
    entry.write_text(entry.read_text().replace('## Flow', '''## Native Dependencies

| Module | Source |
| --- | --- |
| fixture_duplicate | [Python](fixture_duplicate.py) |

## Flow'''))

    with pytest.raises(LayoutError, match='dependency'):
        load_definition(entry, base_root=root, pack_root=root)


@pytest.mark.parametrize('duplicate', [False, True], ids=['wrong-extension', 'duplicate-manifest'])
def test_manifest_ownership_is_checked_before_loading_valid_toml(application_graph, duplicate):
    root, entry, source, manifest = with_dependency_manifest(application_graph,
        'version = 1\n[modules]\n')
    row = '| [TOML](dependencies.toml) |'
    if duplicate:
        entry.write_text(entry.read_text().replace(row, row + '\n' + row))
    else:
        manifest.rename(root / 'dependencies.txt')
        entry.write_text(entry.read_text().replace('dependencies.toml', 'dependencies.txt'))

    with pytest.raises(LayoutError, match='invalid native dependency manifest'):
        load_definition(entry, base_root=root, pack_root=root)
