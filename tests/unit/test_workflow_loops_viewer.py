"""Diagrams show a loop's backward edges and the configuration key that bounds them."""
from html import unescape

from api_agent.workflows.execution import diagram_layout
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source
from loop_support import edges, graph, limits, load
from test_workflow_loops import RETRY, retry


def source(projection, stable_id):
    document = next(item for item in projection.documents if item.stable_id == stable_id)
    text, links = _projection_source(projection, document)
    return unescape(text.decode()), links


def test_limits_are_projected_as_configuration_references_with_their_owner(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')], sections=limits('REPLAN_PASSES'),
                  components={'group': (['draft', 'verify'], limits('PLAN_PASSES') + edges(
                      RETRY, ('verify', 'END', 'otherwise')))})

    projection = project_view(load(entry))

    assert {(ref.kind, ref.owner_id, dict(ref.attributes)['key'], ref.target_path)
            for ref in projection.references if ref.kind == 'limit'} == {
        ('limit', None, 'REPLAN_PASSES', 'base/config.toml'), ('limit', 'group', 'PLAN_PASSES', 'base/config.toml')}


def test_declaring_diagrams_show_backward_edges_and_their_configuration_key(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')], sections=limits('REPLAN_PASSES'),
                  components={'group': (['draft', 'verify'], limits('PLAN_PASSES') + edges(
                      RETRY, ('verify', 'END', 'otherwise')))})
    projection = project_view(load(entry))

    root, root_links = source(projection, 'loops')
    group, group_links = source(projection, 'group')

    assert 'Configuration: REPLAN_PASSES' in root and 'Configuration: PLAN_PASSES' not in root
    assert 'Configuration: PLAN_PASSES' in group and 'REPLAN_PASSES' not in group
    # Aliases follow declaration order: draft is n0 and verify is n1, so this arrow points back.
    assert 'n1 --> n0 : not verify.accepted and draft.attempt < PLAN_PASSES' in group
    assert any(link.endswith('config.toml') for link in root_links | group_links)


def test_root_loops_render_with_the_limit_in_the_edge_label(tmp_path):
    projection = project_view(load(retry(tmp_path)))

    text, _ = source(projection, 'loops')

    assert 'Configuration: PLAN_PASSES' in text
    assert any('draft.attempt < PLAN_PASSES' in line for line in text.splitlines())


def test_the_text_diagram_lists_the_backward_edge(tmp_path):
    layout = load(retry(tmp_path)).layout

    assert '|not verify.accepted and draft.attempt < PLAN_PASSES|' in diagram_layout(layout)
