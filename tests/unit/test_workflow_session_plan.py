"""A compiled plan carries the Session table and runs sessions like its source."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built, v2_built, v3_built  # noqa: F401
from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.identity import declaration_identity

from session_workflow_support import FIXTURE, load_session, session_entry


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    directory = tmp_path_factory.mktemp('session_plan')
    root = directory / 'tree'
    entry = session_entry(root)
    package = FIXTURE / 'src' / 'aq_session'
    wheel = build_wheel(directory, 'aq-session', {
        'aq_session/__init__.py': (package / '__init__.py').read_bytes(),
        'aq_session/nodes.py': (package / 'nodes.py').read_bytes()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory), entry


def run(workflow, message, thread='chat'):
    return asyncio.run(workflow.arun({'message': message}, thread_id=thread))


def table_rows(value, name):
    tables = dict(value['layout']['tables']['$m'])
    return tables[name]


def set_cell(row, key, new):
    for pair in row['$m']:
        if pair[0] == key:
            pair[1] = new
            return
    row['$m'].append([key, new])


def test_compiled_plan_round_trips_the_session_table(built):
    plan, entry = built
    compiled = load_built(plan)
    source = load_session(entry)

    assert [(row['Field'], row['Source'], row['Merge']) for row in compiled.layout.tables['Session']] == [
        (row['Field'], row['Source'], row['Merge']) for row in source.layout.tables['Session']]
    assert declaration_identity(compiled) == declaration_identity(source)


def test_compiled_workflow_continues_a_session_with_an_injected_checkpointer(built):
    plan, _ = built
    saver = InMemorySaver()
    first = load_built(plan).workflow(checkpointer=saver)
    run(first, 'hello')

    resumed = run(load_built(plan).workflow(checkpointer=saver), 'again')

    assert resumed['reply']['seen'] == {'history': 2, 'lines': ['hello#2'], 'last': 'hello#2'}
    assert resumed['draft']['saw_topic'] == 'hello'


@pytest.mark.parametrize(('change', 'message'), [
    (lambda value: set_cell(table_rows(value, 'Session')[0], 'Source', 'ghost.messages'),
     'unknown session source node'),
    (lambda value: set_cell(table_rows(value, 'Session')[0], 'Merge', 'sum'), 'invalid session merge'),
    (lambda value: set_cell(table_rows(value, 'Session')[0], 'Field', 'bad-name'), 'invalid session field name'),
    (lambda value: table_rows(value, 'Session').pop(1), 'undeclared session field'),
    (lambda value: table_rows(value, 'Session')[0]['$m'].append(['Extra', 'x']), 'invalid session row'),
    (lambda value: table_rows(value, 'Session')[0]['$m'].pop(), 'invalid session row'),
    (lambda value: set_cell(table_rows(value, 'Session')[0], 'line', 'one'), 'invalid session row'),
    (lambda value: set_cell(table_rows(value, 'Session')[0], 'Merge', 3), 'invalid session row'),
    (lambda value: value['layout']['tables']['$m'].remove(
        ['Session', table_rows(value, 'Session')]), 'undeclared session field'),
])
def test_plan_decoding_rejects_invalid_session_tables(built, change, message):
    plan, _ = built

    with pytest.raises(ArtifactRejected, match=message):
        load_built(plan, edited(plan.plan, change))


def test_plan_without_session_stays_loadable_and_session_is_rejected_for_typed_plans(v3_built, v2_built):
    assert 'Session' not in load_built(v2_built).layout.tables

    def add(value):
        value['layout']['tables']['$m'].append(['Session', []])
    with pytest.raises(ArtifactRejected, match='invalid layout tables'):
        load_built(v3_built, edited(v3_built.plan, add))
