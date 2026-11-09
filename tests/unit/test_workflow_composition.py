"""A pack owns its executable root while importing an admitted shared graph."""
import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.model import LayoutError
from api_agent.workflows.viewer_projection import project_view
from test_workflow_stateful import fixture_graph


def composition(pack, *, root='base', source='flow.md.html', extra=''):
    pack.mkdir(exist_ok=True)
    entry = pack / 'flow.md.html'
    entry.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| product |\n\n'
                     '## Import\n\n| Root | Path |\n| --- | --- |\n'
                     f'| {root} | {source} |\n' + extra)
    return entry


def test_import_keeps_shared_relative_links_and_admits_both_roots(fixture_graph):
    shared, source = fixture_graph
    entry = composition(shared / 'product')

    definition = load_definition(entry, base_root=shared, pack_root=entry.parent, entry_root='pack')

    assert definition.entry == entry and definition.entry_root == 'pack'
    assert definition.schema_source == shared / 'native.py'
    assert definition.graph_id == 'product'
    assert entry in definition.contents and source in definition.contents
    projection = project_view(definition)
    assert projection.entry_path == 'pack/flow.md.html'
    assert {d.logical_path for d in projection.documents} >= {'pack/flow.md.html', 'base/flow.md.html'}
    assert next(r for r in projection.references if r.kind == 'state_schema').owner_path == 'base/flow.md.html'


@pytest.mark.parametrize('root,source,extra', [
    ('unknown', 'flow.md.html', ''),
    ('pack', 'flow.md.html', ''),
    ('base', '../flow.md.html', ''),
    ('base', 'https://example.com/flow.md.html', ''),
    ('base', 'flow.md.html', '\n## State\n\n| Registry | Source |\n| --- | --- |\n| X | x.py |\n'),
])
def test_import_rejects_undeclared_roots_cycles_and_overlays(fixture_graph, root, source, extra):
    shared, _ = fixture_graph
    entry = composition(shared / 'product', root=root, source=source, extra=extra)

    with pytest.raises(LayoutError, match='workflow import'):
        load_definition(entry, base_root=shared, pack_root=entry.parent, entry_root='pack')


def test_static_import_publishes_only_the_pack_and_keeps_library_sources(fixture_graph):
    from api_agent.workflows.viewer_build import check_views, generate_views
    from test_workflow_viewer_render_controls import TinyRenderer

    root, _ = fixture_graph
    files = tuple(root.iterdir())
    shared = root / 'library'
    shared.mkdir()
    for path in files:
        path.rename(shared / path.name)
    generate_views(shared, renderer=TinyRenderer(), repository_url='//github.com/example/shared')
    before = {path.relative_to(shared): path.read_bytes() for path in shared.rglob('*') if path.is_file()}
    pack = root / 'product'
    entry = composition(pack)
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version=1\n[fields]\n')
    (pack / 'pack.toml').write_text('version=1\nid="product"\nconfig="cfg/config.toml"\n'
        '[workflow]\nversion=1\nroot="pack"\nentry="flow.md.html"\n'
        '[workflow.library]\nsource="../library"\ndistribution="shared"\nresource="shared_resources"\n')

    class RecordingRenderer(TinyRenderer):
        def __init__(self):
            self.links = []
        def render(self, source, *, allowed_links=frozenset()):
            self.links.extend(allowed_links)
            return super().render(source, allowed_links=allowed_links)
    renderer = RecordingRenderer()
    changed = generate_views(pack, renderer=renderer, repository_url='//github.com/example/product')
    check_views(pack)

    assert changed and all(path.is_relative_to(pack) for path in changed)
    assert {path.relative_to(shared): path.read_bytes() for path in shared.rglob('*') if path.is_file()} == before
    assert any(link.startswith('//github.com/example/shared/blob/') and link.endswith('/intake.md.html') for link in renderer.links)
    assert b'./launch.sh --view-flow' in entry.read_bytes()
    assert not (pack / 'intake.md.html').exists()


def test_reused_component_has_one_static_owner(fixture_graph):
    from api_agent.workflows.viewer_build import check_views, generate_views
    from test_workflow_viewer_render_controls import TinyRenderer

    root, entry = fixture_graph
    nested = root / 'nested'
    nested.mkdir()
    (nested / 'flow.md.html').write_text(entry.read_text().replace('| synthetic_flow |', '| nested_flow |')
        .replace('(native.py)', '(../native.py)').replace('(intake.md.html)', '(../intake.md.html)')
        .replace('(finish.md.html)', '(../finish.md.html)'))

    generate_views(root, renderer=TinyRenderer(), repository_url='//github.com/example/shared')
    check_views(root)

    assert (root / '.private_resources/diagrams/intake.svg').is_file()
    assert (root / '.private_resources/diagrams/nested/flow.svg').is_file()


