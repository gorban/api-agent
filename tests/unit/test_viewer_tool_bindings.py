"""Static tool dependencies follow admitted lexical scopes, not runtime calls."""
from dataclasses import replace
from hashlib import sha256
from html import unescape
from pathlib import Path

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import RenderError, _projection_source
from test_workflow_viewer_projection import graph


@pytest.fixture
def selected(tmp_path):
    entry = graph(tmp_path)
    entry.write_text(entry.read_text().replace('[node work](children/work.md.html)',
        '[component branch](children/branch.md.html)') +
        '\n## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| branch | 1 | - |\n'
        '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n| root_port | [shared](shared.toml) |\n')
    (tmp_path / 'children/branch.md.html').write_text('# Component v2: branch\n\n## Flow\n\n'
        '1.  [node work](work.md.html)\n\n## Tools\n\n| Port | Instance |\n| --- | --- |\n'
        '| component_port | [shared](../shared.toml) |\n')
    node = tmp_path / 'children/work.md.html'
    node.write_text(node.read_text() + '\n## Tools\n\n| Port | Instance |\n| --- | --- |\n'
        '| first | [shared](../shared.toml) |\n| second | [other](../other.toml) |\n')
    (tmp_path / 'shared.toml').write_text('version = 1\n')
    (tmp_path / 'other.toml').write_text('version = 1\n')
    return entry


def admit(entry):
    return project_view(load_definition(entry, base_root=entry.parent, pack_root=entry.parent))


@pytest.mark.parametrize('scope,ports,absent', [
    ('example', ('root_port', 'component_port'), ('first', 'second')),
    ('branch', ('component_port', 'first', 'second'), ('root_port',)),
    ('work', ('first', 'second'), ('root_port', 'component_port')),
])
def test_diagrams_show_declared_tool_ports_on_their_owning_scope(selected, scope, ports, absent):
    view = admit(selected)
    document = next(item for item in view.documents if item.stable_id == scope)
    raw, links = _projection_source(view, document)
    source = unescape(raw.decode())
    for port in ports:
        assert f'declares port {port}' in source
    for port in absent:
        assert f'declares port {port}' not in source
    assert '.. toolbinding' in source
    assert '--> toolbinding' not in source
    assert not any(link.endswith('.toml') for link in links)
    assert all(binding.instance_sha256 == sha256(next(resource.content_bytes for resource in view.resources
        if resource.logical_path == binding.instance_path)).hexdigest() for binding in view.tool_bindings)
    assert len(view.tool_bindings) == 4


def test_changed_instance_bytes_change_view_provenance_without_changing_execution_edges(selected):
    before = admit(selected)
    (selected.parent / 'shared.toml').write_text('version = 2\n')
    after = admit(selected)
    assert before.edges == after.edges
    assert before.projection_sha256 != after.projection_sha256
    assert before.tool_bindings[0].instance_sha256 != after.tool_bindings[0].instance_sha256


@pytest.mark.parametrize('changes', [
    {'scope_kind': 'unknown'}, {'scope': 'unknown'}, {'declaration_path': 'base/other.md.html'},
    {'instance_path': 'base/missing.toml'}, {'instance_sha256': '0' * 64},
])
def test_renderer_rejects_tool_bindings_that_differ_from_admitted_sources(selected, changes):
    view = admit(selected)
    binding = replace(view.tool_bindings[0], **changes)
    with pytest.raises(RenderError, match='tool binding differs'):
        _projection_source(replace(view, tool_bindings=(binding,)), view.documents[0])


def test_native_source_change_invalidates_provenance_even_when_diagram_markup_is_unchanged(selected):
    before = admit(selected)
    native = selected.parent / 'native.py'
    native.write_text(native.read_text() + '\n# A reviewed implementation change.\n')
    after = admit(selected)
    assert before.projection_sha256 != after.projection_sha256
    assert _projection_source(before, before.documents[0]) == _projection_source(after, after.documents[0])


