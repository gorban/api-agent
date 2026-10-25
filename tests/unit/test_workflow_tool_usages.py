"""Tool discovery preserves selected use sites without executing native code."""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from api_agent.workflows.definition import Replacement, load_definition
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.stateful import load_stateful_definition


def tools(*rows):
    return '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n' + ''.join(
        f'| {port} | [instance]({target}) |\n' for port, target in rows)


@pytest.fixture(params=[2, 3], ids=['v2', 'v3'])
def graph(tmp_path, request):
    version = request.param
    (tmp_path / 'native.py').write_text('raise AssertionError("native code executed")\n')
    (tmp_path / 'input.json').write_text('{"type":"object"}')
    (tmp_path / 'output.json').write_text('{"type":"object"}')
    (tmp_path / 'instance.toml').write_text('id = "sample/primary"\n')
    node = tmp_path / 'node.md.html'
    node.write_text(
        f'# Node v{version}: inspect\n\n## Native\n\n'
        '| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| inspect | deterministic | [source](native.py) |\n' + (
            '\n## Functions\n\n| ID | Registry | Input | Output |\n'
            '| --- | --- | --- | --- |\n'
            '| inspect | inspect | [input](input.json) | [output](output.json) |\n'
            if version == 2 else ''))
    component = tmp_path / 'component.md.html'
    component.write_text(
        f'# Component v{version}: work\n\n## Flow\n\n'
        '1.  [node inspect](node.md.html)\n' + (
            '\n## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n'
            '| work | 1 | inspect |\n' if version == 2 else ''))
    entry = tmp_path / 'flow.md.html'
    entry.write_text(
        f'# Workflow v{version}\n\n## Graph ID\n\n| ID |\n| --- |\n| sample |\n'
        '\n## Flow\n\n1.  [component work](component.md.html)\n' + (
            '\n## State\n\n| Registry | Source |\n| --- | --- |\n| State | [source](native.py) |\n'
            '\n## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
            '| inspect | END | - | - |\n| work | END | - | - |\n' if version == 3 else ''))
    loader = load_definition if version == 2 else load_stateful_definition
    return tmp_path, entry, component, node, loader


def admit(graph, **kwargs):
    root, entry, _, _, loader = graph
    return loader(entry, base_root=root, pack_root=root, **kwargs)


def test_capture_every_scope_and_port_with_one_immutable_resource(graph):
    root, entry, component, node, _ = graph
    entry.write_text(entry.read_text() + tools(('primary', 'instance.toml')))
    component.write_text(component.read_text() + tools(('primary', 'instance.toml')))
    node.write_text(node.read_text() + tools(('primary', 'instance.toml'), ('secondary', 'instance.toml')))

    definition = admit(graph)

    assert [(use.scope_kind, use.scope, use.port, use.instance, use.declaration) for use in definition.tool_usages] == [
        ('graph', 'sample', 'primary', root / 'instance.toml', entry),
        ('component', 'work', 'primary', root / 'instance.toml', component),
        ('node', 'inspect', 'primary', root / 'instance.toml', node),
        ('node', 'inspect', 'secondary', root / 'instance.toml', node)]
    for use in definition.tool_usages:
        assert use.declaration.read_text().splitlines()[use.line - 1].startswith(f'| {use.port} |')
    resources = [item for item in definition.resources if item.kind == 'tool_instance']
    assert len(resources) == 1
    assert resources[0].content_bytes == b'id = "sample/primary"\n'
    with pytest.raises(FrozenInstanceError):
        definition.tool_usages[0].port = 'changed'
    (root / 'instance.toml').write_text('id = "changed"\n')
    assert definition.contents[root / 'instance.toml'] == resources[0].content_bytes
    assert admit(graph).definition_id != definition.definition_id


def test_replacement_omits_old_instance_even_when_missing(graph):
    root, _, _, node, _ = graph
    replacement = root / 'replacement.md.html'
    replacement.write_text(node.read_text() + tools(('primary', 'instance.toml')))
    node.write_text(node.read_text() + tools(('old', 'missing.toml')))

    definition = admit(graph, replacements=(Replacement('inspect', Path('replacement.md.html'), 'base'),))

    assert len(definition.tool_usages) == 1
    use = definition.tool_usages[0]
    assert (use.scope_kind, use.scope, use.port, use.instance, use.declaration) == (
        'node', 'inspect', 'primary', root / 'instance.toml', replacement)
    assert node not in definition.contents
    assert root / 'missing.toml' not in definition.contents


