"""Declared session state: load-time contracts, cross-turn reads and turn-scoped commits."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from api_agent.workflows import LayoutError, WorkflowError
from api_agent.workflows.identity import declaration_identity

from session_workflow_support import load_session, session_entry, session_workflow, turn


def test_session_round_trips_between_turns(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    first = turn(workflow, 'hello')
    second = turn(workflow, 'again')

    assert first['reply']['seen'] == {'history': 0, 'lines': [], 'last': ''}
    assert first['draft']['saw_topic'] == 'none'
    assert second['reply']['seen'] == {'history': 2, 'lines': ['hello#2'], 'last': 'hello#2'}
    # SESSION is the start-of-turn value; this turn's own write is read from the node.
    assert second['draft']['saw_topic'] == 'hello'
    assert second['reply']['topic'] == 'again'


def test_append_merge_accumulates_arrays_across_turns(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    for message in ('one', 'two', 'three'):
        turn(workflow, message)

    assert turn(workflow, 'four')['reply']['seen']['history'] == 6
    assert turn(workflow, 'five')['reply']['seen']['lines'] == ['one#2', 'two#2', 'three#2', 'four#2']


def test_replace_merge_keeps_only_the_latest_value_and_the_last_writer_wins(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    turn(workflow, 'one')
    turn(workflow, 'two')

    # last_text has two writers; the later node in execution order (draft) wins.
    assert turn(workflow, 'three')['draft']['saw_topic'] == 'two'
    assert turn(workflow, 'four')['reply']['seen']['last'] == 'three#2'


def test_a_writer_that_did_not_run_leaves_its_fields_unchanged(tmp_path):
    edges = ('## Session', '## Edges\n\n| From | To | When |\n| --- | --- | --- |\n'
             '| reply | END | reply["topic"] == "skip" |\n| reply | refine | otherwise |\n\n## Session')
    workflow = session_workflow(session_entry(tmp_path, edges))
    turn(workflow, 'hello')

    assert 'draft' not in turn(workflow, 'skip')
    seen = turn(workflow, 'next')['reply']['seen']

    assert seen['lines'] == ['hello#2']
    assert seen['last'] == 'skip (turns seen: 1)'


def test_sessions_are_isolated_by_thread(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    turn(workflow, 'a1', thread='a')
    turn(workflow, 'a2', thread='a')

    assert turn(workflow, 'b1', thread='b')['reply']['seen']['history'] == 0
    assert turn(workflow, 'a3', thread='a')['reply']['seen']['history'] == 4


def test_injected_checkpointer_carries_the_session_to_another_workflow(tmp_path):
    saver = InMemorySaver()
    entry = session_entry(tmp_path)
    turn(session_workflow(entry, checkpointer=saver), 'before restart')

    resumed = turn(session_workflow(entry, checkpointer=saver), 'after restart')

    assert resumed['reply']['seen']['history'] == 2
    assert turn(session_workflow(entry), 'fresh saver')['reply']['seen']['history'] == 0


def test_rejected_guard_pass_does_not_leak_into_the_session(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    turn(workflow, 'hello')

    # The guard rejects pass one, so only the accepted pass's line is persisted.
    assert turn(workflow, 'again')['reply']['seen']['lines'] == ['hello#2']
    assert turn(workflow, 'last')['reply']['seen']['lines'] == ['hello#2', 'again#2']


def test_failed_turn_persists_none_of_its_session_writes(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'hello')

    with pytest.raises(WorkflowError, match='draft failed'):
        turn(workflow, 'boom')

    seen = turn(workflow, 'after')['reply']['seen']
    # reply wrote history before the draft node failed; none of it was kept.
    assert seen == {'history': 2, 'lines': ['hello#2'], 'last': 'hello#2'}


def test_cancelled_turn_persists_none_of_its_session_writes(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'hello')

    async def hang():
        await asyncio.wait_for(workflow.arun({'message': 'hang'}, thread_id='chat'), 0.3)

    with pytest.raises(TimeoutError):
        asyncio.run(hang())

    assert turn(workflow, 'after')['reply']['seen']['history'] == 2


def test_session_read_uses_default_when_the_field_is_absent(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert turn(workflow, 'first')['draft']['saw_topic'] == 'none'


def test_required_session_read_fails_when_the_field_is_absent(tmp_path):
    entry = session_entry(tmp_path, ('| reply | history | SESSION | history | [] |',
                                     '| reply | history | SESSION | history | - |'))
    workflow = session_workflow(entry)

    with pytest.raises(WorkflowError, match='Missing required session field history for reply.history'):
        turn(workflow, 'first')


def test_session_workflow_requires_a_string_thread_id(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    for thread in (None, '', 7):
        with pytest.raises(ValueError, match='thread_id'):
            asyncio.run(workflow.arun({'message': 'hi'}, thread_id=thread))


NO_SESSION = (
    ('| reply | history | SESSION | history | [] |', '| reply | history | START | history | [] |'),
    ('| reply | lines | SESSION | draft_lines | [] |', '| reply | lines | START | lines | [] |'),
    ('| reply | last | SESSION | last_text | "" |', '| reply | last | START | last | "" |'),
    ('| draft | known_topic | SESSION | topic | "none" |', '| draft | known_topic | START | topic | "none" |'),
)


def without_session(tmp_path):
    entry = session_entry(tmp_path, *NO_SESSION)
    body = entry.read_text()
    entry.write_text(body[:body.index('## Session')])
    return entry


def test_workflow_without_session_still_rejects_thread_and_checkpointer(tmp_path):
    entry = without_session(tmp_path)
    workflow = session_workflow(entry)

    with pytest.raises(WorkflowError, match='Conversation checkpoints require a declared'):
        asyncio.run(workflow.arun({'message': 'hi'}, thread_id='chat'))
    with pytest.raises(LayoutError, match='Checkpoint state requires a declared'):
        session_workflow(entry, checkpointer=InMemorySaver())
    assert asyncio.run(workflow.arun({'message': 'hi'}))['reply']['seen']['history'] == 0


@pytest.mark.parametrize(('edit', 'message'), [
    (('| reply | history | SESSION | history | [] |', '| reply | history | SESSION | nope | [] |'),
     'undeclared session field'),
    (('| history | reply.messages | append |', '| history | ghost.messages | append |'),
     'unknown session source node'),
    (('| history | reply.messages | append |', '| history | reply.nothing | append |'),
     'unknown session source output field'),
    (('| history | reply.messages | append |', '| history | reply.messages | merge |'),
     'invalid session merge'),
    (('| history | reply.messages | append |', '| history | reply | append |'),
     'invalid session source'),
    (('| history | reply.messages | append |', '| history | .messages | append |'),
     'invalid session source'),
    (('| history | reply.messages | append |', '| his-tory | reply.messages | append |'),
     'invalid session field name'),
    (('| topic | reply.topic | replace |', '| history | reply.messages | append |'),
     'duplicate session source'),
    (('| last_text | draft.text | replace |', '| last_text | draft.lines | append |'),
     'conflicting merges'),
    (('| topic | reply.topic | replace |', '| topic | reply.topic | append |'),
     'append session source must be an array'),
    (('| draft | known_topic | SESSION | topic | "none" |', '| draft | known_topic | SESSION | history | "none" |'),
     'does not match the input schema'),
    (('| reply | history | SESSION | history | [] |', '| reply | history | SESSION | * | [] |'),
     'undeclared session field'),
    (('| draft | known_topic | SESSION | topic | "none" |', '| draft | known_topic | SESSION | topic | 3 |'),
     'is not of type'),
])
def test_load_rejects_invalid_session_contracts(tmp_path, edit, message):
    entry = session_entry(tmp_path, edit)

    with pytest.raises(LayoutError, match=message):
        load_session(entry)


def test_load_rejects_session_binding_without_a_session_table(tmp_path):
    entry = without_session(tmp_path)
    entry.write_text(entry.read_text().replace('| draft | known_topic | START | topic | "none" |',
                                               '| draft | known_topic | SESSION | topic | "none" |'))

    with pytest.raises(LayoutError, match='undeclared session field'):
        load_session(entry)


def test_load_rejects_an_empty_session_table(tmp_path):
    entry = without_session(tmp_path)
    entry.write_text(entry.read_text() + '## Session\n\n| Field | Source | Merge |\n| --- | --- | --- |\n')

    with pytest.raises(LayoutError, match='at least one row'):
        load_session(entry)


def test_load_rejects_wrong_session_columns_and_child_scope(tmp_path):
    columns = session_entry(tmp_path / 'columns', ('| Field | Source | Merge |', '| Field | Source | Mode |'))
    with pytest.raises(LayoutError, match='unknown table columns'):
        load_session(columns)
    child = session_entry(tmp_path / 'child')
    reply = child.parent / 'reply.md.html'
    reply.write_text(reply.read_text() + '\n## Session\n\n| Field | Source | Merge |\n| --- | --- | --- |\n'
                     '| a | reply.topic | replace |\n')
    with pytest.raises(LayoutError, match='unknown declaration section'):
        load_session(child)


def test_session_table_changes_workflow_identity(tmp_path):
    base = declaration_identity(load_session(session_entry(tmp_path / 'base')))

    assert declaration_identity(load_session(session_entry(tmp_path / 'same'))) == base
    for index, edit in enumerate((('| topic | reply.topic | replace |', '| topic | draft.text | replace |'),
                                  ('| last_text | reply.reply | replace |\n', ''))):
        changed = declaration_identity(load_session(session_entry(tmp_path / f'edit{index}', edit)))
        assert changed != base