@pytest.mark.parametrize('limit,accepted', [(10, True), (9, False)])
def test_mixed_tool_dependencies_and_cross_scope_boundaries_share_one_record_budget(selected, monkeypatch, limit, accepted):
    from api_agent.workflows import viewer_render
    from api_agent.workflows.viewer_projection import ViewerEdge
    view = admit(selected)
    crossing = ViewerEdge('branch', 'work', 'branch', 'always', False)
    view = replace(view, edges=(*view.edges, crossing))
    # One visible step, three routes, one cross-scope boundary, one graph
    # declaration stub, two tool instances and two dependency edges: ten.
    monkeypatch.setattr(viewer_render, '_MAX_RECORDS', limit)
    if not accepted:
        with pytest.raises(RenderError, match='record limit'):
            _projection_source(view, view.documents[0])
        return
    source, _ = _projection_source(view, view.documents[0])
    assert b'boundary0' in source and b'toolscope0' in source
    assert source.count(b'declares port') == 2


def test_invisible_tool_bindings_do_not_consume_the_displayed_scope_budget(selected, monkeypatch):
    from api_agent.workflows import viewer_render
    view = admit(selected)
    node_binding = next(binding for binding in view.tool_bindings if binding.scope == 'work')
    invisible = tuple(replace(node_binding, port=f'unshown_{index}') for index in range(100))
    expected = _projection_source(view, view.documents[0])
    # Eight visible records: one step, two routes, a graph stub and two
    # instance/dependency pairs. The nested node's bindings are not shown.
    monkeypatch.setattr(viewer_render, '_MAX_RECORDS', 8)
    assert _projection_source(replace(view, tool_bindings=(*view.tool_bindings, *invisible)),
                              view.documents[0]) == expected


def test_multiple_graph_ports_share_one_declaration_stub(selected):
    selected.write_text(selected.read_text() + '| again | [shared](shared.toml) |\n')
    view = admit(selected)
    source, _ = _projection_source(view, view.documents[0])
    assert source.count(b'as toolscope0 ') == 1
    assert source.count(b'declares port') == 3


@pytest.mark.parametrize('scope,expected', [
    ('example', ('graph-example-root_port', 'component-branch-component_port')),
    ('branch', ('component-branch-component_port', 'node-work-first', 'node-work-second')),
    ('work', ('node-work-first', 'node-work-second')),
])
def test_instance_links_select_the_owning_port_row(selected, scope, expected):
    view = admit(selected)
    document = next(item for item in view.documents if item.stable_id == scope)
    source, links = _projection_source(view, document)
    for fragment in expected:
        target = '#workflow-tool-' + fragment
        assert any(link.endswith(target) for link in links)
        assert target.encode() in source


@pytest.mark.parametrize('scope,fragment', [
    ('example', 'graph-example'), ('branch', 'component-branch'),
])
def test_tool_scope_stub_links_to_its_tools_section(selected, scope, fragment):
    view = admit(selected)
    document = next(item for item in view.documents if item.stable_id == scope)
    source, links = _projection_source(view, document)
    target = '#workflow-tools-' + fragment
    assert any(link.endswith(target) for link in links)
    assert b'as toolscope0 [[' in source
    assert target.encode() in source


def test_tool_owner_links_use_the_selected_repository_identity(selected):
    from api_agent.workflows.viewer_repository import RepositoryIdentity
    view = admit(selected)
    identity = RepositoryIdentity('//github.com/example/viewer')
    _, links = _projection_source(view, view.documents[0], repositories={'base': identity})
    assert identity.link('flow.md.html') in links
    assert not any(link.endswith('.toml') for link in links)


@pytest.mark.parametrize('change', ['missing', 'wrong_kind'])
def test_projection_requires_the_admitted_tool_instance_resource(selected, change):
    definition = load_definition(selected, base_root=selected.parent, pack_root=selected.parent)
    target = next(resource for resource in definition.resources if resource.kind == 'tool_instance')
    resources = tuple(resource for resource in definition.resources if resource is not target)
    if change == 'wrong_kind':
        resources += (replace(target, kind='source'),)
    with pytest.raises(ValueError, match='missing captured tool instance'):
        project_view(replace(definition, resources=resources))