def test_selected_library_roots_cannot_be_overridden_by_view_configuration(fixture_graph):
    import json
    from api_agent.workflows.viewer_build import read_view_roots
    from api_agent.workflows.viewer_render import RenderError

    root, _ = fixture_graph
    files = tuple(root.iterdir())
    shared = root / 'library'
    shared.mkdir()
    for path in files:
        path.rename(shared / path.name)
    pack = root / 'product'
    composition(pack)
    (pack / 'cfg').mkdir()
    (pack / 'alternate').mkdir()
    (pack / 'cfg/config.toml').write_text('version=1\n[fields]\n')
    (pack / 'pack.toml').write_text('version=1\nid="product"\nconfig="cfg/config.toml"\n'
        '[workflow]\nversion=1\nroot="pack"\nentry="flow.md.html"\n'
        '[workflow.library]\nsource="../library"\ndistribution="shared"\nresource="shared_resources"\n')
    (pack / 'cfg/workflow-view-roots.json').write_text(json.dumps({'version': 1, 'entries': {
        'flow.md.html': {'base_root': 'alternate', 'pack_root': '.', 'entry_root': 'pack'}}}))

    with pytest.raises(RenderError, match='owned by the pack manifest'):
        read_view_roots(pack)


def test_import_rejects_a_root_changed_during_admission(fixture_graph, monkeypatch):
    from api_agent.workflows import definition as loader
    shared, _ = fixture_graph
    entry = composition(shared / 'product')
    original = loader._read_bounded
    reads = 0
    def changed(path, root):
        nonlocal reads
        result = original(path, root)
        if path == entry:
            reads += 1
            if reads == 2:
                return result + b'\n'
        return result
    monkeypatch.setattr(loader, '_read_bounded', changed)

    with pytest.raises(LayoutError, match='changed during admission'):
        load_definition(entry, base_root=shared, pack_root=entry.parent, entry_root='pack')


def test_import_enforces_combined_snapshot_budget(fixture_graph, monkeypatch):
    from api_agent.workflows import definition as loader
    shared, source = fixture_graph
    entry = composition(shared / 'product')
    selected = load_definition(source, base_root=shared, pack_root=entry.parent)
    monkeypatch.setattr(loader, '_MAX_SNAPSHOT_BYTES', sum(map(len, selected.contents.values())))

    with pytest.raises(LayoutError, match='import snapshot exceeds'):
        load_definition(entry, base_root=shared, pack_root=entry.parent, entry_root='pack')


@pytest.mark.parametrize('owners,documents,message', [
    (frozenset(), None, 'publication owners'),
    ({'base'}, None, 'publication owners'),
    (frozenset({'foreign'}), None, 'publication owners'),
    (None, frozenset(), 'publication documents'),
    (None, {'base/flow.md.html'}, 'publication documents'),
    (None, frozenset({'base/foreign.md.html'}), 'publication documents'),
])
def test_static_publication_requires_owned_entry(fixture_graph, owners, documents, message):
    from api_agent.workflows.viewer_render import _publication_documents, RenderError
    root, entry = fixture_graph
    projection = project_view(load_definition(entry, base_root=root, pack_root=root))

    with pytest.raises(RenderError, match=message):
        _publication_documents(projection, owners, documents)


def test_external_library_identity_must_exist_before_static_publication(fixture_graph):
    from api_agent.workflows.viewer_build import generate_views
    from api_agent.workflows.viewer_render import RenderError
    from test_workflow_viewer_render_controls import TinyRenderer
    root, _ = fixture_graph
    files = tuple(root.iterdir())
    shared = root / 'library'
    shared.mkdir()
    for path in files:
        path.rename(shared / path.name)
    pack = root / 'product'
    composition(pack)
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version=1\n[fields]\n')
    (pack / 'pack.toml').write_text('version=1\nid="product"\nconfig="cfg/config.toml"\n'
        '[workflow]\nversion=1\nroot="pack"\nentry="flow.md.html"\n'
        '[workflow.library]\nsource="../library"\ndistribution="shared"\nresource="shared_resources"\n')
    before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}

    with pytest.raises(RenderError, match='library repository identity missing'):
        generate_views(pack, renderer=TinyRenderer(), repository_url='//github.com/example/product')

    assert {path: path.read_bytes() for path in root.rglob('*') if path.is_file()} == before


def test_static_hierarchies_cannot_publish_conflicting_bytes(fixture_graph, monkeypatch):
    from dataclasses import replace
    from api_agent.workflows import viewer_build
    from api_agent.workflows.viewer_render import RenderError
    from test_workflow_viewer_render_controls import TinyRenderer
    root, entry = fixture_graph
    projection = project_view(load_definition(entry, base_root=root, pack_root=root))
    original = viewer_build.build_view_bundle
    calls = 0
    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        bundle = original(*args, **kwargs)
        if calls == 2:
            first, *rest = bundle.files
            return replace(bundle, files=(replace(first, body=first.body + b'\n'), *rest))
        return bundle
    monkeypatch.setattr(viewer_build, 'build_view_bundle', changed)

    with pytest.raises(RenderError, match='hierarchies disagree on shared output'):
        viewer_build._bundle_files((projection, projection), roots={'base': root, 'pack': root},
                                   renderer=TinyRenderer(), verify=False, hosted_selected=False)
