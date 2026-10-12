"""Focused regression tests for indexed historical windows and reconciliation."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import sqlite3
from uuid import uuid4

import pytest

from api_agent.telemetry.history import HistoryCache
from api_agent.testing.storage import RunStorage, StorageError


def evidence(finished):
    run_id = str(uuid4())
    artifact = {'version': 1, 'estimator_version': 1, 'run_id': run_id,
                'rate_card': None, 'quota': {'before': None, 'after': None,
                'pct_point_delta': None, 'quota_unavailable_reason': 'missing_snapshot',
                'payment_policy': None, 'payment_month': None}, 'tests': []}
    payload = json.dumps(artifact).encode()
    manifest = {'schema': 1, 'repo': 'repo', 'mode': 'integration', 'status': 'completed',
                'run_id': run_id, 'started_at': (finished - timedelta(hours=1)).isoformat(),
                'finished_at': finished.isoformat(),
                'files': {'accounting.json': sha256(payload).hexdigest()}}
    return manifest, payload


def test_window_filters_before_retrieval_without_cap_and_reopens(tmp_path):
    path = tmp_path / 'history.sqlite3'
    cache = HistoryCache(path)
    base = datetime(2026, 10, 10, tzinfo=timezone.utc)
    old = evidence(base - timedelta(days=10))
    cache.ingest('origin', *old)
    for _ in range(5001):
        cache.ingest('origin', *evidence(base))
    assert len(cache.records('origin', since=base - timedelta(days=1), until=base)) == 5001
    plan = cache.connection.execute(
        'EXPLAIN QUERY PLAN SELECT h.run_id FROM history h JOIN history_finish f '
        'ON (f.origin=h.origin AND f.run_id=h.run_id AND f.digest=h.digest) '
        'WHERE h.origin=? AND f.finished_utc>=? AND f.finished_utc<=?',
        ('origin', cache._utc_key(base - timedelta(days=1)), cache._utc_key(base)),
    ).fetchall()
    assert any('history_origin_window' in detail and 'finished_utc>?' in detail
               for _, _, _, detail in plan)
    assert len(cache.records('origin', since=base - timedelta(days=30), until=base)) == 5002
    assert len(cache.records('origin', since=base - timedelta(days=1), until=base)) == 5001
    cache.close()
    reopened = HistoryCache(path)
    assert len(reopened.records('origin', since=base - timedelta(days=30), until=base)) == 5002
    reopened.close()


def test_utc_offset_bounds_and_immutable_payload(tmp_path):
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    instant = datetime(2026, 10, 10, 12, tzinfo=timezone(timedelta(hours=2)))
    manifest, payload = evidence(instant)
    cache.ingest('origin', manifest, payload)
    assert len(cache.records('origin', since=datetime(2026, 10, 10, 10, tzinfo=timezone.utc),
                             until=datetime(2026, 10, 10, 10, tzinfo=timezone.utc))) == 1
    changed = json.loads(payload)
    changed['tests'] = [{'id': 'changed'}]
    with pytest.raises(ValueError, match='digest'):
        cache.ingest('origin', manifest, json.dumps(changed).encode())
    assert cache.records('origin')[0][1]['tests'] == []
    with pytest.raises(ValueError, match='timezone-aware'):
        cache.records('origin', since=datetime(2026, 10, 10))
    cache.close()


def test_legacy_migration_keeps_original_bytes_and_rejects_tamper(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    valid = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    tampered = evidence(datetime(2026, 10, 11, tzinfo=timezone.utc))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        for manifest, payload in (valid, tampered):
            encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
            alternate = json.loads(payload)
            alternate['estimator_version'] = 2
            stored = payload.decode() if (manifest, payload) == valid else json.dumps(alternate)
            connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                               ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, stored))
    cache = HistoryCache(path)
    assert [row[0]['run_id'] for row in cache.records('origin')] == [valid[0]['run_id']]
    assert cache.connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 2
    cache.close()


def test_late_manifest_reconciliation_and_corruption_reports_incomplete(monkeypatch):
    class Client:
        objects = {}
        def get_object(self, *, Bucket, Key):
            return {'Body': io.BytesIO(self.objects[Key])}
    client = Client()
    storage = RunStorage('bucket', client=client)
    early = evidence(datetime(2026, 10, 9, tzinfo=timezone.utc))
    late = evidence(datetime(2026, 10, 8, tzinfo=timezone.utc))
    manifests = [early[0]]
    monkeypatch.setattr(storage, 'history', lambda *args, **kwargs: manifests)
    client.objects[storage._key('repo', early[0]['run_id'], 'accounting.json')] = early[1]
    assert list(storage.accounting_history('repo')) == [early]
    manifests.insert(0, late[0])
    client.objects[storage._key('repo', late[0]['run_id'], 'accounting.json')] = late[1]
    assert list(storage.accounting_history('repo')) == [late, early]
    client.objects[storage._key('repo', late[0]['run_id'], 'accounting.json')] = b'bad'
    with pytest.raises(StorageError, match='Could not load Allure history'):
        list(storage.accounting_history('repo'))


def test_valid_conflicting_run_rejected_and_missing_index_repaired(tmp_path):
    path = tmp_path / 'history.sqlite3'
    cache = HistoryCache(path)
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    cache.ingest('origin', manifest, payload)
    cache.connection.execute('DELETE FROM history_finish')
    cache.connection.commit()
    assert cache.records('origin') == []
    cache.ingest('origin', manifest, payload)
    assert len(cache.records('origin')) == 1
    changed = json.loads(payload)
    changed['estimator_version'] = 2
    changed_payload = json.dumps(changed).encode()
    changed_manifest = dict(manifest, files={'accounting.json': sha256(changed_payload).hexdigest()})
    with pytest.raises(ValueError, match='Conflicting immutable run'):
        cache.ingest('origin', changed_manifest, changed_payload)
    assert cache.records('origin')[0][0] == manifest
    cache.close()


def test_duplicate_legacy_run_fails_closed_and_migration_rolls_back(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    changed = json.loads(payload)
    changed['estimator_version'] = 2
    changed_payload = json.dumps(changed).encode()
    changed_manifest = dict(manifest, files={'accounting.json': sha256(changed_payload).hexdigest()})
    changed_encoded = json.dumps(changed_manifest, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, payload.decode()))
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(changed_encoded.encode()).hexdigest(), changed_encoded, changed_payload.decode()))
    with pytest.raises(ValueError, match='Conflicting immutable run'):
        HistoryCache(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='history_finish'").fetchone()[0] == 0


@pytest.mark.parametrize('manifest_body', [b'{invalid', b'{}'])
def test_accounting_refresh_rejects_malformed_manifest(monkeypatch, manifest_body):
    class Client:
        def list_objects_v2(self, **kwargs):
            return {'Contents': [{'Key': 'allure/repo/run/manifest.json'}]}
        def get_object(self, **kwargs):
            return {'Body': io.BytesIO(manifest_body)}
    storage = RunStorage('bucket', client=Client())
    with pytest.raises(StorageError, match='Could not load Allure history'):
        list(storage.accounting_history('repo'))


def test_accounting_refresh_rejects_finalized_manifest_without_accounting():
    manifest, _ = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    manifest['files'] = {'media.mp4': 'a' * 64}
    class Client:
        def list_objects_v2(self, **kwargs):
            return {'Contents': [{'Key': f"allure/repo/{manifest['run_id']}/manifest.json"}]}
        def get_object(self, **kwargs):
            return {'Body': io.BytesIO(json.dumps(manifest).encode())}
    storage = RunStorage('bucket', client=Client())
    assert storage.history('repo') == [manifest]
    with pytest.raises(StorageError, match='Could not load Allure history'):
        list(storage.accounting_history('repo'))


def test_reopen_and_binding_decode_only_selected_evidence(tmp_path, monkeypatch):
    from api_agent.telemetry.history import binding_digest
    from api_agent.telemetry import accounting as accounting_module

    path = tmp_path / 'history.sqlite3'
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    old = evidence(now - timedelta(days=30))
    selected = evidence(now)
    cache = HistoryCache(path)
    cache.ingest('origin', *old)
    cache.ingest('origin', *selected)
    cache.close()

    validated = []
    original = accounting_module.validate_accounting
    def spy(artifact):
        validated.append(artifact['run_id'])
        return original(artifact)
    monkeypatch.setattr(accounting_module, 'validate_accounting', spy)

    reopened = HistoryCache(path)
    assert validated == []
    assert [manifest['run_id'] for manifest, _ in reopened.records(
        'origin', since=now - timedelta(days=1), until=now)] == [selected[0]['run_id']]
    assert validated == [selected[0]['run_id']]

    scopehash = 'a' * 64
    encoded = json.dumps(selected[0], sort_keys=True, separators=(',', ':'))
    manifest_digest = sha256(encoded.encode()).hexdigest()
    reopened.connection.execute('INSERT INTO allocation_bindings VALUES (?,?,?,?,?)',
                                ('origin', scopehash, selected[0]['run_id'], manifest_digest,
                                 binding_digest('origin', scopehash, selected[0]['run_id'], manifest_digest)))
    reopened.connection.commit()
    validated.clear()
    assert reopened.bound_allocation('origin', scopehash, now) is None
    assert validated == [selected[0]['run_id']]
    reopened.close()


def test_migration_rejects_valid_accounting_for_different_run(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    manifest, _ = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    _, other_payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    manifest['files']['accounting.json'] = sha256(other_payload).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, other_payload.decode()))
    cache = HistoryCache(path)
    assert cache.records('origin') == []
    assert cache.connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 1
    assert cache.connection.execute('SELECT COUNT(*) FROM history_finish').fetchone()[0] == 0
    cache.close()


def test_migration_keeps_malformed_legacy_row_but_does_not_index_it(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, '{invalid'))
    cache = HistoryCache(path)
    assert cache.records('origin') == []
    assert cache.connection.execute('SELECT accounting FROM history').fetchone()[0] == '{invalid'
    cache.close()


def test_records_reject_wrong_identity_finish_and_malformed_accounting(tmp_path):
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    instant = datetime(2026, 10, 10, tzinfo=timezone.utc)
    manifest, payload = evidence(instant)
    cache.ingest('origin', manifest, payload)
    digest = cache.connection.execute('SELECT digest FROM history').fetchone()[0]
    cache.connection.execute('UPDATE history SET run_id=?', (str(uuid4()),))
    cache.connection.execute('UPDATE history_finish SET run_id=?',
                             (cache.connection.execute('SELECT run_id FROM history').fetchone()[0],))
    cache.connection.commit()
    assert cache.records('origin') == []
    cache.connection.execute('UPDATE history SET run_id=?', (manifest['run_id'],))
    cache.connection.execute('UPDATE history_finish SET run_id=?,finished_utc=?',
                             (manifest['run_id'], cache._utc_key(instant + timedelta(days=1))))
    cache.connection.commit()
    assert cache.records('origin') == []
    cache.connection.execute('UPDATE history_finish SET finished_utc=?', (cache._utc_key(instant),))
    cache.connection.execute('UPDATE history SET accounting=?', ('{invalid',))
    cache.connection.commit()
    assert cache.records('origin') == []
    assert cache.connection.execute('SELECT digest FROM history').fetchone()[0] == digest
    cache.close()


def test_records_reject_valid_accounting_with_wrong_run(tmp_path):
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    cache.ingest('origin', manifest, payload)
    _, other_payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    manifest['files']['accounting.json'] = sha256(other_payload).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    digest = sha256(encoded.encode()).hexdigest()
    cache.connection.execute('UPDATE history SET digest=?,manifest=?,accounting=?',
                             (digest, encoded, other_payload.decode()))
    cache.connection.execute('UPDATE history_finish SET digest=?', (digest,))
    cache.connection.commit()
    assert cache.records('origin') == []
    cache.close()


def test_legacy_binding_keeps_window_without_claiming_allocation(tmp_path, monkeypatch):
    from api_agent.telemetry.history import binding_digest
    import api_agent.telemetry.history as history_module

    cache = HistoryCache(tmp_path / 'history.sqlite3')
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    manifest, payload = evidence(now)
    cache.ingest('origin', manifest, payload)
    scopehash = 'a' * 64
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    digest = sha256(encoded.encode()).hexdigest()
    cache.connection.execute('INSERT INTO allocation_bindings VALUES (?,?,?,?,?)',
                             ('origin', scopehash, manifest['run_id'], digest,
                              binding_digest('origin', scopehash, manifest['run_id'], digest)))
    cache.connection.commit()
    artifact = json.loads(payload)
    artifact['quota'] = {'before': {'window_id': 'old-window'},
                         'after': {'reset_at': (now + timedelta(days=1)).isoformat()}}
    monkeypatch.setattr(cache, 'records', lambda *args, **kwargs: [(manifest, artifact)])
    monkeypatch.setattr(history_module, 'binding_eligible', lambda candidate: candidate is artifact)
    assert cache.bound_window('origin', scopehash, now) == 'old-window'
    assert cache.bound_allocation('origin', scopehash, now) is None
    cache.close()


def test_strict_history_rejects_oversized_and_invalid_finalized_manifest():
    valid, _ = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    invalid = dict(valid, status='pending')
    cases = [b' ' * 2_000_001, json.dumps(invalid).encode()]
    for body in cases:
        class Client:
            def list_objects_v2(self, **kwargs):
                return {'Contents': [{'Key': f"allure/repo/{valid['run_id']}/manifest.json"}]}
            def get_object(self, **kwargs):
                return {'Body': io.BytesIO(body)}
        storage = RunStorage('bucket', client=Client())
        with pytest.raises(StorageError, match='Could not load Allure history'):
            list(storage.accounting_history('repo'))


def test_selected_summary_displays_incomplete_refresh():
    from api_agent.tui.cost_history import selected_summary
    class Snapshot:
        sync_unavailable = True
        def total(self, ids):
            return {'incomplete': True}
    assert selected_summary(Snapshot(), {'test'}) == 'Selected estimate: not enough historical data (history refresh incomplete)'


def test_migration_preserves_hash_valid_but_unparseable_accounting(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    manifest, _ = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    malformed = b'{invalid'
    manifest['files']['accounting.json'] = sha256(malformed).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, malformed.decode()))
    cache = HistoryCache(path)
    assert cache.records('origin') == []
    assert cache.connection.execute('SELECT COUNT(*) FROM history_finish').fetchone()[0] == 0
    cache.close()


def test_records_skip_hash_valid_malformed_manifest(tmp_path):
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    cache.ingest('origin', manifest, payload)
    invalid = dict(manifest, status='pending')
    encoded = json.dumps(invalid, sort_keys=True, separators=(',', ':'))
    digest = sha256(encoded.encode()).hexdigest()
    cache.connection.execute('UPDATE history SET digest=?,manifest=?', (digest, encoded))
    cache.connection.execute('UPDATE history_finish SET digest=?', (digest,))
    cache.connection.commit()
    assert cache.records('origin') == []
    cache.close()


def test_failed_legacy_migration_closes_connection_once_and_retains_rows(tmp_path, monkeypatch):
    import api_agent.telemetry.history as history_module

    path = tmp_path / 'legacy.sqlite3'
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    second = dict(manifest, status='failed')
    second_encoded = json.dumps(second, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        for body in (encoded, second_encoded):
            connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                               ('origin', manifest['run_id'], sha256(body.encode()).hexdigest(), body, payload.decode()))

    closed = []
    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()
    original_connect = sqlite3.connect
    monkeypatch.setattr(history_module.sqlite3, 'connect',
                        lambda *args, **kwargs: original_connect(*args, factory=TrackedConnection, **kwargs))

    with pytest.raises(ValueError, match='Conflicting immutable run'):
        HistoryCache(path)
    assert len(closed) == 1
    with original_connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='history_finish'").fetchone()[0] == 0


def test_interrupted_migration_closes_connection_and_rolls_back(tmp_path, monkeypatch):
    import api_agent.telemetry.history as history_module

    path = tmp_path / 'legacy.sqlite3'
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, payload.decode()))

    closed = []
    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()
    original_connect = sqlite3.connect
    monkeypatch.setattr(history_module.sqlite3, 'connect',
                        lambda *args, **kwargs: original_connect(*args, factory=TrackedConnection, **kwargs))
    def interrupt(_manifest):
        raise KeyboardInterrupt()
    monkeypatch.setattr(history_module, 'validate_manifest', interrupt)

    with pytest.raises(KeyboardInterrupt):
        HistoryCache(path)
    assert len(closed) == 1
    with original_connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='history_finish'").fetchone()[0] == 0


@pytest.mark.parametrize('index_sql,duplicate', [
    ('CREATE INDEX immutable_run ON history(origin,run_id)', True),
    ('CREATE UNIQUE INDEX immutable_run ON history(origin,run_id) WHERE 0', True),
    ('CREATE UNIQUE INDEX immutable_run ON history(origin,digest)', False),
])
def test_migration_rejects_untrusted_named_immutable_index(tmp_path, index_sql, duplicate):
    path = tmp_path / 'legacy.sqlite3'
    manifest, payload = evidence(datetime(2026, 10, 10, tzinfo=timezone.utc))
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    second = dict(manifest, status='failed')
    second_encoded = json.dumps(second, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin,run_id,digest))')
        connection.execute(index_sql)
        connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                           ('origin', manifest['run_id'], sha256(encoded.encode()).hexdigest(), encoded, payload.decode()))
        if duplicate:
            connection.execute('INSERT INTO history VALUES (?,?,?,?,?)',
                               ('origin', manifest['run_id'], sha256(second_encoded.encode()).hexdigest(),
                                second_encoded, payload.decode()))
    with pytest.raises(ValueError, match='Invalid immutable run index'):
        HistoryCache(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == (2 if duplicate else 1)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='history_finish'").fetchone()[0] == 0
