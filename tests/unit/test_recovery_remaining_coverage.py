"""Failure ownership and private DEBUG interaction joins beyond successful login."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.auth import credentials


@pytest.mark.parametrize('timeout', [0, -1, float('inf')])
def test_raw_cli_invalid_budget_never_spawns(timeout, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(credentials.subprocess, 'Popen', spawn)
    with pytest.raises(credentials.CredentialError, match='positive'):
        credentials._run(['unused'], timeout)
    spawn.assert_not_called()


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_oversized_cli_stream_is_disposed_without_echo(stream, monkeypatch):
    import sys
    if stream == 'stderr':
        from test_auth_container_coverage import scripted_process
        _, dispose = scripted_process(monkeypatch, [(1, b'x' * (1024 * 1024 + 16384)), (0, None), (1, None)])
        argv = ['synthetic']
    else:
        command = 'import sys; sys.stdout.write("x" * (1024 * 1024 + 16384)); sys.stdout.flush()'
        argv = [sys.executable, '-c', command]
    with pytest.raises(credentials.CredentialError, match='output exceeded its limit') as error:
        credentials._run(argv, 5)
    if stream == 'stderr':
        dispose.assert_called_once_with()
    assert 'xxx' not in str(error.value)


def test_failed_wait_still_disposes_once_and_reaps(monkeypatch):
    from test_auth_container_coverage import scripted_process
    process, dispose = scripted_process(monkeypatch, [(0, None), (1, None)], waits=(0,))
    monkeypatch.setattr(credentials.os, 'waitid', Mock(side_effect=OSError('synthetic wait failure')))
    with pytest.raises(OSError, match='synthetic wait failure'):
        credentials._run(['unused'], 1)
    dispose.assert_called_once_with()
    process.wait.assert_called_once_with()
    assert process.returncode == 0


@pytest.mark.parametrize('broken', ['raise', 'invalid'])
def test_adapter_classification_failures_never_leak_output(broken, monkeypatch):
    adapter = SimpleNamespace(command=lambda source, kind, cli: [cli], parse=lambda output, kind: output)
    def classify(output):
        if broken == 'raise':
            raise RuntimeError('private helper details')
        return 'not a boolean'
    adapter.classify_login = classify
    registry = SimpleNamespace(get=lambda identifier: SimpleNamespace(label='Synthetic'), load=lambda identifier: adapter)
    source = credentials.CredentialSource('synthetic', 'cli', 'vault', 'item', registry)
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda value: '/synthetic/cli')
    def run(argv, timeout, *, classify_login):
        classify_login('private transcript')
    monkeypatch.setattr(credentials, '_run', run)
    with pytest.raises(credentials.CredentialError, match='classification') as error:
        credentials.get_password(source)
    assert 'private' not in str(error.value)


def test_expired_parent_budget_prevents_retrieval(monkeypatch):
    from api_agent.providers import progress
    @contextmanager
    def exhausted(*args):
        yield SimpleNamespace(remaining=0)
    monkeypatch.setattr(progress, 'report_timeout_sync', exhausted)
    spawn = Mock()
    monkeypatch.setattr(credentials, '_run', spawn)
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda value: '/synthetic/cli')
    with pytest.raises(credentials.CredentialRecoveryError, match='deadline expired'):
        credentials.get_password({'manager': 'keeper', 'cli_path': 'cli', 'vault': 'vault', 'user': 'item'})
    spawn.assert_not_called()


def test_lock_acquired_after_deadline_never_retrieves(monkeypatch):
    now = iter([0, 2])
    lock = SimpleNamespace(acquire=Mock(return_value=True), release=Mock())
    interaction = SimpleNamespace(lock=lock)
    monkeypatch.setattr(credentials.time, 'monotonic', lambda: next(now))
    retrieve = Mock()
    monkeypatch.setattr(credentials, '_retrieve', retrieve)
    with pytest.raises(credentials.CredentialRecoveryError, match='deadline expired'):
        credentials._coordinated_interaction(None, 'password', 15, interaction, 1)
    retrieve.assert_not_called()
    lock.release.assert_called_once_with()


def test_debug_runner_owns_private_interaction_listener_and_snapshot(tmp_path, monkeypatch):
    from test_integration_runner import fixture_repo, context
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from api_agent.auth import interaction_transport
    import socket
    real_server = interaction_transport.InteractionServer
    servers = []
    def server(interaction):
        instance = real_server(interaction)
        servers.append(instance)
        return instance
    monkeypatch.setattr(interaction_transport, 'InteractionServer', server)
    base = fixture_repo(tmp_path / 'base', '''def test_context():
    import json, os
    from pathlib import Path
    data = json.loads(Path(os.environ['API_AGENT_INTEGRATION_CONTEXT']).read_text())
    assert data['interaction']['port'] > 0
    assert len(data['interaction']['token']) == 64
''')
    ctx = context(base)
    interaction = SimpleNamespace(open=Mock(side_effect=AssertionError('No login requested')))
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
                                   config={'DEBUG': True}, interaction=interaction)
    assert result.exit_code == 0 and len(result.tests) == 1 and result.tests[0].status == 'passed'
    interaction.open.assert_not_called()
    assert len(servers) == 1 and servers[0].closed and not servers[0].thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', servers[0].descriptor['port']), timeout=.2)
