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


def test_temporary_view_honors_explicit_renderer_and_rejects_other_errors(tmp_path, monkeypatch):
    from api_agent.runtime import view_flow
    from api_agent.workflows import viewer_build
    definition = SimpleNamespace(roots={'base': tmp_path})
    projection = object()
    selected_renderer, bundle = object(), object()
    monkeypatch.setattr(view_flow, 'load_selected_definition', lambda context: definition)
    monkeypatch.setattr(view_flow, '_canonical_selection', lambda *args: True)
    monkeypatch.setattr('api_agent.workflows.viewer_projection.project_view', lambda definition: projection)
    monkeypatch.setattr(viewer_render, 'capture_repository_sources', lambda roots: ({}, ()))
    monkeypatch.setenv('API_AGENT_PLANTUML_JAR', '/explicit/renderer.jar')
    monkeypatch.setattr(viewer_render, 'default_renderer', lambda: selected_renderer)
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: pytest.fail('explicit renderer ignored'))
    calls = []
    def build(*args, **kwargs):
        calls.append(kwargs)
        if kwargs['verify']:
            raise viewer_render.StaleViewError('stale')
        return bundle
    monkeypatch.setattr(viewer_render, 'build_view_bundle', build)
    assert view_flow._prepare_view(object())[3] is bundle
    assert calls[1]['renderer'] is selected_renderer and calls[1]['verify'] is False

    def invalid(*args, **kwargs):
        raise RenderError('invalid workflow diagram provenance')
    monkeypatch.setattr(viewer_render, 'build_view_bundle', invalid)
    with pytest.raises(RenderError, match='invalid workflow diagram provenance'):
        view_flow._prepare_view(object())


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


def test_owner_inline_prompt_uses_temporary_diagram_when_saved_one_is_missing(tmp_path, monkeypatch):
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
    class StubRenderer:
        def render(self, source, *, allowed_links=frozenset()):
            return (b'<?xml version="1.0"?>\n'
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"/>')

    from api_agent.workflows import viewer_build
    monkeypatch.delenv('API_AGENT_PLANTUML_JAR', raising=False)
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: tmp_path / 'renderer.jar')
    monkeypatch.setattr(viewer_render, 'PlantUMLRenderer', lambda jar: StubRenderer())
    before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}
    prepared = _prepare_view(launch)
    assert any(item.path.endswith('/flow.svg') for item in prepared[3].files)
    assert {path: path.read_bytes() for path in base.rglob('*') if path.is_file()} == before


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


def test_omitted_flow_uses_selected_application_declaration():
    selected = load_launch_context(ROOT)
    assert load_selected_definition(selected).entry == ROOT / 'demo/simple/flow.md.html'


@pytest.mark.parametrize(('relative', 'base', 'pack', 'owner'), [
    ('flow.md.html', '.', 'demo', 'base'),
    ('demo/simple/flow.md.html', '.', 'demo', 'base'),
    ('demo/contrived/flow.md.html', '.', 'demo', 'pack'),
    ('tests/integration/fixtures/workflow_preferences/flow.md.html',
     'tests/integration/fixtures/workflow_preferences', 'tests/integration/fixtures/workflow_preferences', 'base'),
    ('tests/integration/fixtures/tool_instances/shared_primary/flow.md.html', '.',
     'tests/integration/fixtures/tool_instances', 'base'),
])
def test_explicit_flow_uses_its_resource_root_mapping(relative, base, pack, owner):
    from api_agent.runtime.view_flow import _view_context
    selected = _view_context(load_launch_context(ROOT), ROOT / relative)
    definition = load_selected_definition(selected)
    assert definition.entry == ROOT / relative
    assert definition.roots == {'base': ROOT / base, 'pack': ROOT / pack}
    assert definition.entry_root == owner


def test_explicit_flow_rejects_outside_paths_and_symlinks(tmp_path):
    from api_agent.runtime.view_flow import _view_context
    from api_agent.runtime.resource_pack import ResourcePackError
    selected = load_launch_context(ROOT)
    outside = tmp_path / 'flow.md.html'
    outside.write_text('# Workflow v2\n')
    with pytest.raises(ResourcePackError, match='inside the selected'):
        _view_context(selected, outside)
    from dataclasses import replace
    alias = tmp_path / 'alias.md.html'
    alias.symlink_to(outside)
    local = replace(selected, base_root=tmp_path, pack_root=tmp_path, workflow_entry=None)
    from api_agent.workflows.definition import LayoutError
    with pytest.raises(LayoutError):
        _view_context(local, alias)


def test_explicit_mapped_fixture_uses_saved_diagram_without_renderer(monkeypatch):
    monkeypatch.setattr(viewer_render, 'default_renderer', lambda: pytest.fail('fresh fixture was rerendered'))
    entry = ROOT / 'tests/integration/fixtures/workflow_preferences/flow.md.html'
    definition, projection, canonical, bundle, _ = _prepare_view(load_launch_context(ROOT), entry=entry)
    assert canonical is True
    assert definition.entry == entry
    assert bundle.entry_path == 'r/base/flow.md.html'


