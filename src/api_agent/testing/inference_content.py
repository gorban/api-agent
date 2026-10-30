"""Explicitly enabled, credential-scrubbed inference evidence for test history."""
import json
import os
import sqlite3
import threading
from contextlib import closing
from uuid import UUID

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, _credential_key


def content_boundary(config):
    boundary = EmissionBoundary(ContentPolicy(config.get('LOG_CONTENT') is True), preserve_json_text=True)
    for name, value in config.items():
        if _credential_key(name) and type(value) is str and value:
            boundary.register_secret(value)
    return boundary


def export_content(boundary, request, result=None):
    if not boundary.policy.allow_content:
        return ()
    from api_agent.providers.inference import request_to_wire, result_to_wire
    rows = [('request', request_to_wire(request))]
    if result is not None:
        rows.append(('response', result_to_wire(result)))
    for _, content in rows:
        messages = content.get('messages', [content['message']] if 'message' in content else [])
        for message in messages:
            message.pop('continuation', None)
    return tuple({'phase': phase, 'content': boundary.export(content)} for phase, content in rows)


def publish_content(rows, call_id):
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node or call_id is None:
        return
    sidecar = os.environ.get('API_AGENT_INFERENCE_EVIDENCE_DB')
    for row in rows:
        if sidecar:
            marker = None
            try:
                marker = _start_marker(sidecar, node.rsplit(' ', 1)[0])
                _publish_sidecar(sidecar, node.rsplit(' ', 1)[0], row, call_id)
                os.unlink(marker)
            except (OSError, ValueError, TypeError, sqlite3.Error):
                _signal_failure(sidecar, node.rsplit(' ', 1)[0])
                notify_failure(node.rsplit(' ', 1)[0])
            continue
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': 'inference_content', 'node': node.rsplit(' ', 1)[0],
                                    'data': dict(call_id=call_id, **row)}, allow_nan=False) + '\n')


def content_attachment(event, boundary):
    row = json.loads(event.text)
    from api_agent.telemetry.inference_diagnostics import valid_diagnostics
    if (type(row) is not dict or set(row) - {'reason', 'transport_attempt_id', 'capture_source', 'diagnostics'} != {'call_id', 'phase', 'content'}
            or ('diagnostics' in row and not valid_diagnostics(row['diagnostics']))
            or type(row['call_id']) is not str or str(UUID(row['call_id'])) != row['call_id']
            or row['phase'] not in ('request', 'response') or type(row['content']) is not dict):
        raise ValueError('Invalid inference content event')
    source = row.get('capture_source')
    if source not in (None, 'fallback', 'provider'):
        raise ValueError('Invalid provider capture source')
    attempt = row.get('transport_attempt_id')
    if attempt is not None and (type(attempt) is not str or str(UUID(attempt)) != attempt):
        raise ValueError('Invalid transport attempt identity')
    return (f"LLM {row['phase']} ({row['call_id']}{', attempt ' + attempt if attempt else ''}{', fallback' if source == 'fallback' else ''}{', partial' if row.get('reason') == 'partial' else ''})",
            json.dumps(boundary.content_export(row['content']), ensure_ascii=False, indent=2, allow_nan=False).encode(),
            'application/json')

# The runner owns this temporary database. SQLite transactions serialize writers
# from parallel pytest workers without sharing the legacy JSONL append stream.
CHUNK_BYTES = 200 * 1024
MAX_TEST_BYTES = 4 * 1024 * 1024
MAX_TEST_ROWS = 256
MARKER_RESERVE = 512
MAX_ROW_ENVELOPE = 8192


