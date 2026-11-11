"""Sessions are filed per graph, forget undeclared fields, and belong to one turn at a time."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from session_workflow_support import session_entry, session_workflow, turn

OTHER_GRAPH = ('| session_chat |', '| session_other |')


def stored(saver):
    return next(iter(saver.list(None))).checkpoint['channel_values']['session']


def test_graphs_sharing_a_checkpointer_and_thread_keep_separate_sessions(tmp_path):
    saver = InMemorySaver()
    first = session_workflow(session_entry(tmp_path / 'first'), checkpointer=saver)
    other = session_workflow(session_entry(tmp_path / 'other', OTHER_GRAPH), checkpointer=saver)

    turn(first, 'one', thread='shared')
    turn(first, 'two', thread='shared')

    assert turn(other, 'fresh', thread='shared')['reply']['seen']['history'] == 0
    assert turn(first, 'three', thread='shared')['reply']['seen']['history'] == 4


def test_a_thread_name_that_looks_like_another_graphs_prefix_stays_separate(tmp_path):
    saver = InMemorySaver()
    first = session_workflow(session_entry(tmp_path / 'first'), checkpointer=saver)
    other = session_workflow(session_entry(tmp_path / 'other', OTHER_GRAPH), checkpointer=saver)

    turn(other, 'one', thread='x')

    assert turn(first, 'fresh', thread='session_other:x')['reply']['seen']['history'] == 0


def test_fields_no_longer_declared_are_dropped_when_the_session_loads(tmp_path):
    saver = InMemorySaver()
    turn(session_workflow(session_entry(tmp_path / 'before'), checkpointer=saver), 'one')
    assert 'topic' in stored(saver)
    without_topic = session_entry(
        tmp_path / 'after',
        ('| topic | reply.topic | replace |\n', ''),
        ('| draft | known_topic | SESSION | topic | "none" |', '| draft | known_topic | START | message | "none" |'))

    turn(session_workflow(without_topic, checkpointer=saver), 'two')

    assert set(stored(saver)) == {'history', 'draft_lines', 'last_text'}


def test_a_second_turn_on_a_running_thread_is_refused_and_the_first_is_unharmed(tmp_path):
    saver = InMemorySaver()
    workflow = session_workflow(session_entry(tmp_path), checkpointer=saver)
    turn(workflow, 'one')

    async def overlap():
        running = asyncio.create_task(workflow.arun({'message': 'hang'}, thread_id='chat'))
        await asyncio.sleep(0.3)
        with pytest.raises(RuntimeError, match='active turn'):
            await workflow.arun({'message': 'two'}, thread_id='chat')
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    asyncio.run(overlap())

    assert turn(workflow, 'three')['reply']['seen']['history'] == 2
