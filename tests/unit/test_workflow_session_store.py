"""A ``WorkflowSessionStore`` replaces the checkpointer as the place a turn's session is loaded and saved."""
import asyncio
from copy import deepcopy

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from api_agent.workflows import SessionConflict, SessionSnapshot, WorkflowSessionStore
from api_agent.workflows.session_store import CheckpointerSessionStore

from session_workflow_support import session_entry, session_workflow, turn
from test_workflow_session_outcome import recorded


class RecordingStore:
    """An in-memory store whose versions are counters, recording every call."""

    def __init__(self):
        self.values, self.versions, self.calls = {}, {}, []

    async def load(self, thread_id):
        self.calls.append(('load', thread_id))
        if thread_id not in self.values:
            return SessionSnapshot({}, None, None)
        return SessionSnapshot(deepcopy(self.values[thread_id]), self.versions[thread_id], 1.0)

    async def save(self, thread_id, value, *, expected_version):
        self.calls.append(('save', thread_id, expected_version))
        if expected_version != self.versions.get(thread_id):
            raise SessionConflict(thread_id)
        self.values[thread_id] = deepcopy(value)
        self.versions[thread_id] = str(int(self.versions.get(thread_id) or 0) + 1)
        return self.versions[thread_id]


def test_a_custom_store_receives_the_raw_thread_id_and_the_version_it_loaded(tmp_path):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)

    turn(workflow, 'one')
    turn(workflow, 'two')

    assert store.calls == [('load', 'chat'), ('save', 'chat', None),
                           ('load', 'chat'), ('save', 'chat', '1')]
    assert store.values['chat']['topic'] == 'two'


def test_a_second_workflow_over_the_same_store_continues_the_session(tmp_path):
    store = RecordingStore()
    turn(session_workflow(session_entry(tmp_path / 'first'), session_store=store), 'one')
    fresh = session_workflow(session_entry(tmp_path / 'second'), session_store=store)

    assert turn(fresh, 'two')['reply']['seen']['history'] == 2


def test_the_session_read_goes_through_the_store(tmp_path):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)
    turn(workflow, 'one')

    assert asyncio.run(workflow.session('chat'))['topic'] == 'one'
    assert store.calls[-1] == ('load', 'chat')


def test_a_version_conflict_fails_the_turn_and_saves_nothing(tmp_path):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)
    turn(workflow, 'one')
    original = store.save

    async def raced(thread_id, value, *, expected_version):
        store.versions[thread_id] = 'elsewhere'
        return await original(thread_id, value, expected_version=expected_version)
    store.save = raced

    with pytest.raises(SessionConflict):
        turn(workflow, 'two')

    assert store.values['chat']['topic'] == 'one'


@pytest.mark.parametrize('target', ['api_agent.workflows.journal.WorkflowJournal.finalize',
                                    'api_agent.workflows.evidence.publish'])
def test_restore_writes_the_previous_session_against_the_version_its_save_returned(
        monkeypatch, tmp_path, target):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)
    turn(workflow, 'one')
    recorded(monkeypatch, tmp_path)
    monkeypatch.setattr(target, lambda *args, **kwargs: (_ for _ in ()).throw(OSError('down')))
    store.calls.clear()

    with pytest.raises(OSError, match='down'):
        turn(workflow, 'two')
    monkeypatch.undo()

    assert store.calls == [('load', 'chat'), ('save', 'chat', '1'), ('save', 'chat', '2')]
    assert store.values['chat']['topic'] == 'one'


def test_a_failed_turn_never_touches_the_store_after_loading(tmp_path):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)
    turn(workflow, 'one')
    store.calls.clear()

    with pytest.raises(Exception, match='draft failed'):
        turn(workflow, 'boom')

    assert store.calls == [('load', 'chat')]


def test_the_default_adapter_keeps_the_graph_prefix_on_the_checkpointer_thread(tmp_path):
    saver = InMemorySaver()
    workflow = session_workflow(session_entry(tmp_path), checkpointer=saver)
    turn(workflow, 'one')

    threads = {item.config['configurable']['thread_id'] for item in saver.list(None)}

    assert threads == {'session_chat:chat'}