def initialize_evidence(path, nodes):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    for node in nodes:
        descriptor = os.open(status_path(path, node), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, b'0') != 1:
                raise OSError('Cannot initialize evidence status')
        finally:
            os.close(descriptor)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''
            CREATE TABLE budget (node TEXT PRIMARY KEY, bytes INTEGER NOT NULL DEFAULT 0,
                                 rows INTEGER NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE evidence (id INTEGER PRIMARY KEY, node TEXT NOT NULL, call_id TEXT NOT NULL,
                                   phase TEXT NOT NULL, length INTEGER NOT NULL, digest TEXT NOT NULL,
                                   chunks INTEGER NOT NULL, reason TEXT);
            CREATE TABLE part (evidence_id INTEGER NOT NULL, number INTEGER NOT NULL, data BLOB NOT NULL,
                               PRIMARY KEY (evidence_id, number));
        ''')
        db.executemany('INSERT INTO budget(node) VALUES (?)', ((node,) for node in nodes))


def _publish_sidecar(path, node, row, call_id):
    import hashlib
    if (type(call_id) is not str or str(UUID(call_id)) != call_id or
            type(row) is not dict or row.get('phase') not in ('request', 'response')):
        raise ValueError('Invalid inference evidence identity')
    raw = json.dumps(dict(call_id=call_id, **row), ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    content_size = len(json.dumps(row.get('content'), ensure_ascii=False,
                                  separators=(',', ':'), allow_nan=False).encode('utf-8'))
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        budget = db.execute('SELECT bytes, rows, stopped FROM budget WHERE node=?', (node,)).fetchone()
        if budget is None or budget[2]:
            return
        size, count, _ = budget
        truncated = (size + content_size > MAX_TEST_BYTES or len(raw) - content_size > MAX_ROW_ENVELOPE
                     or count >= MAX_TEST_ROWS - 1)
        if truncated:
            marker = {'call_id': call_id, 'phase': row['phase'], 'content': {}, 'reason': 'truncated'}
            if 'transport_attempt_id' in row:
                marker['transport_attempt_id'] = row['transport_attempt_id']
            if 'capture_source' in row:
                marker['capture_source'] = row['capture_source']
            raw = json.dumps(marker, separators=(',', ':')).encode()
            content_size = 0
        chunks = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
        cursor = db.execute('INSERT INTO evidence(node, call_id, phase, length, digest, chunks, reason) '
                            'VALUES (?, ?, ?, ?, ?, ?, ?)',
                            (node, call_id, row['phase'], len(raw), hashlib.sha256(raw).hexdigest(),
                             chunks, 'truncated' if truncated else None))
        db.executemany('INSERT INTO part VALUES (?, ?, ?)',
                       ((cursor.lastrowid, n, raw[n * CHUNK_BYTES:(n + 1) * CHUNK_BYTES])
                        for n in range(chunks)))
        db.execute('UPDATE budget SET bytes=bytes+?, rows=rows+1, stopped=? WHERE node=?',
                   (content_size, int(truncated), node))


def read_evidence(path, after):
    """Read only committed records, validating bounds, sequence and digest."""
    import hashlib
    result = []
    with closing(sqlite3.connect(path, timeout=0.05)) as db, db:
        records = db.execute('SELECT id, node, call_id, phase, length, digest, chunks, reason '
                             'FROM evidence WHERE id>? ORDER BY id LIMIT 1', (after,)).fetchall()
        for record in records:
            ident, node, call_id, phase, length, digest, count, reason = record
            if (type(length) is not int or not 0 < length <= MAX_TEST_BYTES + MAX_ROW_ENVELOPE or
                    type(count) is not int or count != (length + CHUNK_BYTES - 1) // CHUNK_BYTES):
                raise ValueError('Invalid inference evidence framing')
            parts = db.execute('SELECT number, data FROM part WHERE evidence_id=? ORDER BY number', (ident,)).fetchall()
            if len(parts) != count or any(n != i or type(data) is not bytes or
                    len(data) != min(CHUNK_BYTES, length - i * CHUNK_BYTES)
                    for i, (n, data) in enumerate(parts)):
                raise ValueError('Incomplete inference evidence framing')
            raw = b''.join(data for _, data in parts)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Invalid inference evidence digest')
            data = json.loads(raw)
            if (type(data) is not dict or data.get('call_id') != call_id or data.get('phase') != phase or
                    (data.get('reason') if reason else None) != reason):
                raise ValueError('Invalid inference evidence identity')
            result.append((ident, node, data))
    return result


def failure_marker(path, node):
    import hashlib
    return path + '.failed-' + hashlib.sha256(node.encode('utf-8')).hexdigest()


def status_path(path, node):
    return failure_marker(path, node) + '.status'


def failure_reported(path, node):
    if os.path.exists(failure_marker(path, node)):
        return True
    try:
        with open(status_path(path, node), 'rb') as stream:
            return stream.read(1) != b'0'
    except OSError:
        return True


def _signal_failure(path, node):
    try:
        descriptor = os.open(failure_marker(path, node), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        return
    except FileExistsError:
        return
    except OSError:
        pass
    # This preallocated byte is independent of creating a new marker, so a
    # directory-full failure still has a bounded reporting channel.
    try:
        descriptor = os.open(status_path(path, node), os.O_WRONLY)
        try:
            if os.write(descriptor, b'1') != 1:
                raise OSError('Short evidence status write')
        finally:
            os.close(descriptor)
    except OSError:
        # A missing status file is also treated as unavailable by the reader.
        try:
            os.unlink(status_path(path, node))
        except OSError:
            pass


def pending_prefix(path, node):
    return failure_marker(path, node) + '.pending-'


def _start_marker(path, node):
    prefix = pending_prefix(path, node)
    for slot in range(MAX_TEST_ROWS):
        marker = prefix + str(slot)
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return marker
    raise OSError('Inference evidence pending slots exhausted')


def has_pending(path, node):
    prefix = pending_prefix(path, node)
    return any(os.path.exists(prefix + str(slot)) for slot in range(MAX_TEST_ROWS))


_unacknowledged = set()
_unacknowledged_lock = threading.Lock()


class EvidencePublicationError(RuntimeError):
    """Evidence could not be stored or acknowledged by the runner."""


class EvidenceFailureChannel:
    """Bounded loopback notification channel owned by one runner suite."""
    def __init__(self, nodes):
        import hashlib
        import secrets
        self._nodes = {hashlib.sha256(node.encode()).hexdigest(): node for node in nodes}
        self._token = secrets.token_hex(32)
        self._reported = set()
        self._scoped = set()
        self._lock = __import__('threading').Lock()

    def __enter__(self):
        import socket
        import threading
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind(('127.0.0.1', 0))
            self._socket.listen(16)
            self._socket.settimeout(.1)
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        except BaseException:
            self._socket.close()
            raise
        self.descriptor = json.dumps({'port': self._socket.getsockname()[1], 'token': self._token})
        return self

    def _serve(self):
        import socket
        import hmac
        import time
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    data = b''
                    deadline = time.monotonic() + .2
                    while len(data) < 512 and not data.endswith(b'\n'):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        conn.settimeout(remaining)
                        part = conn.recv(512 - len(data))
                        if not part:
                            break
                        data += part
                    if not data.endswith(b'\n') or len(data) < 130:
                        continue
                    token, digest = data[:64].decode('ascii'), data[65:129].decode('ascii')
                    if data[64:65] != b' ' or not hmac.compare_digest(token, self._token):
                        continue
                    node = self._nodes.get(digest)
                    if node is None:
                        continue
                    suffix = data[129:-1]
                    scoped = None
                    if suffix:
                        if not suffix.startswith(b' '):
                            continue
                        try:
                            scoped = json.loads(suffix[1:])
                            if (type(scoped) is not dict or set(scoped) != {'invocation_id', 'visit_id', 'call_id', 'direction'}
                                    or scoped['direction'] not in ('input', 'output')
                                    or any(type(scoped[k]) is not str or str(UUID(scoped[k])) != scoped[k]
                                           for k in ('invocation_id', 'visit_id', 'call_id'))):
                                continue
                            scoped = (node, scoped['invocation_id'], scoped['visit_id'], scoped['call_id'], scoped['direction'])
                        except (ValueError, TypeError, UnicodeError):
                            continue
                    with self._lock:
                        if scoped is not None and len(self._scoped) >= 1024 and scoped not in self._scoped:
                            continue
                        self._reported.add(node)
                        if scoped is not None:
                            self._scoped.add(scoped)
                    conn.sendall(b'1')
                except (OSError, UnicodeError):
                    continue

    def reported(self):
        with self._lock:
            return frozenset(self._reported)

    def reported_scopes(self):
        with self._lock:
            return frozenset(self._scoped)

    def __exit__(self, *_):
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError('Evidence notification listener did not stop')


def notify_failure(node, scope=None):
    import hashlib
    import socket
    try:
        descriptor = json.loads(os.environ['API_AGENT_EVIDENCE_FAILURE_CHANNEL'])
        port, token = descriptor['port'], descriptor['token']
        if type(port) is not int or not 0 < port < 65536 or type(token) is not str or len(token) != 64:
            raise ValueError
        message = (token + ' ' + hashlib.sha256(node.encode()).hexdigest()).encode('ascii')
        if scope is not None:
            message += b' ' + json.dumps(scope, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        message += b'\n'
        with socket.create_connection(('127.0.0.1', port), timeout=1) as conn:
            conn.settimeout(1)
            conn.sendall(message)
            if conn.recv(1) != b'1':
                raise ValueError
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        with _unacknowledged_lock:
            _unacknowledged.add(node)
        raise EvidencePublicationError('Inference evidence unavailable and notification was not acknowledged') from None


def pytest_configure(config):
    """Install a worker-local fail-closed report latch, including under xdist."""
    import pytest
    class EvidenceReportLatch:
        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_logreport(self, report):
            if report.when != 'teardown':
                return
            with _unacknowledged_lock:
                failed = report.nodeid in _unacknowledged
                _unacknowledged.discard(report.nodeid)
            if failed:
                report.outcome = 'failed'
                report.longrepr = 'Inference evidence failure notification was not acknowledged.'
    config.pluginmanager.register(EvidenceReportLatch(), 'api-agent-evidence-report-latch')
