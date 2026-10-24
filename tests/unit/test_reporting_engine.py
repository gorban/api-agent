"""Synthetic Unix sockets and read-only CLI evidence; no real engines."""
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace

import pytest

from api_agent.runtime import reporting_engine as subject
from api_agent.runtime.prerequisites import PrerequisiteError

IDENTIFIER = 'a' * 64


@pytest.fixture
def tmp_path():
    # Physical ownership checks use a real trusted ancestor, not the test host's
    # potentially remapped shared temporary-directory owner.
    with tempfile.TemporaryDirectory(prefix='api-agent-reporting-test-', dir=Path.home()) as directory:
        yield Path(directory)


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / 'engine.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    state = SimpleNamespace(path=path, direct={'ID': 'engine-id', 'Name': 'host', 'DockerRootDir': '/store'},
        cli={'ID': 'engine-id'}, direct_ids=[{'Id': IDENTIFIER}], cli_ids=[IDENTIFIER], closed=[], status=200)
    class Connection:
        def __init__(self, address):
            assert address == str(path)
        def request(self, method, route):
            assert method == 'GET'
            self.route = route
        def getresponse(self):
            data = state.direct if self.route == '/info' else state.direct_ids
            return SimpleNamespace(status=state.status, read=lambda limit: json.dumps(data).encode(), close=lambda: None)
        def close(self):
            state.closed.append(True)
    state.connection = Connection
    def runner(command, **kwargs):
        assert kwargs['environment'] == {'DOCKER_HOST': 'unix://' + str(path)}
        return json.dumps(state.cli) if command[1] == 'info' else '\n'.join(state.cli_ids)
    state.runner = runner
    yield state
    server.close()


def identify(engine, **kwargs):
    return subject.engine_identity({'DOCKER_HOST': 'unix://' + str(engine.path)},
                                   runner=engine.runner, connection=engine.connection, **kwargs)


def test_matching_native_and_nonempty_podman_handshakes(engine):
    native = identify(engine)
    engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
    fallback = identify(engine)
    assert fallback != native and identify(engine) == fallback
    assert len(native) == len(fallback) == 64 and len(engine.closed) == 6


def test_verified_binding_exposes_canonical_transport_without_changing_identity(engine):
    environment = {'DOCKER_HOST': 'unix://' + str(engine.path)}
    identity, transport = subject.verified_engine_binding(environment,
        runner=engine.runner, connection=engine.connection)
    assert identity == identify(engine)
    assert transport == 'unix://' + str(engine.path.resolve())


@pytest.mark.parametrize('kind', ['empty-podman', 'wrong-id', 'wrong-host', 'wrong-store',
    'wrong-inventory', 'bad-id', 'duplicate-cli', 'duplicate-direct', 'malformed-direct', 'status'])
def test_ambiguous_or_mismatched_engine_is_refused(engine, kind):
    if kind == 'empty-podman':
        engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
        engine.cli_ids = engine.direct_ids = []
    if kind == 'wrong-id': engine.cli['ID'] = 'different'
    if kind in {'wrong-host', 'wrong-store'}:
        engine.cli = {'host': {'hostname': 'different' if kind == 'wrong-host' else 'host'},
                      'store': {'graphRoot': '/different' if kind == 'wrong-store' else '/store'}}
    if kind == 'wrong-inventory': engine.cli_ids = ['b' * 64]
    if kind == 'bad-id': engine.direct['ID'] = '../invalid'
    if kind == 'duplicate-cli': engine.cli_ids *= 2
    if kind == 'duplicate-direct': engine.direct_ids *= 2
    if kind == 'malformed-direct': engine.direct_ids = None
    if kind == 'status': engine.status = 500
    with pytest.raises(PrerequisiteError, match='Cannot verify'):
        identify(engine)
    assert engine.closed


@pytest.mark.parametrize('environment', [{'DOCKER_HOST': 'tcp://localhost:2375'},
    {'DOCKER_HOST': 'unix:///one', 'CONTAINER_HOST': 'unix:///two'}])
