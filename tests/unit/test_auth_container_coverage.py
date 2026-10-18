"""Controlled failure boundaries for credential processes and container metadata."""
import os
import json
import queue
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import api_agent.auth.credentials as credentials
import api_agent.runtime.containers as containers
from api_agent.runtime.ownership import StackScope


def test_credential_start_error_is_user_facing(monkeypatch):
    monkeypatch.setattr(credentials.subprocess, 'Popen', Mock(side_effect=OSError('private detail')))
    with pytest.raises(credentials.CredentialError, match='Unable to start configured credential CLI') as error:
        credentials._run(['missing'], 1)
    assert 'private detail' not in str(error.value)


def scripted_process(monkeypatch, events, *, waits=(0,), polls=(0,), platform='posix', kill_error=None):
    """Feed stream events deterministically, including events after both EOF markers."""
    event_queue = queue.Queue()
    for event in events:
        event_queue.put(event)
    monkeypatch.setattr(credentials.queue, 'Queue', lambda: event_queue)
    class IdleThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def join(self, **kwargs): pass
    monkeypatch.setattr(credentials.threading, 'Thread', IdleThread)
    wait_values = iter(waits)
    poll_values = iter(polls)
    def wait(*args, **kwargs):
        value = next(wait_values)
        if isinstance(value, Exception):
            raise value
        process.returncode = value
        return value
    process = SimpleNamespace(pid=4242, stdout=object(), stderr=object(),
                              wait=Mock(side_effect=wait), poll=Mock(side_effect=lambda: next(poll_values)),
                              kill=Mock(side_effect=kill_error), returncode=None)
    monkeypatch.setattr(credentials.subprocess, 'Popen', Mock(return_value=process))
    kill = Mock(side_effect=kill_error)
    monkeypatch.setattr(credentials.os, 'killpg', kill)
    process.disposed = False
    def dispose():
        if not process.disposed:
            process.disposed = True
            kill()
    process.dispose = dispose
    if platform == 'posix':
        from api_agent.auth import owned_process
        monkeypatch.setattr(owned_process, 'OwnedProcess', Mock(return_value=process))
        monkeypatch.setattr(credentials.os, 'waitid', lambda *args: object())
    if platform != 'posix':
        monkeypatch.setattr(credentials, 'os', SimpleNamespace(name=platform))
    return process, kill


def test_wait_timeout_terminates_owned_group(monkeypatch):
    process, kill = scripted_process(monkeypatch, [(0, None), (1, None)],
                                    waits=(subprocess.TimeoutExpired('cli', 1), -9), polls=(None,), platform='nt')
    with pytest.raises(credentials.CredentialError, match='timed out'):
        credentials._run(['cli'], 1)
    assert process.wait.call_count == 2
    process.kill.assert_called_once()


def test_process_already_gone_and_late_output_is_reported(monkeypatch):
    process, kill = scripted_process(monkeypatch, [(0, None), (1, None), (1, b'late failure')],
                                    waits=(3,), kill_error=ProcessLookupError())
    with pytest.raises(credentials.CredentialError, match='failed with exit code 3') as error:
        credentials._run(['cli'], 1)
    assert 'late failure' not in str(error.value)
    kill.assert_called_once_with()
    assert process.wait.call_count == 1


def test_invalid_utf8_and_final_cleanup(monkeypatch):
    process, kill = scripted_process(monkeypatch, [(0, b'\xff'), (0, None), (1, None)],
                                    waits=(0, 0), polls=(None,))
    with pytest.raises(credentials.CredentialError, match='invalid UTF-8'):
        credentials._run(['cli'], 1)
    kill.assert_called_once()
    assert process.wait.call_count == 1


def test_non_posix_owned_process_kill_and_process_lookup(monkeypatch):
    process, _ = scripted_process(monkeypatch, [(0, None), (1, None)],
                                  waits=(subprocess.TimeoutExpired('cli', 1), -9),
                                  polls=(None,), platform='nt', kill_error=ProcessLookupError())
    with pytest.raises(credentials.CredentialError, match='timed out'):
        credentials._run(['cli'], 1)
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_non_posix_finished_process_is_left_alone(monkeypatch):
    process, _ = scripted_process(monkeypatch, [(0, None), (1, None)], waits=(0,), polls=(0,), platform='nt')
    assert credentials._run(['cli'], 1) == ''
    process.kill.assert_not_called()


