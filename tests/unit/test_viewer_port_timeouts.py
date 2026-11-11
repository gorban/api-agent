"""A declared tool port timeout is shown on that port's tool binding, not on its node."""
from dataclasses import replace
from html import unescape

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source
from test_workflow_viewer_projection import graph

CONFIG = ('version = 1\n\n[fields.NODE_SECONDS]\ntype = "float"\ndefault = 5\nvalidation = "positive"\n'
          'acknowledge_default = false\nhelp = "Node budget."\n'
          '\n[fields.TOOL_SECONDS]\ntype = "float"\ndefault = 1\nvalidation = "positive"\n'
          'acknowledge_default = false\nhelp = "Tool budget."\n')


@pytest.fixture
def selected(tmp_path):
    entry = graph(tmp_path)
    (tmp_path / 'timeouts.toml').write_text(CONFIG)
    (tmp_path / 'tool.toml').write_text('version = 1\n')
    node = tmp_path / 'children/work.md.html'
    node.write_text(node.read_text() + '\n## Tools\n\n| Port | Instance | Operations |\n| --- | --- | --- |\n'
                    '| search | [tool](../tool.toml) | describe, prepare, verify, execute |\n')
    entry.write_text(entry.read_text() + '\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n'
                     '| work | [config](timeouts.toml) | NODE_SECONDS |\n'
                     '| work.search | [config](timeouts.toml) | TOOL_SECONDS |\n')
    return entry


def rendered(entry):
    view = project_view(load_definition(entry, base_root=entry.parent, pack_root=entry.parent))
    source, links = _projection_source(view, view.documents[0])
    return view, unescape(source.decode()), links


def alias_of(source, marker):
    """The PlantUML alias of the one rectangle whose label contains ``marker``."""
    found = [line for line in source.splitlines() if line.startswith('rectangle') and marker in line]
    assert len(found) == 1, source
    line = found[0]
    return line.split(' as ')[1].split()[0]


def test_port_timeout_labels_the_tool_binding_and_node_timeout_stays_on_the_node(selected):
    view, source, links = rendered(selected)
    lines = source.splitlines()
    port_alias, node_alias = alias_of(source, 'Timeout: TOOL_SECONDS'), alias_of(source, 'Timeout: NODE_SECONDS')
    binding, node_box = alias_of(source, 'Tool instance: tool'), alias_of(source, '"work"')
    import re
    assert any(re.fullmatch(rf'{node_box} \.{{2,}} {node_alias}', line) for line in lines)
    assert not any(re.fullmatch(rf'{node_box} \.{{2,}} {port_alias}', line) for line in lines)
    assert f'{binding} .. {port_alias}' in lines
    assert source.count('Timeout: TOOL_SECONDS') == source.count('Timeout: NODE_SECONDS') == 1
    assert any(link.endswith('timeouts.toml') for link in links)
    assert any(ref.owner_id == 'work.search' and ref.kind == 'timeouts' for ref in view.references)


def test_port_timeout_is_absent_when_the_row_is_absent(selected):
    selected.write_text(selected.read_text().replace('| work.search | [config](timeouts.toml) | TOOL_SECONDS |\n', ''))
    _, source, _ = rendered(selected)
    assert 'TOOL_SECONDS' not in source and 'Timeout: NODE_SECONDS' in source


def test_port_timeout_counts_toward_the_record_budget(selected, monkeypatch):
    from api_agent.workflows import viewer_render
    from api_agent.workflows.viewer_render import RenderError
    rendered(selected)
    view, *_ = rendered(selected)
    monkeypatch.setattr(viewer_render, '_MAX_RECORDS', 3)
    with pytest.raises(RenderError, match='record limit'):
        _projection_source(view, view.documents[0])


def test_shared_instance_shows_each_consumers_own_port_timeout(selected):
    selected.write_text(selected.read_text().replace(
        '[node work](children/work.md.html)',
        '[node work](children/work.md.html)\n2.  [node other](children/other.md.html)'))
    (selected.parent / 'children/other.md.html').write_text(
        '# Node: other\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| other | work | [input](../schema.json) | [output](../schema.json) |\n\n'
        '## Tools\n\n| Port | Instance | Operations |\n| --- | --- | --- |\n'
        '| search | [tool](../tool.toml) | describe, prepare, verify, execute |\n')
    selected.write_text(selected.read_text() + '| other.search | [config](timeouts.toml) | NODE_SECONDS |\n')
    _, source, _ = rendered(selected)
    assert source.count('Tool instance: tool') == 1
    assert source.count('Timeout: TOOL_SECONDS') == 1 and source.count('Timeout: NODE_SECONDS') == 2
