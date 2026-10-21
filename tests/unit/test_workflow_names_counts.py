"""Generic root-heading aliases and recursive executable component labels."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.document_format import extract_document, wrap_document
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source

ROOT = Path(__file__).resolve().parents[2]


def root_graph(root, heading):
    (root / 'native.py').write_text('def work(inputs, context): return {}\n')
    (root / 'schema.json').write_text('{"type":"object"}')
    entry = root / 'flow.md.html'
    entry.write_text(heading + '\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
                     '## Flow\n\n1.  [node work](work.md.html)\n')
    (root / 'work.md.html').write_text(
        '# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](native.py) |\n\n## Functions\n\n'
        '| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| work | work | [input](schema.json) | [output](schema.json) |\n')
    return entry


@pytest.mark.parametrize('heading', ['# Workflow v2', '# LangGraph flow'])
def test_exact_v2_root_headings_keep_execution_and_root_carrier(tmp_path, heading):
    entry = root_graph(tmp_path, heading)
    raw = entry.read_bytes()
    entry.write_bytes(wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/'))

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert definition.graph_id == 'example'
    assert [(step.id, step.kind) for step in definition.layout.steps] == [('work', 'node')]
    assert extract_document(entry.read_bytes(), path=entry).executable_bytes == raw
    assert b'<h2>Viewing this file</h2>' in entry.read_bytes()


@pytest.mark.parametrize('heading', ['# LangGraph flow extra', '# LangGraph flow <script>x</script>', '# Arbitrary title'])
def test_unapproved_root_titles_are_rejected(tmp_path, heading):
    entry = root_graph(tmp_path, heading)

    with pytest.raises(LayoutError):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_real_contrived_components_have_three_executable_nodes_in_both_views():
    root = ROOT / 'demo/contrived'
    definition = load_definition(root / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    projection = project_view(definition)

    for document in projection.documents:
        if document.logical_path == projection.entry_path or document.stable_id in ('first', 'second'):
            source, _ = _projection_source(projection, document)
            if document.logical_path == projection.entry_path:
                assert b'package "first (3 nodes)" {' in source
                assert b'package "second (3 nodes)" {' in source
            else:
                assert ('package "' + document.stable_id + ' (3 nodes)" {').encode() in source


def test_nested_counts_include_executable_descendants_only_even_in_component_view():
    root = SimpleNamespace(logical_path='base/flow.md.html')
    group_doc = SimpleNamespace(logical_path='base/group.md.html')
    child_doc = SimpleNamespace(logical_path='base/child.md.html')
    def step(identifier, kind, parent, document):
        return SimpleNamespace(id=identifier, kind=kind, parent=parent,
                               declaration_path=document.logical_path)
    steps = (step('group', 'component', None, group_doc),
             step('nested', 'component', 'group', child_doc),
             step('one', 'node', 'nested', child_doc),
             step('two', 'node', 'nested', child_doc),
             step('single', 'component', 'group', child_doc),
             step('three', 'node', 'single', child_doc),
             step('outside', 'node', None, child_doc))
    projection = SimpleNamespace(entry_path=root.logical_path, documents=(root, group_doc, child_doc),
                                 steps=steps, edges=(), references=(), resources=())

    source, _ = _projection_source(projection, root)
    local_source, _ = _projection_source(projection, group_doc)

    assert b'package "group (3 nodes)" {' in source
    assert b'package "nested (2 nodes)" {' in source
    assert b'package "single" {' in source
    assert b'package "single (1 nodes)"' not in source
    assert b'package "group (3 nodes)" {' in local_source


def test_alias_keeps_physical_diagnostics_and_cannot_replace_child_identity(tmp_path):
    entry = root_graph(tmp_path, '# LangGraph flow')
    raw = entry.read_bytes() + (b'\n## Bindings\n\n| Node | Input | From | Field | Default |\n'
                                b'| --- | --- | --- | --- | --- |\n| missing | field | START | field | - |\n')
    wrapped = wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/')
    entry.write_bytes(wrapped)
    physical = wrapped.splitlines().index(b'| missing | field | START | field | - |') + 1

    with pytest.raises(LayoutError, match=str(entry) + ':' + str(physical) + ':'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    entry.write_text('# LangGraph flow\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
                     '## Flow\n\n1.  [node work](work.md.html)\n')
    child = tmp_path / 'work.md.html'
    child_raw = child.read_bytes().replace(b'# Node v2: work', b'# LangGraph flow')
    child.write_bytes(wrap_document(child_raw, root=True, asset_prefix='.private_resources/viewer/'))
    with pytest.raises(LayoutError, match='identity mismatch'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
