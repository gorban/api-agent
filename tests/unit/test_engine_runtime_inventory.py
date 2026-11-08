"""Runtime qualification uses the verified engine API, independent of CLI dialect."""
import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest

from api_agent.runtime import engine_targets as subject


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.closed = False

    def read(self, maximum):
        return self.body[:maximum]

    def close(self):
        self.closed = True


class Connection:
    response = None
    instances: ClassVar[list] = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.instances.append(self)

    def request(self, method, path):
        assert (method, path) == ('GET', '/info')

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def controlled(monkeypatch):
    Connection.instances = []
    monkeypatch.setattr('api_agent.runtime.reporting_engine.UnixConnection', Connection)
    target = SimpleNamespace(environment={'DOCKER_HOST': 'unix:///engine.sock'}, verify=Mock())
    return target


@pytest.mark.parametrize('engine', ['docker', 'podman'])
def test_compatible_engine_runtime_inventory_uses_pinned_socket(controlled, engine):
    Connection.response = Response(json.dumps({'Runtimes': {'runsc': {'path': '/usr/bin/runsc'}},
                                                'ServerVersion': engine}).encode())
    assert 'runsc' in subject.runtime_inventory(controlled, cancelled=lambda: False)
    assert Connection.instances[0].path == '/engine.sock'
    assert Connection.instances[0].closed and Connection.response.closed
    assert controlled.verify.call_count == 2


@pytest.mark.parametrize('body,status', [
    (b'{}', 200), (b'{"Runtimes": []}', 200), (b'[]', 200), (b'bad', 200),
    (b'{"Runtimes": {"runsc": {}}}', 302), (b'x' * (1048576 + 1), 200),
])
def test_invalid_engine_response_is_closed_and_rejected(controlled, body, status):
    Connection.response = Response(body, status)
    with pytest.raises(ValueError):
        subject.runtime_inventory(controlled, cancelled=lambda: False)
    assert Connection.instances[0].closed and Connection.response.closed


def test_connection_request_failure_still_closes(controlled, monkeypatch):
    def fail(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(Connection, 'request', fail)
    with pytest.raises(OSError):
        subject.runtime_inventory(controlled, cancelled=lambda: False)
    assert Connection.instances[0].closed


def test_engine_binding_change_after_read_is_rejected(controlled):
    Connection.response = Response(b'{"Runtimes": {"runsc": {}}}')
    controlled.verify.side_effect = [None, ValueError('changed engine')]
    with pytest.raises(ValueError, match='changed engine'):
        subject.runtime_inventory(controlled, cancelled=lambda: False)
    assert Connection.instances[0].closed and Connection.response.closed


def test_non_unix_transport_is_rejected_without_connect(controlled):
    controlled.environment['DOCKER_HOST'] = 'tcp://remote:2375'
    with pytest.raises(ValueError, match='Unix'):
        subject.runtime_inventory(controlled, cancelled=lambda: False)
    assert Connection.instances == []


def test_both_identity_checks_share_whole_runtime_query_budget(controlled):
    from api_agent.providers import progress
    observed = []
    controlled.verify.side_effect = lambda **kw: observed.append(progress._scope.get().timeout)
    Connection.response = Response(b'{"Runtimes": {"runsc": {}}}')
    assert subject.runtime_inventory(controlled, timeout_seconds=2) == {'runsc': {}}
    assert len(observed) == 2
    assert all(0 < timeout <= 2 for timeout in observed)