def test_graph_tool_owner_requires_its_repository_even_when_visible_steps_are_overridden(selected):
    from api_agent.workflows.definition import Replacement
    from api_agent.workflows.viewer_repository import RepositoryIdentity
    root = selected.parent
    pack = root / 'pack'
    pack.mkdir()
    for filename in ('native.py', 'schema.json', 'shared.toml', 'other.toml'):
        (pack / filename).write_bytes((root / filename).read_bytes())
    for filename in ('branch.md.html', 'work.md.html'):
        text = (root / 'children' / filename).read_text()
        (pack / filename).write_text(text.replace('../', ''))
    definition = load_definition(selected, base_root=root, pack_root=pack,
        replacements=(Replacement('branch', Path('branch.md.html'), 'pack'),))
    view = project_view(definition)
    with pytest.raises(RenderError, match='missing workflow repository identity'):
        _projection_source(view, view.documents[0],
            repositories={'pack': RepositoryIdentity('//github.com/example/pack')})


@pytest.mark.parametrize('case', ['shared_primary', 'independent'])
def test_tool_conversation_explanations_remain_admitted_notes(case):
    root = Path(__file__).resolve().parents[2]
    entry = root / 'tests/integration/fixtures/tool_instances' / case / 'flow.md.html'
    definition = load_definition(entry, base_root=root, pack_root=root)
    view = project_view(definition)
    document = next(document for document in view.documents if document.stable_id == 'conversation')
    source, _ = _projection_source(view, document)
    assert source.count(b'..> toolbinding') == 2
    assert b'uses port' not in source
    assert b'## Notes' in document.raw_bytes
    assert b'`converse` is not an API tool' in document.raw_bytes


@pytest.mark.parametrize('colliding_id', ['branch', 'work'])
def test_graph_tool_scope_stays_distinct_when_graph_and_step_ids_match(selected, colliding_id):
    selected.write_text(selected.read_text().replace('| example |', f'| {colliding_id} |'))
    view = admit(selected)
    root_document = next(document for document in view.documents
                         if document.logical_path == view.entry_path)
    source, _ = _projection_source(view, root_document)
    rendered = unescape(source.decode())
    assert f'Graph tool declarations: {colliding_id}' in rendered
    assert 'toolscope0 .. toolbinding0 : declares port root_port' in rendered
    for document in view.documents:
        if document.logical_path != view.entry_path:
            nested, _ = _projection_source(view, document)
            nested_rendered = unescape(nested.decode())
            assert 'declares port root_port' not in nested_rendered
            expected_ports = ('component_port',) if document.stable_id == 'branch' else ('first', 'second')
            for port in expected_ports:
                assert f'declares port {port}' in nested_rendered


def test_declared_tool_instance_links_are_served_from_captured_bytes(selected):
    import asyncio
    from api_agent.runtime.viewer_host import start_viewer_host
    from api_agent.workflows.viewer_render import build_view_bundle
    from test_workflow_viewer_discovery import TinyRenderer
    from test_viewer_host import request, wire
    view = admit(selected)
    expected = (selected.parent / 'shared.toml').read_bytes()
    bundle = build_view_bundle(view, roots={'base': selected.parent, 'pack': selected.parent},
                               renderer=TinyRenderer(), verify=False)
    async def exercise():
        host = await start_viewer_host(bundle, projection=view)
        try:
            (selected.parent / 'shared.toml').write_text('version = 9\n')
            response = await request(host.port, wire('GET', '/r/base/shared.toml', host.port))
            assert response.startswith(b'HTTP/1.1 200 OK')
            assert response.endswith(expected)
            missing = await request(host.port, wire('GET', '/r/base/unselected.toml', host.port))
            assert missing.startswith(b'HTTP/1.1 404')
        finally:
            await host.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['missing', 'wrong_hash', 'wrong_kind', 'invalid_toml', 'oversize'])