def test_nonlocal_or_ambiguous_transport_is_refused(environment):
    with pytest.raises(PrerequisiteError, match='unambiguous'):
        subject.engine_identity(environment)


def test_trusted_socket_alias_normalizes_to_same_generation(engine, monkeypatch):
    alias = engine.path.with_name('alias.sock')
    alias.symlink_to(engine.path)
    native = identify(engine)
    original = engine.runner
    def runner(command, **kwargs):
        kwargs['environment'] = {'DOCKER_HOST': 'unix://' + str(engine.path)}
        return original(command, **kwargs)
    assert subject.engine_identity({'DOCKER_HOST': 'unix://' + str(alias)}, runner=runner,
                                   connection=engine.connection) == native


def test_socket_replacement_during_handshake_is_refused(engine):
    original = engine.runner
    replacement = socket.socket(socket.AF_UNIX)
    def runner(command, **kwargs):
        if command[1] == 'ps':
            engine.path.unlink()
            replacement.bind(str(engine.path))
        return original(command, **kwargs)
    engine.runner, original_runner = runner, engine.runner
    try:
        with pytest.raises(PrerequisiteError, match='Cannot verify'):
            identify(engine)
    finally:
        replacement.close()


def test_unix_connection_owns_successful_and_failed_socket(engine):
    listener = socket.socket(socket.AF_UNIX)
    path = engine.path.with_name('listening.sock')
    listener.bind(str(path)); listener.listen()
    client = subject.UnixConnection(str(path))
    try:
        client.connect()
        assert client.sock.fileno() >= 0
    finally:
        client.close(); listener.close()
    with pytest.raises(OSError):
        subject.UnixConnection(str(path.with_name('absent.sock'))).connect()


@pytest.mark.parametrize('kind', ['file', 'foreign-owner', 'writable-parent'])
def test_socket_trust_checks_precede_cli(engine, monkeypatch, kind):
    if kind == 'file':
        engine.path.unlink(); engine.path.write_text('not a socket')
    if kind == 'foreign-owner':
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return os.stat_result((*value[:4], 99, *value[5:])) if path == engine.path else value
        monkeypatch.setattr(Path, 'lstat', lstat)
    if kind == 'writable-parent': engine.path.parent.chmod(0o777)
    engine.runner = lambda *args, **kwargs: pytest.fail('untrusted socket reached CLI')
    try:
        with pytest.raises(PrerequisiteError): identify(engine)
    finally:
        engine.path.parent.chmod(0o700)


def test_default_aliases_are_deduplicated_and_missing_or_distinct_sockets_refused(engine, monkeypatch):
    runtime = engine.path.parent / 'runtime'
    (runtime / 'podman').mkdir(parents=True)
    local = runtime / 'podman/podman.sock'
    local.symlink_to(engine.path)
    alias = engine.path.with_name('docker.sock'); alias.symlink_to(engine.path)
    real_path = Path
    monkeypatch.setattr(subject, 'Path', lambda value: alias if value == '/var/run/docker.sock' else real_path(value))
    runner = lambda args, **kwargs: engine.runner(args, **{**kwargs,
        'environment': {'DOCKER_HOST': 'unix://' + str(engine.path)}})
    expected = identify(engine)
    assert subject.engine_identity({'XDG_RUNTIME_DIR': str(runtime)}, runner=runner,
                                   connection=engine.connection) == expected
    alias.unlink(); local.unlink()
    with pytest.raises(PrerequisiteError):
        subject.engine_identity({'XDG_RUNTIME_DIR': str(runtime)}, runner=runner, connection=engine.connection)
    local.symlink_to(engine.path)
    other = socket.socket(socket.AF_UNIX)
    try:
        other.bind(str(alias))
        with pytest.raises(PrerequisiteError):
            subject.engine_identity({'XDG_RUNTIME_DIR': str(runtime)}, runner=runner, connection=engine.connection)
    finally:
        other.close()


