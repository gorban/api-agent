"""Transactional, bounded intermediate LLM evidence."""
import concurrent.futures
import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from api_agent.testing import inference_content as framing


def test_evidence_connections_close_on_success_and_failure(tmp_path, monkeypatch):
    connections = []
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(framing.sqlite3, 'connect', tracked_connect)
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    framing._publish_sidecar(path, 'test', {'phase': 'request', 'content': {}}, str(uuid4()))
    assert framing.read_evidence(path, 0)
    with pytest.raises(sqlite3.OperationalError):
        framing.read_evidence(str(tmp_path / 'missing.sqlite'), 0)
    assert len(connections) == 4
    assert all(connection.closed for connection in connections)


def test_parallel_writers_share_one_bounded_budget(tmp_path, monkeypatch):
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', path)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test (call)')
    monkeypatch.setattr(framing, 'MAX_TEST_BYTES', 700_000)
    def publish(_):
        framing.publish_content(({'phase': 'request', 'content': {'text': 'a' * 180_000}},), str(uuid4()))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(publish, range(12)))
    rows = []
    while batch := framing.read_evidence(path, rows[-1][0] if rows else 0):
        rows.extend(batch)
    assert len(rows) == 4
    assert rows[-1][2]['reason'] == 'truncated'
    assert all(row[2]['content']['text'] == 'a' * 180_000 for row in rows[:-1])
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute('SELECT max(length(data)) FROM part').fetchone()[0] <= framing.CHUNK_BYTES
        assert db.execute('SELECT sum(length(data)) FROM part').fetchone()[0] < 700_500


def test_chunk_corruption_is_detected(tmp_path):
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    framing._publish_sidecar(path, 'test', {'phase': 'response', 'content': {'text': 'x' * 210_000}}, str(uuid4()))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE part SET data=? WHERE number=1', (b'bad',))
    with pytest.raises(ValueError, match='framing'):
        framing.read_evidence(path, 0)


def test_storage_failure_is_signalled_without_raising(tmp_path, monkeypatch):
    path = str(tmp_path / 'missing.sqlite')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', path)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test (call)')
    with framing.EvidenceFailureChannel(['test']) as channel:
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        framing.publish_content(({'phase': 'request', 'content': {}},), str(uuid4()))
        assert channel.reported() == {'test'}
    assert (tmp_path / framing.failure_marker('missing.sqlite', 'test')).is_file()


def _interrupted_writer(path):
    import os
    framing._start_marker(path, 'test')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE budget SET bytes=10 WHERE node='test'")
        os._exit(17)


def test_worker_death_leaves_pending_marker_and_rolls_back(tmp_path):
    import multiprocessing
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    worker = multiprocessing.get_context('spawn').Process(target=_interrupted_writer, args=(path,))
    worker.start()
    worker.join(10)
    assert worker.exitcode == 17
    assert framing.has_pending(path, 'test')
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute('SELECT bytes FROM budget').fetchone()[0] == 0


def test_transient_exclusive_lock_can_be_retried(tmp_path):
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            framing.read_evidence(path, 0)
    assert framing.read_evidence(path, 0) == []


def test_truncation_marker_fits_inside_test_budget(tmp_path, monkeypatch):
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    monkeypatch.setattr(framing, 'MAX_TEST_BYTES', 4096)
    framing._publish_sidecar(path, 'test', {'phase': 'request', 'content': {'text': 'x' * 3500}}, str(uuid4()))
    framing._publish_sidecar(path, 'test', {'phase': 'response', 'content': {'text': 'x' * 600}}, str(uuid4()))
    with closing(sqlite3.connect(path)) as db, db:
        size, stopped = db.execute('SELECT bytes, stopped FROM budget').fetchone()
    assert stopped == 1
    assert size <= 4096


