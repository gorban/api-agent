"""Each document presents its immediate execution scope and explicit boundaries."""
import re
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.stateful import load_stateful_definition
from api_agent.workflows.viewer_projection import ViewerEdge, project_view
from api_agent.workflows.viewer_render import RenderError, _projection_source
from api_agent.workflows.viewer_repository import RepositoryIdentity

ROOT = Path(__file__).resolve().parents[2]


def synthetic_projection(specifications, edges):
    entry = SimpleNamespace(logical_path='base/flow.md.html', stable_id='graph')
    documents = [entry]
    steps = []
    for identifier, kind, parent, declaration in specifications:
        documents.append(SimpleNamespace(logical_path=declaration, stable_id=identifier))
        steps.append(SimpleNamespace(id=identifier, kind=kind, parent=parent,
                                     declaration_path=declaration))
    return SimpleNamespace(entry_path=entry.logical_path, documents=tuple(documents),
                           steps=tuple(steps), edges=tuple(edges), references=(), resources=())


def source_for(projection, stable_id):
    document = next(document for document in projection.documents if document.stable_id == stable_id)
    source, links = _projection_source(projection, document)
    return unescape(source.decode()), links


def rectangles(source):
    return re.findall(r'^rectangle "([^\"]*)" as (n\d+|boundary\d+)', source, flags=re.MULTILINE)


def execution_routes(source):
    return [line for line in source.splitlines() if ' --> ' in line]


@pytest.fixture
def nested_projection():
    specifications = [
        ('group', 'component', None, 'base/group.md.html'),
        ('nested', 'component', 'group', 'pack/selected/nested.md.html'),
        ('first', 'node', 'nested', 'base/first.md.html'),
        ('second', 'node', 'nested', 'base/second.md.html'),
        ('single', 'component', 'group', 'base/single.md.html'),
        ('only', 'node', 'single', 'base/only.md.html'),
        ('after', 'node', None, 'base/after.md.html'),
    ]
    edges = [
        ViewerEdge('GRAPH', 'START', 'group', 'always', True),
        ViewerEdge('GRAPH', 'group', 'after', 'next', False),
        ViewerEdge('GRAPH', 'after', 'END', 'done', False),
        ViewerEdge('group', 'START', 'nested', 'always', True),
        ViewerEdge('group', 'nested', 'single', 'ready', False),
        ViewerEdge('group', 'single', 'END', 'always', True),
        ViewerEdge('nested', 'START', 'first', 'always', True),
        ViewerEdge('nested', 'first', 'second', 'route', False),
        ViewerEdge('nested', 'second', 'END', 'always', True),
        ViewerEdge('single', 'START', 'only', 'always', True),
        ViewerEdge('single', 'only', 'END', 'always', True),
    ]
    return synthetic_projection(specifications, edges)


def test_original_bundled_root_has_one_opaque_component_and_scoped_route():
    definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    projection = project_view(definition)

    source, links = source_for(projection, 'simple_chat')

    assert rectangles(source) == [('agent', 'n0')]
    assert execution_routes(source) == ['start --> n0 : always', 'n0 --> end : always']
    assert 'package ' not in source
    assert 'llm_call' not in source
    assert '../../workflows/components/agent.md.html' in links


def test_original_component_shows_child_once_without_a_self_execution_link():
    projection = project_view(load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT))

    source, links = source_for(projection, 'agent')

    assert rectangles(source) == [('llm_call', 'n0')]
    assert execution_routes(source) == ['start --> n0 : always', 'n0 --> end : always']
    assert any(link.endswith('/agent.md.html#node-llm_call') for link in links)
    assert 'package ' not in source


def test_inline_node_has_fragment_navigation_without_an_invented_document():
    projection = project_view(load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT))

    source, links = source_for(projection, 'agent')

    assert rectangles(source) == [('llm_call', 'n0')]
    assert any(link.endswith('/agent.md.html#node-llm_call') for link in links)
    assert all(document.stable_id != 'llm_call' for document in projection.documents)


