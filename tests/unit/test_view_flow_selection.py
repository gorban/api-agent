"""Canonical view identity determines when static files must match."""
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from api_agent.runtime.view_flow import _canonical_selection, _prepare_view
from api_agent.runtime.resource_pack import load_launch_context, load_selected_definition
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows import viewer_render
from api_agent.workflows.viewer_render import RenderError

ROOT = Path(__file__).resolve().parents[2]


def context(root):
    return SimpleNamespace(base_root=root, workflow_base_root=None)


def test_unmodified_owner_root_uses_static_freshness(tmp_path):
    definition = SimpleNamespace(entry_root='base', entry=tmp_path / 'flow.md.html',
                                 roots={'base': tmp_path}, replacements=())
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),), resources=())
    assert _canonical_selection(context(tmp_path), definition, projection)


def test_nested_selected_graph_uses_its_generated_static_diagram(tmp_path):
    definition = SimpleNamespace(entry_root='base', entry=tmp_path / 'demo/simple/flow.md.html',
                                 roots={'base': tmp_path}, replacements=())
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/demo/simple/flow.md.html'),), resources=())
    assert _canonical_selection(context(tmp_path), definition, projection)


@pytest.mark.parametrize('entry', ['example.md.html', '../outside/flow.md.html'])
def test_unregistered_entry_shape_does_not_claim_generated_graph_freshness(tmp_path, entry):
    definition = SimpleNamespace(entry_root='base', entry=(tmp_path / entry).resolve(),
                                 roots={'base': tmp_path}, replacements=())
    projection = SimpleNamespace(documents=(), resources=())
    assert not _canonical_selection(context(tmp_path), definition, projection)


def test_selected_replacement_uses_temporary_projection(tmp_path):
    definition = SimpleNamespace(entry_root='base', entry=tmp_path / 'flow.md.html',
                                 roots={'base': tmp_path}, replacements=(object(),))
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),))
    assert not _canonical_selection(context(tmp_path), definition, projection)


def test_cross_owner_document_uses_temporary_projection(tmp_path):
    definition = SimpleNamespace(entry_root='base', entry=tmp_path / 'flow.md.html',
                                 roots={'base': tmp_path}, replacements=())
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),
                                            SimpleNamespace(logical_path='pack/overlay.md.html')))
    assert not _canonical_selection(context(tmp_path), definition, projection)


def test_pack_owned_resource_generates_temporary_selected_diagram(tmp_path, monkeypatch):
    base = tmp_path / 'base'
    pack = tmp_path / 'pack'
    (base / 'children').mkdir(parents=True)
    pack.mkdir()
    (base / 'flow.md.html').write_text(
        '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
        '## Flow\n\n1.  [node work](children/work.md.html)\n')
    (base / 'children/work.md.html').write_text(
        '# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| work | work | [input](../schema.json) | [output](../schema.json) |\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        r'| work | [Pack prompt](\<resource_pack\>/prompt.md) | always |' + '\n')
    (base / 'native.py').write_text('def work(inputs, context):\n    return {}\n')
    (base / 'schema.json').write_text('{"type":"object"}')
    (pack / 'prompt.md').write_text('Synthetic selected prompt.\n')
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text('version=1\nid="synthetic-pack"\nconfig="config.toml"\n')
    source_before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}

    class StubRenderer:
        def render(self, source, *, allowed_links=frozenset()):
            return (b'<?xml version="1.0"?>\n'
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"/>')

    monkeypatch.setattr(viewer_render, 'default_renderer', StubRenderer)

    definition, projection, canonical, bundle, repository_sources = _prepare_view(load_launch_context(base, pack))

    assert definition.entry_root == 'base'
    assert {document.logical_path for document in projection.documents} == {
        'base/flow.md.html', 'base/children/work.md.html'}
    assert any(resource.logical_path == 'pack/prompt.md' for resource in projection.resources)
    assert canonical is False
    assert any(item.path == 'r/base/.private_resources/diagrams/flow.svg'
               and b'<svg' in item.body for item in bundle.files)
    assert {path: path.read_bytes() for path in base.rglob('*') if path.is_file()} == source_before
    assert not (base / '.private_resources').exists()


def test_owner_inline_prompt_requires_saved_static_diagram(tmp_path):
    base = tmp_path / 'base'
    pack = tmp_path / 'pack'
    (base / 'children').mkdir(parents=True)
    pack.mkdir()
    (base / 'flow.md.html').write_text(
        '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
        '## Flow\n\n1.  [node work](children/work.md.html)\n')
    (base / 'children/work.md.html').write_text(
        '# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| work | work | [input](../schema.json) | [output](../schema.json) |\n\n'
        '## Prompt: greeting\n\n```text\nHello.\n```\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| work | @greeting | always |\n')
    (base / 'native.py').write_text('def work(inputs, context):\n    return {}\n')
    (base / 'schema.json').write_text('{"type":"object"}')
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text('version=1\nid="synthetic-pack"\nconfig="config.toml"\n')
    launch = load_launch_context(base, pack)
    definition = load_selected_definition(launch)
    projection = project_view(definition)

    assert {document.logical_path for document in projection.documents} == {
        'base/flow.md.html', 'base/children/work.md.html'}
    assert any(resource.logical_path == 'inline/base/greeting' for resource in projection.resources)
    assert _canonical_selection(launch, definition, projection)
    with pytest.raises(RenderError, match='missing or stale workflow diagram'):
        _prepare_view(launch)


def test_sibling_workflow_library_is_temporary_selected_view(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    library = tmp_path / 'library'
    pack = tmp_path / 'pack'
    runtime.mkdir()
    library.mkdir()
    pack.mkdir()
    shutil.copy2(ROOT / 'flow.md.html', library / 'flow.md.html')
    shutil.copytree(ROOT / 'cfg', library / 'cfg')
    shutil.copytree(ROOT / 'workflows', library / 'workflows')
    shutil.copytree(ROOT / 'demo/simple', library / 'demo/simple')
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text('''version=1
id="synthetic-pack"
config="config.toml"
[workflow]
version=2
root="base"
entry="flow.md.html"
[workflow.library]
source="../library"
distribution="synthetic-workflow"
resource="synthetic_workflows"
''')
    launch = load_launch_context(runtime, pack)
    definition = load_selected_definition(launch)
    projection = project_view(definition)
    assert launch.workflow_base_root == library
    assert definition.entry == library / 'flow.md.html'
    assert not _canonical_selection(launch, definition, projection)
    observed = []
    monkeypatch.setattr(viewer_render, 'build_view_bundle',
                        lambda selected, *, roots, verify, **kwargs: observed.append((selected, roots, verify)))
    _prepare_view(launch)
    assert observed == [(projection, definition.roots, False)]
