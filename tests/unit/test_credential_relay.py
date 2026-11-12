"""Tests read configured password-manager entries through their launcher's session."""
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from api_agent.auth import credentials
from api_agent.auth.credentials import (CredentialCapabilityUnsupportedError, CredentialError,
                                        CredentialLoginRequiredError, CredentialSource)
from api_agent.testing import credential_relay
from api_agent.testing.credential_relay import ENVIRONMENT, CredentialRelay, configured_sources, relayed

AUTH = {'password_manager': {'manager': '1password', 'cli_path': 'op'},
        'roles': {'Partner': [{'vault': 'Employee', 'user': 'builder'}],
                  'LLM User': [{'vault': 'Employee', 'user': 'gateway'}]}}
BUILDER = CredentialSource('1password', 'op', 'Employee', 'builder')
GATEWAY = CredentialSource('1password', 'op', 'Employee', 'gateway')
OTHER = CredentialSource('1password', 'op', 'Employee', 'someone-else')


@pytest.fixture
def relay(monkeypatch):
    calls = []
    def retrieve(source, kind, timeout):
        calls.append((source.user, kind))
        if source.user == 'gateway' and kind == 'totp':
            raise CredentialLoginRequiredError('locked')
        if source.user == 'gateway' and kind == 'password':
            raise CredentialCapabilityUnsupportedError('unsupported')
        if source.user == 'gateway' and kind == 'username':
            raise CredentialError('private failure detail')
        return f'{source.user}-{kind}'
    owner = CredentialRelay((BUILDER, GATEWAY), retrieve=retrieve)
    monkeypatch.setenv(ENVIRONMENT, owner.start())
    owner.calls = calls
    yield owner
    owner.close()


def test_configured_account_is_read_by_the_relay(relay):
    assert relayed(BUILDER, 'password', 5) == 'builder-password'
    assert relay.calls == [('builder', 'password')]


def test_unconfigured_account_is_refused_without_a_read(relay):
    with pytest.raises(CredentialError, match='^Credential relay refused the request.$'):
        relayed(OTHER, 'password', 5)
    assert relay.calls == []


@pytest.mark.parametrize('kind, error, message', [
    ('totp', CredentialLoginRequiredError, 'sign-in is required'),
    ('password', CredentialCapabilityUnsupportedError, 'capability is unsupported'),
    ('username', CredentialError, 'refused the request')])
def test_failures_keep_their_kind_but_not_their_detail(relay, kind, error, message):
    with pytest.raises(error, match=message) as raised:
        relayed(GATEWAY, kind, 5)
    assert 'private' not in str(raised.value)


