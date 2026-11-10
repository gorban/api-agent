"""A compiled plan keeps TURN bindings and checks them as strictly as source admission."""
import asyncio

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.identity import declaration_identity

from session_workflow_support import load_session, session_entry
from test_workflow_turn import FIXTURE


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    directory = tmp_path_factory.mktemp('turn_plan')
    root = directory / 'tree'
    entry = session_entry(root, fixture=FIXTURE, flow='turn_chat.md.html')
    package = FIXTURE / 'src' / 'aq_turn'
    wheel = build_wheel(directory, 'aq-turn', {
        'aq_turn/__init__.py': (package / '__init__.py').read_bytes(),
        'aq_turn/nodes.py': (package / 'nodes.py').read_bytes()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory), entry


def binding_row(value, node, name):
    tables = dict(value['layout']['tables']['$m'])
    return next(row for row in tables['Bindings']
                if dict(row['$m'])['Node'] == node and dict(row['$m'])['Input'] == name)


def set_cell(row, key, new):
    for pair in row['$m']:
        if pair[0] == key:
            pair[1] = new


def test_compiled_plan_keeps_turn_bindings_and_runs_like_source(built):
    plan, entry = built
    compiled = load_built(plan)

    assert declaration_identity(compiled) == declaration_identity(load_session(entry))
    values = asyncio.run(compiled.workflow().arun({'message': 'a1'}, thread_id='chat'))

    assert values['finish']['saw_extra'] == 'extra-from-a1'
    assert values['draft']['saw_hint'] == 'seed' and values['draft']['saw_scratch'] == 'none'


@pytest.mark.parametrize(('change', 'message'), [
    (lambda value: set_cell(binding_row(value, 'finish', 'extra'), 'Field', 'nothing'), 'no node declares turn field'),
    (lambda value: set_cell(binding_row(value, 'finish', 'extra'), 'Field', '*'), 'TURN requires a named field'),
    (lambda value: set_cell(binding_row(value, 'draft', 'topic'), 'Input', 'attempt'), 'does not match the input schema'),
])
def test_plan_decoding_rejects_invalid_turn_bindings(built, change, message):
    plan, _ = built

    with pytest.raises(ArtifactRejected, match=message):
        load_built(plan, edited(plan.plan, change))