def test_tool_instance_display_rejects_unproven_resources(selected, change, monkeypatch):
    from api_agent.workflows import viewer_render
    view = admit(selected)
    resource = next(item for item in view.resources if item.logical_path == view.tool_bindings[0].instance_path)
    if change == 'missing':
        view = replace(view, resources=tuple(item for item in view.resources if item != resource))
    else:
        fields = {'wrong_hash': {'sha256': '0' * 64}, 'wrong_kind': {'kind': 'native'},
                  'invalid_toml': {'content_bytes': b'broken ['}, 'oversize': {}}[change]
        if change == 'invalid_toml':
            fields['sha256'] = sha256(fields['content_bytes']).hexdigest()
            view = replace(view, tool_bindings=tuple(replace(binding, instance_sha256=fields['sha256'])
                           if binding.instance_path == resource.logical_path else binding
                           for binding in view.tool_bindings))
        replaced = replace(resource, **fields)
        view = replace(view, resources=tuple(replaced if item == resource else item for item in view.resources))
        if change == 'oversize':
            monkeypatch.setattr(viewer_render, '_MAX_REFERENCE', 0)
            view = replace(view, references=())
    with pytest.raises(RenderError, match='tool instance'):
        viewer_render._reference_sources(view)


def test_tool_only_flow_keeps_hosted_instance_links(selected):
    from api_agent.workflows.viewer_render import _hosted_declaration
    view = replace(admit(selected), references=())
    document = next(item for item in view.documents if item.stable_id == 'work')
    hosted = _hosted_declaration(view, document)
    assert b'[shared](../shared.toml)' in hosted
    assert b'[other](../other.toml)' in hosted
    assert b'captured tool_instance:' not in hosted


@pytest.fixture
def consuming(selected):
    node = selected.parent / 'children/work.md.html'
    node.write_text(node.read_text().replace('| Port | Instance |\n| --- | --- |', '| Port | Instance | Operations |\n| --- | --- | --- |')
                    .replace('| first | [shared](../shared.toml) |', '| first | [shared](../shared.toml) | describe, prepare |')
                    .replace('| second | [other](../other.toml) |', '| second | [other](../other.toml) | execute |'))
    return selected


@pytest.mark.parametrize('scope', ['example', 'branch', 'work'])
def test_declared_consumers_keep_unlabeled_edges_and_clickable_instances(consuming, scope):
    view = admit(consuming)
    document = next(item for item in view.documents if item.stable_id == scope)
    source, links = _projection_source(view, document)
    text = unescape(source.decode())
    dependencies = [line for line in text.splitlines() if '..> toolbinding' in line]
    assert len(dependencies) == 2
    assert all(' : ' not in line and '[[' not in line for line in dependencies)
    assert 'uses port' not in text
    assert any('#workflow-tool-' in link for link in links)
    assert all(f'[[{link}]]' in text for link in links if '#workflow-tool-' in link)
    assert 'Operations' in (consuming.parent / 'children/work.md.html').read_text()
    # One shared instance box connects availability and consumption, rather than
    # creating another configured owner or implying an executable flow edge.
    assert text.count('Tool instance: shared') == (2 if scope == 'example' else 1)
    assert all(binding.operations == ('describe', 'prepare') for binding in view.tool_bindings
               if binding.scope == 'work' and binding.port == 'first')


def test_tool_operation_changes_affect_view_identity_not_control_flow(consuming):
    before = admit(consuming)
    node = consuming.parent / 'children/work.md.html'
    node.write_text(node.read_text().replace('describe, prepare', 'describe'))
    after = admit(consuming)
    assert before.projection_sha256 != after.projection_sha256
    assert before.edges == after.edges


@pytest.mark.parametrize('operations', [('unknown',), ('execute', 'execute'), ([],), ['execute']])
def test_renderer_rejects_invalid_captured_consumer_operations(consuming, operations):
    view = admit(consuming)
    binding = next(item for item in view.tool_bindings if item.scope == 'work')
    with pytest.raises(RenderError, match='invalid captured tool operations'):
        _projection_source(replace(view, tool_bindings=(replace(binding, operations=operations),)),
                           view.documents[0])


def test_consumer_operations_are_not_graph_availability(selected):
    view = admit(selected)
    binding = replace(view.tool_bindings[0], operations=('execute',))
    with pytest.raises(RenderError, match='invalid captured tool operations'):
        _projection_source(replace(view, tool_bindings=(binding,)), view.documents[0])