@pytest.mark.parametrize('rows, message', [
    ((('bad-port', 'instance.toml'),), 'invalid or duplicate tool port'),
    ((('primary', 'instance.toml'), ('primary', 'instance.toml')), 'invalid or duplicate tool port'),
    ((('primary', 'input.json'),), 'tool instance must be TOML'),
    ((('primary', '../outside.toml'),), 'outside'),
])
def test_invalid_tool_usage_is_rejected(graph, rows, message):
    _, entry, _, _, _ = graph
    entry.write_text(entry.read_text() + tools(*rows))

    with pytest.raises(LayoutError, match=message):
        admit(graph)


def test_symlink_tool_instance_is_rejected(graph):
    root, entry, _, _, _ = graph
    (root / 'alias.toml').symlink_to(root / 'instance.toml')
    entry.write_text(entry.read_text() + tools(('primary', 'alias.toml')))

    with pytest.raises(LayoutError):
        admit(graph)


def test_graph_and_node_with_same_id_keep_distinct_use_sites(graph):
    _, entry, _, node, _ = graph
    entry.write_text(entry.read_text().replace('| sample |', '| inspect |') + tools(('primary', 'instance.toml')))
    node.write_text(node.read_text() + tools(('primary', 'instance.toml')))

    definition = admit(graph)

    assert [(use.scope_kind, use.scope, use.port) for use in definition.tool_usages] == [
        ('graph', 'inspect', 'primary'), ('node', 'inspect', 'primary')]


@pytest.mark.parametrize('graph', [3], indirect=True)
def test_snapshot_budget_stops_reading_before_later_tools_or_native(graph, monkeypatch):
    from api_agent.workflows import stateful
    root, entry, _, _, _ = graph
    for name in ('first', 'second', 'third'):
        (root / f'{name}.toml').write_bytes(b'x' * 1024)
    entry.write_text(entry.read_text() + tools(*((name, f'{name}.toml') for name in ('first', 'second', 'third'))))
    monkeypatch.setattr(stateful, '_MAX_SNAPSHOT_BYTES', len(entry.read_bytes()) + 1500)
    reads = []
    original = stateful._read_bounded
    def record_read(path, owner):
        reads.append(path)
        return original(path, owner)
    monkeypatch.setattr(stateful, '_read_bounded', record_read)

    with pytest.raises(LayoutError, match='snapshot exceeds'):
        admit(graph)

    assert reads == [entry, root / 'first.toml', root / 'second.toml']


@pytest.mark.parametrize('graph', [3], indirect=True)
def test_repeated_instance_counts_once_against_snapshot_budget(graph, monkeypatch):
    from api_agent.workflows import stateful
    _, entry, _, node, _ = graph
    entry.write_text(entry.read_text() + tools(('primary', 'instance.toml')))
    node.write_text(node.read_text() + tools(('primary', 'instance.toml')))
    size = sum(len(resource.content_bytes) for resource in admit(graph).resources)
    monkeypatch.setattr(stateful, '_MAX_SNAPSHOT_BYTES', size)

    definition = admit(graph)

    assert len(definition.tool_usages) == 2


def test_tool_instance_cannot_also_be_configuration_resource(graph):
    _, entry, component, node, loader = graph
    node.write_text(node.read_text() + tools(('primary', 'instance.toml')))
    if loader is load_definition:
        component.write_text(component.read_text().replace('| work | 1 | inspect |',
            '| work | [PASSES](instance.toml) | inspect |'))
    else:
        entry.write_text(entry.read_text() + '\n## Resources\n\n| ID | Kind | Source |\n'
            '| --- | --- | --- |\n| settings | configuration | [settings](instance.toml) |\n')

    with pytest.raises(LayoutError, match='tool instance has conflicting resource kind'):
        admit(graph)