def test_preallocated_status_reports_marker_creation_failure(tmp_path, monkeypatch):
    import os
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', path)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test (call)')
    original_open = os.open
    def deny_creation(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError('directory full')
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', deny_creation)
    with framing.EvidenceFailureChannel(['test']) as channel:
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        framing.publish_content(({'phase': 'request', 'content': {}},), str(uuid4()))
        assert channel.reported() == {'test'}
    assert framing.failure_reported(path, 'test')
    assert framing.read_evidence(path, 0) == []


def test_runner_reports_unread_committed_evidence_when_final_lock_persists(tmp_path, monkeypatch):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo

    root = fixture_repo(tmp_path / 'base')
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    initialize = framing.initialize_evidence
    held_locks = []
    def seed_and_lock(path, nodes):
        initialize(path, nodes)
        framing._publish_sidecar(path, selected[0].node_id,
            {'phase': 'request', 'content': {'text': 'committed but unread'}}, str(uuid4()))
        lock = sqlite3.connect(path)
        lock.execute('BEGIN EXCLUSIVE')
        held_locks.append(lock)
    monkeypatch.setattr(framing, 'initialize_evidence', seed_and_lock)
    events = []
    try:
        result = run_integration_tests(ctx, selected, accepted=True, on_event=events.append)
    finally:
        for lock in held_locks:
            lock.close()
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert [(event.identity, event.text) for event in events
            if event.kind == 'inference_evidence_incomplete'] == [(selected[0], 'unavailable')]
    assert not any(event.kind == 'inference_content' for event in events)


def test_short_status_write_is_visible_as_unavailable(tmp_path, monkeypatch):
    import os
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    original_open = os.open
    def deny_creation(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError('directory full')
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', deny_creation)
    monkeypatch.setattr(os, 'write', lambda *_: 0)
    framing._signal_failure(path, 'test')
    assert framing.failure_reported(path, 'test')
    assert not (tmp_path / framing.status_path('evidence.sqlite', 'test')).exists()


def test_unacknowledged_failure_raises_fixed_error(tmp_path, monkeypatch):
    import os
    path = str(tmp_path / 'evidence.sqlite')
    framing.initialize_evidence(path, ['test'])
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', path)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test (call)')
    monkeypatch.delenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', raising=False)
    original_open = os.open
    def deny_creation(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError('full')
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', deny_creation)
    with pytest.raises(framing.EvidencePublicationError, match='not acknowledged'):
        framing.publish_content(({'phase': 'request', 'content': {}},), str(uuid4()))


def test_failure_channel_rejects_bad_capability(monkeypatch):
    with framing.EvidenceFailureChannel(['test']) as channel:
        descriptor = json.loads(channel.descriptor)
        descriptor['token'] = '0' * 64
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', json.dumps(descriptor))
        with pytest.raises(framing.EvidencePublicationError):
            framing.notify_failure('test')
        assert channel.reported() == set()


def test_slow_unauthorized_connection_does_not_block_failure_notification(monkeypatch):
    import socket
    import threading

    with framing.EvidenceFailureChannel(['test']) as channel:
        descriptor = json.loads(channel.descriptor)
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        stop = threading.Event()
        with socket.create_connection(('127.0.0.1', descriptor['port'])) as slow:
            slow.sendall(b'x')
            def trickle():
                while not stop.wait(.05):
                    try:
                        slow.sendall(b'x')
                    except OSError:
                        break
            writer = threading.Thread(target=trickle)
            writer.start()
            try:
                framing.notify_failure('test')
                assert channel.reported() == {'test'}
            finally:
                stop.set()
                writer.join(timeout=2)


@pytest.mark.parametrize('acknowledged', [True, False])
def test_real_runner_reports_failure_or_fails_unacknowledged_worker(tmp_path, acknowledged):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo

    source = '''
import os
from uuid import uuid4
from api_agent.testing import inference_content as framing

def test_publish(monkeypatch):
    def failed(*args):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(framing, '_publish_sidecar', failed)
    if not ACKNOWLEDGED:
        monkeypatch.delenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL')
    framing.publish_content(({'phase': 'request', 'content': {}},), str(uuid4()))
'''.replace('ACKNOWLEDGED', repr(acknowledged))
    root = fixture_repo(tmp_path / 'base', source)
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    events = []
    result = run_integration_tests(ctx, selected, accepted=True, on_event=events.append)
    assert result.tests[0].status == ('passed' if acknowledged else 'failed')
    assert any(event.kind == 'inference_evidence_incomplete' and event.identity == selected[0]
               and event.text == 'unavailable' for event in events)


@pytest.mark.parametrize('jobs', [0, 2])
def test_caught_unacknowledged_failure_still_fails_test(tmp_path, jobs):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo

    root = fixture_repo(tmp_path / 'base', '''
from uuid import uuid4
from api_agent.testing import inference_content as framing

def test_publish(monkeypatch):
    def failed(*args):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(framing, '_publish_sidecar', failed)
    monkeypatch.delenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL')
    try:
        framing.publish_content(({'phase': 'request', 'content': {}},), str(uuid4()))
    except framing.EvidencePublicationError:
        pass
''')
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=jobs)
    assert result.exit_code == 1
    assert result.tests[0].status == 'error'
    assert 'not acknowledged' in result.tests[0].traceback