def test_reads_are_serialized_so_one_approval_is_pending_at_most(monkeypatch):
    active, peak, lock = [0], [0], threading.Lock()
    def retrieve(source, kind, timeout):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return 'value'
    owner = CredentialRelay((BUILDER,), retrieve=retrieve)
    monkeypatch.setenv(ENVIRONMENT, owner.start())
    try:
        threads = [threading.Thread(target=relayed, args=(BUILDER, 'password', 5)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    finally:
        owner.close()
    assert peak[0] == 1


def raw_request(relay, payload):
    descriptor = json.loads(os.environ[ENVIRONMENT])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(descriptor['path'])
        sock.sendall(payload)
        return json.loads(sock.makefile().readline())


@pytest.mark.parametrize('change', [
    {'token': '0' * 64}, {'version': 2}, {'kind': 'login'}, {'kind': ['password']}, {'timeout': 0},
    {'timeout': True}, {'source': {'user': 'builder'}}, {'source': None},
    {'source': {'manager': '1password', 'cli_path': 'op', 'vault': 'Employee', 'user': 1}}, {'extra': 1}])
def test_malformed_requests_are_refused(relay, change):
    descriptor = json.loads(os.environ[ENVIRONMENT])
    request = {'version': 1, 'token': descriptor['token'], 'kind': 'password', 'timeout': 5,
               'source': {'manager': '1password', 'cli_path': 'op', 'vault': 'Employee', 'user': 'builder'}}
    assert raw_request(relay, (json.dumps(request | change) + '\n').encode()) == {'status': 'refused'}
    assert relay.calls == []


def test_non_finite_timeout_is_refused(relay):
    descriptor = json.loads(os.environ[ENVIRONMENT])
    payload = ('{"version":1,"token":"%s","kind":"password","timeout":1e999,"source":'
               '{"manager":"1password","cli_path":"op","vault":"Employee","user":"builder"}}\n') % descriptor['token']
    assert raw_request(relay, payload.encode()) == {'status': 'refused'}


def test_reply_failure_is_contained():
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def settimeout(self, value):
            pass
        def fileno(self):
            raise OSError('no peer')
        def sendall(self, data):
            raise OSError('peer gone')
    CredentialRelay((BUILDER,), retrieve=None)._serve(Socket())


def test_warm_reads_one_configured_username_and_hides_failure():
    calls = []
    def retrieve(source, kind, timeout):
        calls.append((source.user, kind))
        raise CredentialError('declined')
    CredentialRelay((BUILDER, GATEWAY), retrieve=retrieve).warm()
    CredentialRelay((), retrieve=retrieve).warm()
    assert calls == [('builder', 'username')]


def test_default_retrieval_is_the_local_read():
    assert CredentialRelay(())._retrieve is credentials._retrieve_locally


def test_failed_start_releases_its_directory(monkeypatch):
    created = []
    original = credential_relay.tempfile.TemporaryDirectory
    def directory(**kwargs):
        created.append(original(**kwargs))
        return created[-1]
    monkeypatch.setattr(credential_relay.tempfile, 'TemporaryDirectory', directory)
    monkeypatch.setattr(credential_relay.os, 'chmod', lambda *args: (_ for _ in ()).throw(OSError('denied')))
    with pytest.raises(OSError):
        CredentialRelay((BUILDER,)).start()
    assert not os.path.exists(created[0].name)


def test_without_a_relay_the_process_reads_locally(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT, raising=False)
    assert relayed(BUILDER, 'password', 5) is None


def test_ended_suite_falls_back_to_a_local_read(monkeypatch, tmp_path):
    monkeypatch.setenv(ENVIRONMENT, json.dumps({'version': 1, 'path': str(tmp_path / 'gone'), 'token': 'a' * 64}))
    assert relayed(BUILDER, 'password', 5) is None


@pytest.mark.parametrize('value', ['not json', '[]', '{"version": 1, "path": "/x", "token": "short"}'])
def test_invalid_descriptor_is_an_error(monkeypatch, value):
    monkeypatch.setenv(ENVIRONMENT, value)
    with pytest.raises(CredentialError, match='Invalid credential relay descriptor'):
        relayed(BUILDER, 'password', 5)


def test_relay_that_hangs_up_is_an_error(monkeypatch):
    import tempfile
    directory = tempfile.TemporaryDirectory(dir='/tmp')
    path = directory.name + '/r'
    with directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        def hang_up():
            connection, _ = server.accept()
            connection.close()
        thread = threading.Thread(target=hang_up)
        thread.start()
        monkeypatch.setenv(ENVIRONMENT, json.dumps({'version': 1, 'path': path, 'token': 'a' * 64}))
        with pytest.raises(CredentialError, match='^Credential relay failed.$'):
            relayed(BUILDER, 'password', 5)
        thread.join(5)


def test_configured_sources_list_every_role_account_once():
    duplicate = {**AUTH, 'roles': {**AUTH['roles'], 'Other': [{'vault': 'Employee', 'user': 'builder'}]}}
    assert configured_sources({'UNATTENDED_AUTH': duplicate}, None) == (BUILDER, GATEWAY)


@pytest.mark.parametrize('auth', [
    {'password_manager': {'manager': '', 'cli_path': ''}, 'roles': {}},
    {'password_manager': {'manager': '1password', 'cli_path': 'op'}, 'roles': {'Role': [{'vault': ''}]}},
    'not an object'])
def test_unconfigured_or_invalid_auth_serves_nothing(auth):
    assert configured_sources({'UNATTENDED_AUTH': auth}, None) == ()


def test_retrieval_prefers_the_relay(relay, monkeypatch):
    monkeypatch.setattr(credentials, '_retrieve_locally', lambda *args, **kwargs: pytest.fail('read locally'))
    assert credentials.get_password(BUILDER) == 'builder-password'
    assert credentials.get_username({'manager': '1password', 'cli_path': 'op', 'vault': 'Employee',
                                     'user': 'builder'}) == 'builder-username'


def test_sign_in_required_recovers_locally_only_when_attended(relay, monkeypatch):
    monkeypatch.setattr(credentials, '_retrieve_locally', lambda *args, **kwargs: 'recovered')
    assert credentials._retrieve(GATEWAY, 'totp', 5, interaction=object()) == 'recovered'
    with pytest.raises(CredentialLoginRequiredError):
        credentials._retrieve(GATEWAY, 'totp', 5)


def test_unreachable_relay_and_login_requests_read_locally(monkeypatch, tmp_path):
    monkeypatch.setenv(ENVIRONMENT, json.dumps({'version': 1, 'path': str(tmp_path / 'gone'), 'token': 'a' * 64}))
    monkeypatch.setattr(credentials, '_retrieve_locally', lambda source, kind, *args, **kwargs: 'local')
    assert credentials._retrieve(BUILDER, 'password', 5) == 'local'
    assert credentials._retrieve(BUILDER, SimpleNamespace(), 5) == 'local'


def test_accept_loop_ends_when_its_socket_closes():
    owner = CredentialRelay(())
    owner._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    owner._server.close()
    owner._accept()


def test_unstarted_relay_closes():
    CredentialRelay(()).close()
