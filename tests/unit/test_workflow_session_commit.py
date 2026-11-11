"""A declared ``## Session Commit`` table lists the outcomes that save a turn's session."""
import asyncio
import re

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeCause, OutcomeState
from api_agent.workflows import LayoutError
from api_agent.workflows import session as session_contract
from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.identity import declaration_identity

from session_workflow_support import FIXTURE, load_session, session_entry, session_workflow, turn

LAST = '| last_text | draft.text | replace |'
COMMIT = '\n\n## Session Commit\n\n| Outcome |\n| --- |\n{rows}\n'


def commit(*outcomes):
    return (LAST, LAST + COMMIT.format(rows='\n'.join(f'| {name} |' for name in outcomes)))


def outcome(state, cause=None):
    return {'execution_outcome': lambda: ExecutionOutcome(state, cause)}


def second_turn_history(workflow, state, cause=None):
    asyncio.run(workflow.arun({'message': 'one'}, thread_id='chat', services=outcome(state, cause)))
    return turn(workflow, 'two')['reply']['seen']['history']


def test_a_partial_turn_is_not_committed_unless_the_workflow_declares_it(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert second_turn_history(workflow, OutcomeState.PARTIAL, OutcomeCause.TOOL) == 0


def test_a_partial_turn_is_committed_when_declared(tmp_path):
    workflow = session_workflow(session_entry(tmp_path, commit('passed', 'partial')))

    assert second_turn_history(workflow, OutcomeState.PARTIAL, OutcomeCause.TOOL) == 2


def test_a_passed_only_table_keeps_the_default_policy(tmp_path):
    workflow = session_workflow(session_entry(tmp_path / 'default', commit('passed')))
    other = session_workflow(session_entry(tmp_path / 'other', commit('partial', 'passed')))

    assert second_turn_history(workflow, OutcomeState.PARTIAL, OutcomeCause.TOOL) == 0
    assert second_turn_history(other, OutcomeState.SUCCESS) == 2


@pytest.mark.parametrize(('state', 'cause'), [
    (OutcomeState.FAILURE, OutcomeCause.EXECUTION),
    (OutcomeState.CANCELLED, None),
    (OutcomeState.UNKNOWN, None),
])
def test_failed_cancelled_and_unknown_turns_never_commit(tmp_path, state, cause):
    workflow = session_workflow(session_entry(tmp_path, commit('passed', 'partial')))

    assert second_turn_history(workflow, state, cause) == 0


@pytest.mark.parametrize(('rows', 'message'), [
    (('failure',), 'session commit outcome must be passed or partial'),
    (('passed', 'cancelled'), 'session commit outcome must be passed or partial'),
    (('passed', 'unknown'), 'session commit outcome must be passed or partial'),
    (('passed', 'timeout'), 'session commit outcome must be passed or partial'),
    (('passed', 'Partial'), 'session commit outcome must be passed or partial'),
    (('passed', 'partial', 'partial'), 'duplicate session commit outcome'),
    (('partial',), 'session commit must list passed'),
    ((), 'Session Commit requires at least one row'),
])
def test_load_rejects_invalid_commit_rows_at_the_row(tmp_path, rows, message):
    entry = session_entry(tmp_path, commit(*rows))
    lines = entry.read_text().splitlines()
    row_lines = [number for number, text in enumerate(lines, 1) if text in {f'| {name} |' for name in rows}]

    with pytest.raises(LayoutError, match=message) as error:
        load_session(entry)

    located = re.search(r'chat\.md\.html:(\d+): ', str(error.value))
    assert located is not None
    assert not rows or int(located.group(1)) in row_lines


def test_a_commit_table_requires_a_session_table(tmp_path):
    entry = session_entry(tmp_path)
    text = entry.read_text()
    entry.write_text(text[:text.index('## Session')] + COMMIT.format(rows='| passed |').lstrip())

    with pytest.raises(LayoutError, match='Session Commit requires a Session table'):
        load_session(entry)


def test_the_commit_policy_changes_the_workflow_identity(tmp_path):
    plain = declaration_identity(load_session(session_entry(tmp_path / 'plain')))

    assert declaration_identity(load_session(session_entry(tmp_path / 'same'))) == plain
    declared = declaration_identity(load_session(session_entry(tmp_path / 'declared', commit('passed', 'partial'))))
    assert declared != plain
    assert declaration_identity(load_session(session_entry(tmp_path / 'again', commit('passed', 'partial')))) == declared


def test_the_commit_outcomes_are_listed_in_declaration_order():
    tables = {'Session Commit': [{'Outcome': 'passed', 'line': 1}, {'Outcome': 'partial', 'line': 2}]}

    assert session_contract.commit_outcomes(tables) == ('passed', 'partial')
    assert session_contract.commit_outcomes({}) == ('passed',)


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    directory = tmp_path_factory.mktemp('session_commit_plan')
    root = directory / 'tree'
    entry = session_entry(root, commit('passed', 'partial'))
    package = FIXTURE / 'src' / 'aq_session'
    wheel = build_wheel(directory, 'aq-session', {
        'aq_session/__init__.py': (package / '__init__.py').read_bytes(),
        'aq_session/nodes.py': (package / 'nodes.py').read_bytes()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory), entry


def rows(value):
    return dict(value['layout']['tables']['$m'])['Session Commit']


def test_compiled_plan_round_trips_the_commit_table_and_commits_partial_turns(built):
    plan, entry = built
    compiled = load_built(plan)

    assert [row['Outcome'] for row in compiled.layout.tables['Session Commit']] == ['passed', 'partial']
    assert declaration_identity(compiled) == declaration_identity(load_session(entry))
    workflow = compiled.workflow(checkpointer=InMemorySaver())
    assert second_turn_history(workflow, OutcomeState.PARTIAL, OutcomeCause.TOOL) == 2


@pytest.mark.parametrize(('change', 'message'), [
    (lambda value: rows(value)[0]['$m'][0].__setitem__(1, 'failure'), 'session commit outcome'),
    (lambda value: rows(value).pop(0), 'session commit must list passed'),
    (lambda value: rows(value).append(rows(value)[0]), 'duplicate session commit outcome'),
    (lambda value: rows(value)[0]['$m'].append(['Extra', 'x']), 'invalid session commit row'),
    (lambda value: rows(value)[0]['$m'][0].__setitem__(1, 3), 'invalid session commit row'),
    (lambda value: rows(value).clear(), 'Session Commit requires at least one row'),
    (lambda value: dict(value['layout']['tables']['$m'])['Session'].clear(), 'Session requires at least one row'),
])
def test_plan_decoding_rejects_invalid_commit_tables(built, change, message):
    plan, _ = built

    with pytest.raises(ArtifactRejected, match=message):
        load_built(plan, edited(plan.plan, change))


def test_a_plan_without_a_commit_table_commits_only_passed_turns(tmp_path):
    entry = session_entry(tmp_path / 'tree')
    package = FIXTURE / 'src' / 'aq_session'
    wheel = build_wheel(tmp_path, 'aq-session', {
        'aq_session/__init__.py': (package / '__init__.py').read_bytes(),
        'aq_session/nodes.py': (package / 'nodes.py').read_bytes()})
    plan = compile_plan(entry, base_root=tmp_path / 'tree', pack_root=tmp_path / 'tree', wheels=[wheel])
    install(wheel, tmp_path / 'site')
    built = Built(plan, plan_digest(plan), tmp_path / 'site', wheel, tmp_path)

    compiled = load_built(built)

    assert 'Session Commit' not in compiled.layout.tables
    assert second_turn_history(compiled.workflow(checkpointer=InMemorySaver()),
                               OutcomeState.PARTIAL, OutcomeCause.TOOL) == 0