def test_relative_runtime_socket_and_untrusted_resolved_parent_are_refused(engine, monkeypatch):
    runtime = engine.path.parent / 'podman'; runtime.mkdir()
    (runtime / 'podman.sock').symlink_to(engine.path)
    monkeypatch.chdir(engine.path.parent)
    real_path = Path
    monkeypatch.setattr(subject, 'Path', lambda value: real_path('absent.sock') if value == '/var/run/docker.sock' else real_path(value))
    with pytest.raises(PrerequisiteError):
        subject.engine_identity({'XDG_RUNTIME_DIR': '.'})
    monkeypatch.undo()
    with tempfile.TemporaryDirectory(dir=Path.home()) as other:
        alias = Path(other) / 'alias.sock'; alias.symlink_to(engine.path)
        engine.path.parent.chmod(0o777)
        try:
            with pytest.raises(PrerequisiteError):
                subject.engine_identity({'DOCKER_HOST': 'unix://' + str(alias)})
        finally:
            engine.path.parent.chmod(0o700)



@pytest.mark.parametrize('phase', ['header', 'body', 'cancel'])
def test_absolute_deadline_bounds_real_trickling_unix_peer(tmp_path, phase):
    import threading
    import time
    from api_agent.providers.progress import report_timeout_sync
    path = tmp_path / 'trickle.sock'
    listener = socket.socket(socket.AF_UNIX); listener.bind(str(path)); listener.listen()
    listener.settimeout(5)
    stopped, entered, cancel = threading.Event(), threading.Event(), threading.Event()
    admitted = threading.Event()
    observed, armed = [], []
    def serve():
        try:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(5); peer.recv(4096)
                entered.set()
                assert admitted.wait(5), 'Controlled peer was not admitted'
                if phase == 'header': payload = b'HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n' + b'x' * 10000
                else:
                    peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n')
                    payload = b'x' * 10000
                if phase == 'cancel': cancel.set()
                for byte in payload:
                    if stopped.wait(.004): break
                    peer.sendall(bytes([byte]))
                observed.append('settled')
        except (BrokenPipeError, ConnectionResetError):
            observed.append('closed')
        finally:
            stopped.set()
    class AdmittedConnection(subject.UnixConnection):
        def request(self, *args, **kwargs):
            super().request(*args, **kwargs)
            assert entered.wait(5), 'Controlled peer entry missing'
            armed.append(time.monotonic())
            self.sock.deadline = armed[-1] + (.15 if phase != 'cancel' else 5)
            admitted.set()
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        with pytest.raises(PrerequisiteError):
            with report_timeout_sync('Synthetic engine startup', 5):
                subject.engine_identity({'DOCKER_HOST': 'unix://' + str(path)}, cancelled=cancel.is_set,
                    connection=AdmittedConnection, runner=lambda *a, **k: pytest.fail('Incomplete response reached CLI'))
        assert entered.is_set()
        assert armed and time.monotonic() - armed[0] < 5
    finally:
        admitted.set(); stopped.set(); listener.close(); worker.join(5)
        assert not worker.is_alive()
    assert observed


def test_expired_or_canceled_connection_closes_allocated_socket(tmp_path):
    import time
    from api_agent.runtime.prerequisites import PrerequisiteCancelled
    client = subject.UnixConnection(str(tmp_path / 'absent'))
    client.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError): client.connect()
    assert client.sock.fileno() == -1
    client.cancelled = lambda: True
    with pytest.raises(PrerequisiteCancelled): client.connect()
    assert client.sock.fileno() == -1



def test_silent_intervals_poll_without_extending_absolute_budget(tmp_path):
    import threading
    import time
    path = tmp_path / 'delayed.sock'
    listener = socket.socket(socket.AF_UNIX); listener.bind(str(path)); listener.listen()
    listener.settimeout(5)
    def serve():
        for document in ({'ID': 'engine'}, []):
            peer, _ = listener.accept()
            with peer:
                peer.recv(4096)
                time.sleep(.15)
                body = json.dumps(document).encode()
                peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    worker = threading.Thread(target=serve); worker.start()
    try:
        value = subject.engine_identity({'DOCKER_HOST': 'unix://' + str(path)}, runner=lambda args, **kw:
            json.dumps({'ID': 'engine'}) if args[1] == 'info' else '')
        assert len(value) == 64
    finally:
        listener.close(); worker.join(5)
        assert not worker.is_alive()


