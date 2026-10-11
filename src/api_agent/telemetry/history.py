"""Bounded verified accounting cache; never stores media or credentials."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from weakref import finalize
from threading import RLock
from uuid import UUID
from datetime import datetime

MAX_BYTES = 2_000_000


def cache_path():
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'api-agent' / 'cost-history.sqlite3'


def validate_manifest(manifest):
    """Accept only canonical finalized integration/E2E commit markers."""
    try:
        run_id = manifest['run_id']
        repo = manifest['repo']
        files = manifest['files']
        start = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(manifest['finished_at'].replace('Z', '+00:00'))
        valid = (str(UUID(run_id)) == run_id and type(manifest.get('schema')) is int and manifest['schema'] == 1
                 and manifest.get('status') in {'completed', 'failed', 'interrupted'} and manifest.get('mode') in {'integration', 'e2e'}
                 and isinstance(repo, str) and bool(repo) and repo not in {'.', '..'} and not any(c in repo for c in '/\\\0')
                 and start.tzinfo is not None and end.tzinfo is not None and start <= end
                 and isinstance(files, dict) and 'accounting.json' in files
                 and all(isinstance(name, str) and bool(name) and name not in {'.', '..', 'manifest.json'}
                         and not any(c in name for c in '/\\\0') and isinstance(digest, str) and len(digest) == 64
                         and all(c in '0123456789abcdef' for c in digest) for name, digest in files.items()))
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('Unfinalized history')
    return manifest


def binding_digest(origin, scopehash, run_id, manifest_digest):
    return sha256(json.dumps([origin, scopehash, run_id, manifest_digest], separators=(',', ':')).encode()).hexdigest()


def binding_eligible(artifact):
    from api_agent.telemetry.estimation import clean_quota, quota_allowed
    rows = artifact.get('tests') or []
    return (clean_quota(artifact.get('quota') or {}) and bool(rows) and all(quota_allowed(row) for row in rows)
            and any(((call.get('quote') or {}).get('accounting') or {}).get('billing_mode') == 'subscription' for row in rows for call in row.get('calls', [])))


class HistoryCache:
    def __init__(self, path=None):
        path = Path(path or cache_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.connection = sqlite3.connect(path, timeout=0.1, check_same_thread=False)
        self._finalizer = finalize(self, self.connection.close)
        self.connection.execute('CREATE TABLE IF NOT EXISTS history (origin TEXT, run_id TEXT, digest TEXT, manifest TEXT, accounting TEXT, PRIMARY KEY(origin, run_id, digest))')
        with self.connection:
            self.connection.execute('DELETE FROM history WHERE (origin,run_id) IN (SELECT origin,run_id FROM history GROUP BY origin,run_id HAVING COUNT(*)>1)')
            self.connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS immutable_run ON history(origin,run_id)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS allocation_bindings (origin TEXT, scopehash TEXT, run_id TEXT, PRIMARY KEY(origin,scopehash))')
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(allocation_bindings)')}
            if 'digest' not in columns:
                self.connection.execute('DROP TABLE allocation_bindings')
                self.connection.execute('CREATE TABLE allocation_bindings (origin TEXT, scopehash TEXT, run_id TEXT, manifest_digest TEXT, digest TEXT, PRIMARY KEY(origin,scopehash))')

    def ingest(self, origin, manifest, payload):
        from api_agent.telemetry.accounting import validate_accounting
        if not isinstance(origin, str) or not origin or type(payload) is not bytes or len(payload) > MAX_BYTES:
            raise ValueError('Invalid history evidence')
        validate_manifest(manifest)
        run_id = manifest['run_id']
        digest = manifest['files']['accounting.json']
        if sha256(payload).hexdigest() != digest:
            raise ValueError('Accounting digest mismatch')
        accounting = validate_accounting(json.loads(payload))
        if accounting['run_id'] != run_id:
            raise ValueError('Accounting run mismatch')
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        if len(encoded) > MAX_BYTES:
            raise ValueError('Oversized manifest')
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            existing = self.connection.execute('SELECT manifest FROM history WHERE origin=? AND run_id=?', (origin, run_id)).fetchone()
            if existing is not None and existing[0] != encoded:
                raise ValueError('Conflicting immutable run')
            self.connection.execute('INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)', (origin, run_id, sha256(encoded.encode()).hexdigest(), encoded, payload.decode('utf-8')))
            self.connection.execute('UPDATE history SET accounting=? WHERE origin=? AND run_id=?', (payload.decode('utf-8'), origin, run_id))

    def records(self, origin=None):
        query = 'SELECT run_id, digest, manifest, accounting FROM history'
        arguments = ()
        if origin is not None:
            query += ' WHERE origin=?'
            arguments = (origin,)
        with self.lock:
            rows = []
            from api_agent.telemetry.accounting import validate_accounting
            for stored_run_id, digest, manifest, accounting in self.connection.execute(query + ' ORDER BY run_id,digest LIMIT 5000', arguments):
                try:
                    if sha256(manifest.encode()).hexdigest() != digest:
                        continue
                    manifest = validate_manifest(json.loads(manifest))
                    if stored_run_id != manifest['run_id']:
                        continue
                    if sha256(accounting.encode()).hexdigest() != manifest['files']['accounting.json']:
                        continue
                    artifact = validate_accounting(json.loads(accounting))
                    if artifact['run_id'] != manifest['run_id']:
                        continue
                    rows.append((manifest, artifact))
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    continue
            return rows

    def bind(self, origin, scopehash, run_id):
        records = [(manifest, artifact) for manifest, artifact in self.records(origin) if manifest['run_id'] == run_id]
        if (not isinstance(scopehash, str) or len(scopehash) != 64 or any(c not in '0123456789abcdef' for c in scopehash)
                or not records or not binding_eligible(records[0][1])):
            return
        with self.lock, self.connection:
            manifest_digest = sha256(json.dumps(records[0][0], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            digest = binding_digest(origin, scopehash, run_id, manifest_digest)
            self.connection.execute('INSERT OR REPLACE INTO allocation_bindings VALUES (?,?,?,?,?)', (origin, scopehash, run_id, manifest_digest, digest))

    def bound_window(self, origin, scopehash, now):
        with self.lock:
            binding = self.connection.execute('SELECT run_id,manifest_digest,digest FROM allocation_bindings WHERE origin=? AND scopehash=?', (origin, scopehash)).fetchone()
        if binding is None or binding[2] != binding_digest(origin, scopehash, binding[0], binding[1]):
            return None
        for manifest, artifact in self.records(origin):
            quota = artifact.get('quota') or {}
            if (manifest['run_id'] == binding[0] and sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == binding[1] and binding_eligible(artifact)
                    and datetime.fromisoformat(quota['after']['reset_at'].replace('Z', '+00:00')) > now):
                return quota['before']['window_id']
        return None

    def close(self):
        self._finalizer()