def test_nested_and_sibling_components_are_opaque_and_counts_follow_all_descendants(nested_projection):
    root_source, root_links = source_for(nested_projection, 'graph')
    component_source, component_links = source_for(nested_projection, 'group')

    assert rectangles(root_source) == [('group · 3 registered nodes', 'n0'), ('after', 'n1')]
    assert execution_routes(root_source) == ['start --> n0 : always', 'n0 --> n1 : next', 'n1 --> end : done']
    assert rectangles(component_source) == [('nested · 2 registered nodes', 'n0'), ('single', 'n1')]
    assert execution_routes(component_source) == ['start --> n0 : always', 'n0 --> n1 : ready', 'n1 --> end : always']
    assert 'registered nodes' not in source_for(nested_projection, 'single')[0]
    assert all('first.md.html' not in link and 'second.md.html' not in link for link in root_links)
    assert any('selected/nested.md.html' in link for link in component_links)


def test_recursive_count_is_independent_of_step_order_and_excludes_containers(nested_projection):
    nested_projection.steps = tuple(reversed(nested_projection.steps))

    source, _ = source_for(nested_projection, 'graph')

    assert ('group · 3 registered nodes', 'n1') in rectangles(source)
    assert ' · 4 registered nodes' not in source


def test_zero_descendant_count_is_explicit_instead_of_silently_treated_as_one():
    projection = synthetic_projection([('empty', 'component', None, 'base/empty.md.html')], [])

    source, _ = source_for(projection, 'graph')

    assert rectangles(source) == [('empty · 0 registered nodes', 'n0')]


def test_cross_scope_target_is_linked_boundary_context_without_ancestor_remapping(nested_projection):
    nested_projection.edges += (ViewerEdge('GRAPH', 'after', 'first', 'foreign_target', False),)

    source, links = source_for(nested_projection, 'graph')

    assert ('Boundary: first · scope nested', 'boundary0') in rectangles(source)
    assert 'n1 ..> boundary0 : boundary (GRAPH): foreign_target' in source
    assert 'n1 --> n0 : foreign_target' not in source
    assert any(link.endswith('/first.md.html') for link in links)
    assert not any('foreign_target' in line for line in execution_routes(source))


def test_foreign_owner_incoming_route_does_not_merge_scope_boundaries(nested_projection):
    nested_projection.edges += (
        ViewerEdge('nested', 'first', 'after', 'external_entry', False),
        ViewerEdge('foreign', 'START', 'after', 'foreign_start', False),
    )

    source, _ = source_for(nested_projection, 'graph')

    assert 'boundary0 ..> n1 : boundary (nested): external_entry' in source
    assert ('Boundary: START · scope foreign', 'boundary1') in rectangles(source)
    assert 'boundary1 ..> n1 : boundary (foreign): foreign_start' in source
    assert 'start --> n1 : foreign_start' not in source
    assert execution_routes(source) == ['start --> n0 : always', 'n0 --> n1 : next', 'n1 --> end : done']


def test_component_context_does_not_expand_foreign_internal_routes(nested_projection):
    nested_projection.edges += (ViewerEdge('nested', 'first', 'after', 'external_exit', False),)

    source, _ = source_for(nested_projection, 'group')

    assert 'external_exit' not in source
    assert len(rectangles(source)) == 2


def test_duplicate_boundary_endpoints_are_one_context_node(nested_projection):
    nested_projection.edges += (
        ViewerEdge('GRAPH', 'after', 'first', 'first_route', False),
        ViewerEdge('GRAPH', 'group', 'first', 'second_route', False),
    )

    source, _ = source_for(nested_projection, 'graph')

    assert sum(label.startswith('Boundary: first') for label, _ in rectangles(source)) == 1
    assert 'n1 ..> boundary0 : boundary (GRAPH): first_route' in source
    assert 'n0 ..> boundary0 : boundary (GRAPH): second_route' in source


def test_repository_links_keep_exact_selected_component_and_boundary_targets(nested_projection):
    document = nested_projection.documents[0]
    nested_projection.edges += (ViewerEdge('GRAPH', 'after', 'first', '', False),)
    repositories = {'base': RepositoryIdentity('//github.com/example/base'),
                    'pack': RepositoryIdentity('//github.com/example/pack')}

    source, links = _projection_source(nested_projection, document, repositories=repositories)

    assert b'boundary (GRAPH): route' in source
    assert '//github.com/example/base/blob/_____switch_branch_____/first.md.html' in links
    component = next(item for item in nested_projection.documents if item.stable_id == 'group')
    _, selected_links = _projection_source(nested_projection, component, repositories=repositories)
    assert '//github.com/example/pack/blob/_____switch_branch_____/selected/nested.md.html' in selected_links


