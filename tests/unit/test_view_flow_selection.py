"""Canonical view identity determines when static files must match."""
from pathlib import Path
import shutil
from types import SimpleNamespace

from api_agent.runtime.view_flow import _canonical_selection, _prepare_view
from api_agent.runtime.resource_pack import load_launch_context, load_selected_definition
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows import viewer_render

ROOT = Path(__file__).resolve().parents[2]


def context(root):
    return SimpleNamespace(base_root=root, workflow_base_root=None)


def test_unmodified_owner_root_uses_static_freshness(tmp_path):
    definition = SimpleNamespace(entry_root='base', entry=tmp_path / 'flow.md.html',
                                 roots={'base': tmp_path}, replacements=())
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),))
    assert _canonical_selection(context(tmp_path), definition, projection)


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


def test_sibling_workflow_library_is_temporary_selected_view(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    library = tmp_path / 'library'
    pack = tmp_path / 'pack'
    runtime.mkdir()
    library.mkdir()
    pack.mkdir()
    shutil.copy2(ROOT / 'flow.md.html', library / 'flow.md.html')
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
                        lambda selected, *, roots, verify: observed.append((selected, roots, verify)))
    _prepare_view(launch)
    assert observed == [(projection, definition.roots, False)]