def test_failed_http_request_still_closes_client(engine):
    class Failed(engine.connection):
        def request(self, *args): raise OSError('synthetic request failure')
    engine.connection = Failed
    with pytest.raises(PrerequisiteError): identify(engine)
    assert engine.closed == [True]



def test_identity_passes_inherited_remaining_budget_into_connection(engine, monkeypatch):
    import time
    from api_agent.providers import progress
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    captured = []
    class Captured(engine.connection):
        def request(self, *args):
            captured.append((self.timeout, self.deadline - time.monotonic()))
            raise OSError('synthetic request stop')
    engine.connection = Captured
    with progress.report_timeout_sync('Synthetic parent budget', 1):
        clock[0] = .75
        with pytest.raises(PrerequisiteError): identify(engine)
    timeout, remaining = captured[0]
    assert timeout == .25 and remaining <= .25



def test_compatibility_response_ids_and_inventory_changes_do_not_change_fallback_key(engine):
    engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
    engine.direct['ID'] = 'compatibility-request-1'
    original = identify(engine)
    engine.direct['ID'] = 'compatibility-request-2'
    assert identify(engine) == original
    engine.cli_ids.append('b' * 64)
    engine.direct_ids.append({'Id': 'b' * 64})
    engine.direct['ID'] = 'compatibility-request-3'
    assert identify(engine) == original
    engine.cli_ids.pop(); engine.direct_ids.pop()
    assert identify(engine) == original
    engine.cli_ids.clear(); engine.direct_ids.clear()
    with pytest.raises(PrerequisiteError): identify(engine)


def test_matched_native_engine_id_change_partitions_identity(engine):
    original = identify(engine)
    engine.direct['ID'] = engine.cli['ID'] = 'replacement-engine'
    assert identify(engine) != original


@pytest.mark.parametrize('field', ['host', 'store'])
def test_fallback_stable_namespace_change_partitions_identity(engine, field):
    engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
    original = identify(engine)
    if field == 'host': engine.cli['host']['hostname'] = engine.direct['Name'] = 'replacement-host'
    else: engine.cli['store']['graphRoot'] = engine.direct['DockerRootDir'] = '/replacement-store'
    assert identify(engine) != original


@pytest.mark.parametrize('hostname,store', [(None, '/store'), ([], '/store'), ('', '/store'),
    ('bad/host', '/store'), ('host', None), ('host', []), ('host', ''), ('host', 'relative'),
    ('host', '/' + 'x' * 4096), ('host', '/store\n')])
def test_fallback_requires_valid_stable_primitive_fields(engine, hostname, store):
    engine.cli = {'host': {'hostname': hostname}, 'store': {'graphRoot': store}}
    engine.direct['Name'], engine.direct['DockerRootDir'] = hostname, store
    with pytest.raises(PrerequisiteError): identify(engine)


def test_different_unix_transport_partitions_fallback_even_with_matching_inventory(engine):
    engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
    original = identify(engine)
    other = engine.path.with_name('other-engine.sock')
    listener = socket.socket(socket.AF_UNIX); listener.bind(str(other))
    class Connection(engine.connection):
        def __init__(self, address): assert address == str(other)
    def runner(command, **kwargs):
        assert kwargs['environment'] == {'DOCKER_HOST': 'unix://' + str(other)}
        return json.dumps(engine.cli) if command[1] == 'info' else '\n'.join(engine.cli_ids)
    try:
        changed = subject.engine_identity({'DOCKER_HOST': 'unix://' + str(other)},
                                           runner=runner, connection=Connection)
        assert changed != original
    finally:
        listener.close()