def test_unknown_nested_document_does_not_fall_back_to_another_execution_scope(nested_projection):
    unknown = SimpleNamespace(logical_path='base/unselected.md.html', stable_id='group')

    with pytest.raises(RenderError, match='document scope'):
        _projection_source(nested_projection, unknown)


def test_boundary_context_counts_toward_output_record_limit():
    projection = synthetic_projection([
        ('local', 'node', None, 'base/local.md.html'),
        ('foreign', 'node', 'elsewhere', 'base/foreign.md.html'),
    ], [ViewerEdge('GRAPH', 'local', 'foreign', 'cross', False)] * 999)

    with pytest.raises(RenderError, match='record limit'):
        source_for(projection, 'graph')


def test_guarded_components_keep_only_their_pass_nodes_and_declared_conditions():
    definition = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    projection = project_view(definition)

    root_source, _ = source_for(projection, 'contrived_qualification')
    first_source, first_links = source_for(projection, 'first')

    assert all(label in {'first · 3 registered nodes', 'second · 3 registered nodes'} for label, _ in rectangles(root_source))
    assert {label for label, _ in rectangles(first_source)} == {'first_draft', 'first_format', 'first_guard'}
    assert len(execution_routes(first_source)) == 4
    assert 'second_draft' not in first_source
    assert 'first_draft.route == "skip"' in root_source
    assert 'not .accepted and attempt < WORKFLOW_MAX_PASSES' in first_source
    assert 'not .accepted and attempt == WORKFLOW_MAX_PASSES' in first_source
    assert 'Configuration: WORKFLOW_MAX_PASSES' in first_source
    assert any('/contracts/' in link and link.endswith('.md.html') for link in first_links)
    assert next(controller for controller in projection.controllers
                if controller.component_id == 'first').max_passes_key == 'WORKFLOW_MAX_PASSES'


def test_real_v3_router_is_scoped_without_leaking_child_start_end_to_root(tmp_path):
    (tmp_path / 'native.py').write_text('from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n')
    (tmp_path / 'work.md.html').write_text(
        '# Node v3: work\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| work | deterministic | [Python](native.py) |\n')
    (tmp_path / 'group.md.html').write_text('# Component v3: group\n\n## Flow\n\n1.  [node work](work.md.html)\n')
    entry = tmp_path / 'flow.md.html'
    entry.write_text(
        '# Workflow v3\n\n## Graph ID\n\n| ID |\n| --- |\n| scoped_v3 |\n\n'
        '## State\n\n| Registry | Source |\n| --- | --- |\n| State | [Python](native.py) |\n\n'
        '## Flow\n\n1.  [component group](group.md.html)\n\n'
        '## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
        '| group | END | - | - |\n| work | END | choose | [Python](native.py) |\n')
    projection = project_view(load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path))

    root_source, _ = source_for(projection, 'scoped_v3')
    component_source, _ = source_for(projection, 'group')

    assert rectangles(root_source) == [('group', 'n0')]
    assert execution_routes(root_source) == ['start --> n0 : always', 'n0 --> end : always']
    assert rectangles(component_source) == [('work', 'n0')]
    assert execution_routes(component_source) == ['start --> n0 : always', 'n0 --> end : choose']


def test_recursive_component_count_terminates_on_repeated_component_identity():
    projection = synthetic_projection([
        ('group', 'component', None, 'base/group.md.html'),
        ('loop', 'component', 'group', 'base/loop.md.html'),
        ('group', 'component', 'loop', 'base/group.md.html'),
        ('task', 'node', 'loop', 'base/task.md.html'),
    ], ())
    source, _ = source_for(projection, 'graph')
    assert 'rectangle "group" as n0' in source


def test_repository_mapping_must_include_every_displayed_declaration_owner(nested_projection):
    document = next(item for item in nested_projection.documents if item.stable_id == 'group')
    with pytest.raises(RenderError, match='missing workflow repository identity'):
        _projection_source(nested_projection, document, repositories={
            'base': RepositoryIdentity('//github.com/example/base'),
        })
