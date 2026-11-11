"""``Workflow.session`` reads the committed session of one thread without running a turn."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeCause, OutcomeState
from api_agent.workflows import WorkflowError

from loop_support import graph, build
from session_workflow_support import session_entry, session_workflow, turn

OTHER_GRAPH = ('| session_chat |', '| session_other |')


def read(workflow, thread='chat'):
    return asyncio.run(workflow.session(thread))


def test_a_thread_without_a_committed_turn_has_an_empty_session(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert read(workflow) == {}


def test_the_committed_session_is_returned_after_a_turn(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')

    committed = read(workflow)

    assert committed['topic'] == 'one'
    assert [item['role'] for item in committed['history']] == ['user', 'assistant']
    assert committed['draft_lines'] == ['one#2'] and committed['last_text'] == 'one#2'


def test_the_returned_session_is_a_deep_copy(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')
    expected = read(workflow)
    tampered = read(workflow)

    tampered['history'].append({'role': 'user', 'text': 'tampered'})
    tampered['topic'] = 'tampered'
    tampered['extra'] = True

    assert read(workflow) == expected
    assert turn(workflow, 'two')['reply']['seen']['history'] == 2


def test_an_uncommitted_turn_does_not_change_the_read_session(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')
    outcome = {'execution_outcome': lambda: ExecutionOutcome(OutcomeState.PARTIAL, OutcomeCause.TOOL)}

    asyncio.run(workflow.arun({'message': 'two'}, thread_id='chat', services=outcome))

    assert read(workflow)['topic'] == 'one'


def test_the_read_follows_the_graph_namespace_of_a_shared_checkpointer(tmp_path):
    saver = InMemorySaver()
    first = session_workflow(session_entry(tmp_path / 'first'), checkpointer=saver)
    other = session_workflow(session_entry(tmp_path / 'other', OTHER_GRAPH), checkpointer=saver)
    turn(first, 'one', thread='shared')

    assert read(first, 'shared')['topic'] == 'one'
    assert read(other, 'shared') == {}
    assert read(first, 'other') == {}


def test_fields_the_session_table_no_longer_declares_are_not_returned(tmp_path):
    saver = InMemorySaver()
    declared = session_entry(tmp_path / 'before', ('| topic | reply.topic | replace |',
                                                   '| topic | reply.topic | replace |\n| extra | reply.reply | replace |'))
    turn(session_workflow(declared, checkpointer=saver), 'one')
    assert 'extra' in read(session_workflow(declared, checkpointer=saver))

    narrowed = session_workflow(session_entry(tmp_path / 'after'), checkpointer=saver)

    assert 'extra' not in read(narrowed) and read(narrowed)['topic'] == 'one'


@pytest.mark.parametrize('thread', ['', None, 3])
def test_a_thread_id_is_required(tmp_path, thread):
    workflow = session_workflow(session_entry(tmp_path))

    with pytest.raises(ValueError, match='thread_id is required to read a session'):
        asyncio.run(workflow.session(thread))


def test_a_workflow_without_a_session_has_none_to_read(tmp_path):
    workflow = build(graph(tmp_path, [('node', 'draft')]))[0]

    with pytest.raises(WorkflowError, match='declares no Session'):
        asyncio.run(workflow.session('chat'))