def test_explicit_flow_prefers_the_nearest_declared_root_mapping(tmp_path):
    from dataclasses import replace
    from api_agent.runtime.view_flow import _view_context
    from test_workflow_viewer_discovery import minimal_graph, roots_manifest
    pack = tmp_path / 'pack'
    fixture = pack / 'fixture'
    minimal_graph(tmp_path)
    entry = minimal_graph(fixture)
    roots_manifest(pack, {'fixture/flow.md.html': {
        'base_root': 'fixture', 'pack_root': 'fixture', 'entry_root': 'base'}})
    selected = replace(load_launch_context(ROOT), base_root=tmp_path, pack_root=pack,
                       workflow_entry=None, workflow_base_root=None)
    viewed = _view_context(selected, entry)
    assert load_selected_definition(viewed).roots == {'base': fixture, 'pack': fixture}


def test_explicit_flow_can_use_parent_relative_paths_inside_selected_roots(tmp_path, monkeypatch):
    from dataclasses import replace
    from api_agent.runtime.view_flow import _view_context
    from test_workflow_viewer_discovery import minimal_graph
    entry = minimal_graph(tmp_path)
    child = tmp_path / 'child'
    child.mkdir()
    monkeypatch.chdir(child)
    selected = replace(load_launch_context(ROOT), base_root=tmp_path, pack_root=tmp_path,
                       workflow_entry=None, workflow_base_root=None)
    assert _view_context(selected, Path('../flow.md.html')) is selected


@pytest.fixture
def installed_view_context(tmp_path):
    from dataclasses import replace
    from test_workflow_viewer_discovery import minimal_graph, roots_manifest
    library = tmp_path / 'library'
    entry = minimal_graph(library / 'fixture')
    roots_manifest(library, {'fixture/flow.md.html': {
        'base_root': 'fixture', 'pack_root': 'fixture', 'entry_root': 'base'}})
    owned = frozenset(path.relative_to(library).as_posix() for path in library.rglob('*') if path.is_file())
    selected = replace(load_launch_context(ROOT), base_root=tmp_path / 'application',
                       pack_root=tmp_path / 'pack', workflow_base_root=library,
                       workflow_library_files=owned, workflow_entry=None)
    return selected, entry


def test_explicit_installed_mapping_preserves_rebased_file_ownership(installed_view_context):
    from api_agent.runtime.view_flow import _view_context
    selected, entry = installed_view_context
    mapped = _view_context(selected, entry)
    assert mapped.workflow_library_files == frozenset({'flow.md.html', 'work.md.html', 'native.py', 'schema.json'})
    assert load_selected_definition(mapped).entry == entry


def test_explicit_installed_mapping_rejects_unowned_mapping(installed_view_context):
    from dataclasses import replace
    from api_agent.runtime.view_flow import _view_context
    from api_agent.runtime.resource_pack import ResourcePackError
    selected, entry = installed_view_context
    selected = replace(selected, workflow_library_files=selected.workflow_library_files - {'cfg/workflow-view-roots.json'})
    with pytest.raises(ResourcePackError, match='mapping is not owned'):
        _view_context(selected, entry)


def test_explicit_installed_mapping_rejects_unowned_base_file(installed_view_context):
    from dataclasses import replace
    from api_agent.runtime.view_flow import _view_context
    from api_agent.runtime.resource_pack import ResourcePackError
    selected, entry = installed_view_context
    selected = replace(selected, workflow_library_files=selected.workflow_library_files - {'fixture/schema.json'})
    with pytest.raises(ResourcePackError, match='not owned'):
        load_selected_definition(_view_context(selected, entry))


def test_explicit_installed_mapping_rejects_unowned_pack_file(installed_view_context):
    from api_agent.runtime.resource_pack import ResourcePackError
    from test_workflow_viewer_discovery import roots_manifest
    selected, entry = installed_view_context
    library = selected.workflow_base_root
    roots_manifest(library, {'fixture/flow.md.html': {
        'base_root': 'fixture', 'pack_root': '.', 'entry_root': 'base'}})
    node = entry.parent / 'work.md.html'
    node.write_text(node.read_text() + '\n## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
                    r'| work | [prompt](\<resource_pack\>/prompt.md) | always |' + '\n')
    (library / 'prompt.md').write_text('Unowned prompt')
    with pytest.raises(ResourcePackError, match='not owned'):
        _prepare_view(selected, entry=entry)


def test_explicit_installed_view_accepts_owned_selected_files(installed_view_context, monkeypatch):
    selected, entry = installed_view_context
    bundle = object()
    monkeypatch.setattr(viewer_render, 'build_view_bundle', lambda *args, **kwargs: bundle)
    definition, _, _, actual, _ = _prepare_view(selected, entry=entry)
    assert definition.entry == entry
    assert actual is bundle


def test_explicit_source_mapping_does_not_borrow_installed_inventory(installed_view_context, monkeypatch):
    from api_agent.runtime.view_flow import _view_context
    from test_workflow_viewer_discovery import minimal_graph, roots_manifest
    selected, _ = installed_view_context
    entry = minimal_graph(selected.base_root / 'fixture')
    roots_manifest(selected.base_root, {'fixture/flow.md.html': {
        'base_root': 'fixture', 'pack_root': 'fixture', 'entry_root': 'base'}})
    mapped = _view_context(selected, entry)
    assert mapped.workflow_library_files is None
    bundle = object()
    monkeypatch.setattr(viewer_render, 'build_view_bundle', lambda *args, **kwargs: bundle)
    assert _prepare_view(selected, entry=entry)[3] is bundle
