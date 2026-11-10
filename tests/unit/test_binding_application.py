"""The binding grammar declares its application factory like the typed grammar."""
import asyncio
from unittest.mock import Mock

import pytest

from test_workflow_definition import make_graph

from api_agent.providers.contracts import ContractError
from api_agent.runtime import local_tools
from api_agent.runtime.local_tools import selected_application
from api_agent.workflows.definition import load_definition
from api_agent.workflows.identity import declaration_identity
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.viewer_projection import project_view

FACTORY = '''from types import SimpleNamespace

def create_application(definition, *, config):
    async def complete(request, *, llm, context):
        return (request, llm, context)
    return SimpleNamespace(definition=definition, config=config, complete=complete,
                           timeouts={}, requires_api=False)
'''
SECTION = '''## Application

| Registry | Source |
| --- | --- |
| create_application | [Python](application.py) |

## Flow'''


@pytest.fixture
def application_graph(tmp_path):
    entry = make_graph(tmp_path)
    source = tmp_path / 'application.py'
    source.write_text(FACTORY)
    entry.write_text(entry.read_text().replace('## Flow', SECTION))
    return tmp_path, entry, source


def load(root, entry):
    return load_definition(entry, base_root=root, pack_root=root)


def test_declared_application_is_admitted_and_selected(application_graph):
    root, entry, source = application_graph
    definition = load(root, entry)
    config = {'WORKFLOW_GRAPH_TIMEOUT_SECONDS': 19}

    application = selected_application(definition, config=config)

    assert definition.application_binding == ('create_application', source)
    assert definition.contents[source] == FACTORY.encode()
    assert {item.kind for item in definition.resources if item.logical_path == 'base/application.py'} == {'native_source'}
    assert application.definition is definition
    assert application.config == config
    assert asyncio.run(application.complete('request', llm='selected', context='owned')) == (
        'request', 'selected', 'owned')


def test_application_can_come_from_the_resource_pack(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    entry = make_graph(base)
    (pack / 'application.py').write_text(FACTORY)
    entry.write_text(entry.read_text().replace('## Flow', SECTION.replace(
        '[Python](application.py)', '[Python](<resource_pack>/application.py)')))

    definition = load_definition(entry, base_root=base, pack_root=pack)

    assert definition.application_binding == ('create_application', pack / 'application.py')
    assert selected_application(definition).definition is definition


def test_absent_application_keeps_the_default_selection(tmp_path, monkeypatch):
    definition = load(tmp_path, make_graph(tmp_path))
    chat = Mock()
    monkeypatch.setattr(local_tools, 'LocalChatApplication', chat)

    assert definition.application_binding is None
    assert selected_application(definition, config={'A': 1}) is chat.return_value
    chat.assert_called_once_with(definition, config={'A': 1})


def test_declared_application_replaces_the_default_selection(application_graph, monkeypatch):
    root, entry, _ = application_graph
    chat = Mock()
    monkeypatch.setattr(local_tools, 'LocalChatApplication', chat)

    selected_application(load(root, entry))

    chat.assert_not_called()


@pytest.mark.parametrize('replacement,line', [
    ('| bad-name | [Python](application.py) |', 'flow.md.html:13'),
    ('| create_application | [Python](application.py) | extra |', 'flow.md.html:13'),
    ('| create_application | [Python](application.py) |\n| second | [Python](application.py) |', 'flow.md.html:14'),
    ('', 'flow.md.html:9'),
    ('| create_application | [Text](notes.txt) |', 'flow.md.html:13'),
    ('| create_application | [Python](missing.py) |', 'missing.py:1'),
    ('| create_application | [Python](../outside.py) |', 'outside'),
    ('| create_application | application.py |', 'flow.md.html:1'),
])
def test_invalid_application_rows_are_rejected_with_a_location(application_graph, replacement, line):
    root, entry, _ = application_graph
    (root / 'notes.txt').write_text('text')
    entry.write_text(entry.read_text().replace('| create_application | [Python](application.py) |', replacement))

    with pytest.raises(LayoutError, match=line):
        load(root, entry)


def test_application_section_without_a_body_is_rejected(application_graph):
    root, entry, _ = application_graph
    entry.write_text(entry.read_text().replace(SECTION, '## Application\n## Flow'))

    with pytest.raises(LayoutError, match='flow.md.html:1: invalid application binding'):
        load(root, entry)


def test_application_section_must_not_repeat(application_graph):
    root, entry, _ = application_graph
    entry.write_text(entry.read_text().replace('## Flow', SECTION))

    with pytest.raises(LayoutError, match='duplicate section'):
        load(root, entry)


def test_application_source_change_changes_identity(application_graph):
    root, entry, source = application_graph
    first = load(root, entry)
    source.write_text(FACTORY + '# changed\n')
    second = load(root, entry)

    assert first.definition_id != second.definition_id
    assert declaration_identity(first) != declaration_identity(second)


def test_application_registry_is_part_of_the_declaration_identity(application_graph):
    root, entry, source = application_graph
    source.write_text(FACTORY + FACTORY.replace('create_application', 'create_other'))
    first = load(root, entry)
    entry.write_text(entry.read_text().replace('| create_application |', '| create_other |'))

    assert declaration_identity(first) != declaration_identity(load(root, entry))


def test_absent_application_leaves_the_declaration_identity_unkeyed(tmp_path):
    from api_agent.workflows.identity import _Snapshot
    definition = load(tmp_path, make_graph(tmp_path))

    assert 'application' not in _Snapshot(definition).declaration()


def test_changed_application_source_is_rejected_before_execution(application_graph):
    root, entry, source = application_graph
    definition = load(root, entry)
    source.write_text("raise RuntimeError('changed source was executed')\n")

    with pytest.raises(LayoutError, match='changed after definition admission'):
        selected_application(definition)


@pytest.mark.parametrize('replacement', [
    'create_application = None',
    'def create_application(definition, *, config):\n    return None',
    'def create_application(definition, *, config):\n    return type("Bad", (), {"definition": definition})()',
])
def test_invalid_factory_contract_fails_closed(application_graph, replacement):
    root, entry, source = application_graph
    source.write_text(replacement + '\n')

    with pytest.raises(ContractError, match='application'):
        selected_application(load(root, entry))


def test_viewer_references_the_application_source(application_graph):
    root, entry, _ = application_graph

    projection = project_view(load(root, entry))

    references = [row for row in projection.references if row.target_path == 'base/application.py']
    assert [dict(row.attributes) for row in references] == [
        {'registry': 'create_application', 'role': 'application'}]
    assert references[0].kind == 'native_source'


def test_viewer_without_application_has_no_application_reference(tmp_path):
    projection = project_view(load(tmp_path, make_graph(tmp_path)))

    assert [row for row in projection.references if dict(row.attributes).get('role') == 'application'] == []


def test_only_admitted_native_sources_can_be_executed(application_graph):
    from api_agent.workflows.definition import load_native_catalog
    root, entry, _ = application_graph
    (root / 'other.py').write_text(FACTORY)

    with pytest.raises(LayoutError, match='not a selected native binding'):
        load_native_catalog(load(root, entry), root / 'other.py')