def test_credential_source_instance_is_revalidated(monkeypatch):
    source = credentials.CredentialSource('keeper', 'configured', 'vault', 'person')
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda value: '/safe/cli')
    run = Mock(return_value='secret\n')
    monkeypatch.setattr(credentials, '_run', run)
    assert credentials.get_password(source) == 'secret'
    assert run.call_args.args[0] == ['/safe/cli', 'find-password', 'vault/person']


def test_container_metadata_absent_os_failure_and_bad_quoting(monkeypatch):
    monkeypatch.setattr(containers.Path, 'read_text', Mock(side_effect=FileNotFoundError()))
    assert isinstance(containers.session_container_identity(), frozenset)
    monkeypatch.setattr(containers.Path, 'read_text', Mock(side_effect=PermissionError('private')))
    with pytest.raises(containers.ContainerError, match='Cannot identify'):
        containers.session_container_identity()
    monkeypatch.setattr(containers.Path, 'read_text', Mock(return_value='id="unclosed\n'))
    with pytest.raises(containers.ContainerError, match='Invalid session-container identity'):
        containers.session_container_identity()


def test_container_inventory_accepts_scoped_inspect_and_rejects_nonobject(monkeypatch):
    scope = StackScope('0123456789abcdef')
    runtime = containers.ContainerRuntime('/safe/docker', scope=scope)
    labels = {**scope.labels('otel'), 'com.docker.compose.project': scope.project,
              'com.docker.compose.service': 'jaeger'}
    identifier = 'a' * 64
    record = {'Id': identifier, 'Name': '/jaeger', 'Config': {'Labels': labels, 'Image': 'trace-image'},
              'State': {'Running': True, 'Status': 'running'}}
    monkeypatch.setattr(containers, 'session_container_identity', lambda: frozenset())
    monkeypatch.setattr(runtime, '_run', Mock(side_effect=[identifier, json.dumps([record])]))
    assert runtime.inventory() == (containers.Container(identifier, 'jaeger', 'trace-image', 'running'),)
    monkeypatch.setattr(runtime, '_run', Mock(side_effect=[identifier, '[1, 2]']))
    with pytest.raises(containers.ContainerError, match='invalid engine response'):
        runtime.inventory()


def test_non_posix_wait_timeout_disposes_once(monkeypatch):
    process, kill = scripted_process(monkeypatch, [(0, None), (1, None)],
                                    waits=(subprocess.TimeoutExpired('cli', 1), 0), polls=(None,), platform='nt')
    with pytest.raises(credentials.CredentialError, match='timed out'):
        credentials._run(['cli'], 1)
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


@pytest.mark.parametrize('missing_field', ['Id', 'Name'])
def test_container_inventory_requires_id_and_name(monkeypatch, missing_field):
    scope = StackScope('0123456789abcdef')
    runtime = containers.ContainerRuntime('/safe/docker', scope=scope)
    labels = {**scope.labels('otel'), 'com.docker.compose.project': scope.project,
              'com.docker.compose.service': 'jaeger'}
    identifier = 'a' * 64
    record = {'Id': identifier, 'Name': '/jaeger', 'Config': {'Labels': labels},
              'State': {'Running': True, 'Status': 'running'}}
    del record[missing_field]
    monkeypatch.setattr(containers, 'session_container_identity', lambda: frozenset())
    monkeypatch.setattr(runtime, '_run', Mock(side_effect=[identifier, json.dumps([record])]))
    with pytest.raises(containers.ContainerError, match='invalid engine response'):
        runtime.inventory()


@pytest.mark.parametrize('identifiers', [[], [''], ['-bad']])
def test_stop_rejects_empty_or_option_like_identifiers(identifiers):
    with pytest.raises(containers.ContainerError, match='Select valid container IDs'):
        containers.ContainerRuntime('/safe/docker', scope=StackScope('0123456789abcdef')).stop(identifiers)


def test_non_posix_process_finishes_before_termination(monkeypatch):
    process, _ = scripted_process(monkeypatch,
                                  [(1, b'Enter password:'), (0, None), (1, None)],
                                  waits=(0,), polls=(0, 0), platform='nt')
    with pytest.raises(credentials.CredentialError, match='requires authentication'):
        credentials._run(['cli'], 1)
    process.kill.assert_not_called()