def test_the_default_adapter_is_a_session_store(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert isinstance(workflow._session_store, CheckpointerSessionStore)
    assert isinstance(workflow._session_store, WorkflowSessionStore)


def snapshot(workflow, thread='chat'):
    return asyncio.run(workflow._session_store.load(thread))


def test_the_default_adapter_reports_an_empty_snapshot_before_any_commit(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert snapshot(workflow) == SessionSnapshot({}, None, None)


def test_the_default_adapter_versions_each_commit_and_stamps_its_time(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')
    first = snapshot(workflow)
    turn(workflow, 'two')
    second = snapshot(workflow)

    assert first.version and second.version and first.version != second.version
    assert first.value['topic'] == 'one' and second.value['topic'] == 'two'
    assert isinstance(first.saved_at, float) and second.saved_at >= first.saved_at


def test_the_default_adapter_rejects_a_stale_expected_version(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')
    stale = snapshot(workflow).version
    turn(workflow, 'two')

    with pytest.raises(SessionConflict):
        asyncio.run(workflow._session_store.save('chat', {'topic': 'x'}, expected_version=stale))
    with pytest.raises(SessionConflict):
        asyncio.run(workflow._session_store.save('chat', {'topic': 'x'}, expected_version=None))
    assert snapshot(workflow).value['topic'] == 'two'


def test_the_default_adapter_save_returns_the_new_version(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    store = workflow._session_store

    version = asyncio.run(store.save('chat', {'topic': 'x'}, expected_version=None))

    assert snapshot(workflow).version == version


class YieldingSaver(InMemorySaver):
    """A saver that suspends on every read, as a networked checkpointer does."""

    async def aget_tuple(self, config):
        await asyncio.sleep(0)
        return await super().aget_tuple(config)


def test_the_default_adapter_lets_only_one_of_two_concurrent_saves_at_one_version_win(tmp_path):
    workflow = session_workflow(session_entry(tmp_path), checkpointer=YieldingSaver())
    store = workflow._session_store

    async def race():
        return await asyncio.gather(
            store.save('chat', {'topic': 'a'}, expected_version=None),
            store.save('chat', {'topic': 'b'}, expected_version=None),
            return_exceptions=True)
    results = asyncio.run(race())

    assert sum(isinstance(result, SessionConflict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, str))
    assert snapshot(workflow).version == winner


def failing_finalize(monkeypatch, tmp_path):
    recorded(monkeypatch, tmp_path)
    monkeypatch.setattr('api_agent.workflows.journal.WorkflowJournal.finalize',
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('evidence store down')))


def break_restore(store, error):
    """Let the turn's own save through and fail the restore write that follows it."""
    original = store.save

    async def save(thread_id, value, *, expected_version):
        if store.restoring:
            raise error
        result = await original(thread_id, value, expected_version=expected_version)
        store.restoring = True
        return result
    store.restoring = False
    store.save = save


@pytest.mark.parametrize('failure', [SessionConflict('chat'), OSError('session store outage')])
def test_a_restore_failure_never_replaces_the_original_error(monkeypatch, tmp_path, failure):
    store = RecordingStore()
    workflow = session_workflow(session_entry(tmp_path), session_store=store)
    turn(workflow, 'one')
    failing_finalize(monkeypatch, tmp_path)
    store.restoring = False
    break_restore(store, failure)

    with pytest.raises(OSError, match='evidence store down') as raised:
        turn(workflow, 'two')

    assert any('restore' in note and type(failure).__name__ in note for note in raised.value.__notes__)
    assert store.values['chat']['topic'] == 'two'


def test_a_restore_failure_alone_is_raised_when_no_error_is_in_flight():
    from api_agent.workflows.execution import _restore_session

    class Broken:
        async def restore(self):
            raise OSError('session store outage')

    with pytest.raises(OSError, match='session store outage'):
        asyncio.run(_restore_session(Broken()))


def test_two_workflows_sharing_a_checkpointer_conflict_instead_of_last_writer_wins(tmp_path):
    saver = InMemorySaver()
    first = session_workflow(session_entry(tmp_path / 'first'), checkpointer=saver)
    second = session_workflow(session_entry(tmp_path / 'second'), checkpointer=saver)
    turn(first, 'one')
    commit = first._session_commit('chat')
    asyncio.run(commit.load())
    turn(second, 'elsewhere')
    commit.merged = {'topic': 'mine'}

    with pytest.raises(SessionConflict):
        asyncio.run(commit.save())

    assert asyncio.run(second.session('chat'))['topic'] == 'elsewhere'
