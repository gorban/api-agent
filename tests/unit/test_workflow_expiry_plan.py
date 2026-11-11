"""Compiled plans and diagrams carry expiry edges and the Timeouts rows that enable them."""
import asyncio
from html import unescape

import pytest

from api_agent.workflows.artifact import plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.execution import diagram_layout
from api_agent.workflows.identity import workflow_identity
from api_agent.workflows.model import LayoutError
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source
from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from expiry_support import NATIVE, REPLAY, ROUTE, expiring
from loop_support import load


@pytest.fixture
def compiled_plan(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = expiring(root, *REPLAY)
    wheel = build_wheel(tmp_path, 'aq-expiry', {'aq_expiry/__init__.py': b'', 'aq_expiry/native.py': NATIVE.encode()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path), entry


def run(compiled, script):
    native = compiled.modules['aq_expiry.native']
    native.SCRIPT['work'] = list(script)
    asyncio.run(compiled.workflow().arun({'accept_at': 1}))
    return native.CALLS


def test_a_compiled_plan_keeps_the_expiry_edges_and_the_timeouts_row(compiled_plan):
    plan, _ = compiled_plan

    compiled = load_built(plan)

    assert [row['Owner'] for row in compiled.layout.tables['Timeouts']] == ['work']
    assert [row['When'] for row in compiled.layout.tables['Edges'] if 'expired' in row['When']] == [
        'work.expired and work.attempt < NODE_TIMEOUT_PASSES',
        'work.expired and work.attempt >= NODE_TIMEOUT_PASSES']


def test_a_compiled_plan_replays_and_routes_expiries_like_its_source(compiled_plan):
    plan, _ = compiled_plan

    assert run(load_built(plan), ['hang', 'ok']) == ['work', 'work', 'finish']
    assert run(load_built(plan), ['hang', 'hang']) == ['work', 'work', 'timed_out']


def test_the_compiled_identity_matches_the_source_definition(compiled_plan):
    plan, entry = compiled_plan
    arguments = dict(timeouts={'work': 0.1}, component_passes={}, limits={'NODE_TIMEOUT_PASSES': 2})

    assert workflow_identity(load_built(plan), **arguments) == workflow_identity(load(entry), **arguments)


def test_a_plan_whose_timeouts_row_was_removed_cannot_run_its_expiry_edges(compiled_plan):
    plan, _ = compiled_plan

    def mutate(value):
        entries = value['layout']['tables']['$m']
        position = next(i for i, (name, _) in enumerate(entries) if name == 'Timeouts')
        entries[position][1] = []

    with pytest.raises(LayoutError, match='work.expired requires work to declare its own Timeouts row'):
        load_built(plan, edited(plan.plan, mutate)).workflow()


def test_diagrams_show_the_expiry_condition_as_the_edge_label(tmp_path):
    definition = load(expiring(tmp_path, *ROUTE))

    assert '|work.expired|' in diagram_layout(definition.layout)
    projection = project_view(definition)
    document = next(item for item in projection.documents if item.stable_id == 'loops')
    text, _ = _projection_source(projection, document)
    assert any(line.endswith(': work.expired') for line in unescape(text.decode()).splitlines())
