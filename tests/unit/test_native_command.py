"""Supervisor acknowledgements require actual canonical disposal observation."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from api_agent.testing import native_command, native_recovery


@pytest.fixture
def intent(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    directory = state / 'commands' / 'intent'
    directory.mkdir(mode=0o700, parents=True)
    (state / 'commands').chmod(0o700)
    descriptor = os.open(state / 'activity.lock', os.O_CREAT | os.O_RDWR, 0o600)
    request = {'version': 1, 'command': ['/usr/bin/true'], 'environment': {},
        'directory': str(state), 'deadline': time.monotonic()+5,
        'combine_output': False, 'output_limit': None}
    native_recovery._write(directory / 'request.json', request)
    monkeypatch.setattr(sys, 'argv', ['native-command', str(directory), str(descriptor)])
    yield SimpleNamespace(state=state, directory=directory, descriptor=descriptor, request=request)
    os.close(descriptor)


def test_supervisor_writes_ack_only_after_observed_disposal(intent, monkeypatch, capsys):
    def run(command, **kwargs):
        assert command == ('/usr/bin/true',)
        assert kwargs['inherited_fds'] == (intent.descriptor,)
        assert kwargs['cancelled']() is False
        kwargs['on_disposed']()
        return 'command output'
    monkeypatch.setattr(native_command, 'run_command', run)
    native_command.main()
    native_recovery._completed(intent.directory)
    assert capsys.readouterr().out == 'command output'


def test_supervisor_records_disposal_even_when_command_failed(intent, monkeypatch, capsys):
    def run(*args, **kwargs):
        kwargs['on_disposed']()
        raise OSError('command failed')
    monkeypatch.setattr(native_command, 'run_command', run)
    with pytest.raises(SystemExit) as caught:
        native_command.main()
    assert caught.value.code == 1
    native_recovery._completed(intent.directory)
    assert 'confirmed disposal' in capsys.readouterr().err


def test_success_without_disposal_observation_stays_pending(intent, monkeypatch):
    monkeypatch.setattr(native_command, 'run_command', lambda *a, **kw: '')
    with pytest.raises(ValueError, match='not confirmed'):
        native_command.main()
    assert not (intent.directory / 'complete.json').exists()
    with pytest.raises(ValueError, match='disposal remains unconfirmed'):
        with native_recovery._quiescent(intent.state):
            pytest.fail('unconfirmed native command admitted recovery')


def test_failure_without_disposal_observation_stays_pending(intent, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('post-spawn setup failure')
    monkeypatch.setattr(native_command, 'run_command', fail)
    with pytest.raises(OSError, match='post-spawn'):
        native_command.main()
    assert not (intent.directory / 'complete.json').exists()


def test_supervisor_ack_survives_absent_worker_output_pipe(intent, monkeypatch):
    def run(*args, **kwargs):
        kwargs['on_disposed']()
        return 'output for the departed worker'
    class Closed:
        def write(self, value):
            raise BrokenPipeError
    monkeypatch.setattr(native_command, 'run_command', run)
    monkeypatch.setattr(native_command.sys, 'stdout', Closed())
    native_command.main()
    native_recovery._completed(intent.directory)


@pytest.mark.parametrize('change', [
    {'version': 2}, {'command': []}, {'command': [1]}, {'environment': {'key': 1}},
    {'directory': 'relative'}, {'deadline': float('inf')}, {'combine_output': 1},
    {'output_limit': 1},
])
def test_supervisor_rejects_invalid_private_payload_before_execution(intent, change):
    request = {**intent.request, **change}
    (intent.directory / 'request.json').write_text(json.dumps(request))
    with pytest.raises(ValueError, match='Invalid private native command'):
        native_command.main()
    assert not (intent.directory / 'complete.json').exists()


@pytest.mark.parametrize('failure_stage', ['register', 'pipe_close'])
def test_real_post_spawn_failure_has_no_completion_and_blocks_recovery(intent, monkeypatch, failure_stage):
    from api_agent.runtime import prerequisites
    from api_agent.testing.processes import finalize
    processes = []
    original_popen = prerequisites.subprocess.Popen
    original_selector = prerequisites.selectors.DefaultSelector
    class UnclosedPipe:
        def __init__(self, pipe):
            self.pipe = pipe
        def fileno(self):
            return self.pipe.fileno()
        def close(self):
            raise OSError('pipe close failed before native disposal')
    def spawn(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        if failure_stage == 'pipe_close':
            process.stdout = UnclosedPipe(process.stdout)
        return process
    selectors = []
    class Selector:
        def __init__(self):
            self.real = original_selector()
            selectors.append(self.real)
        def register(self, *args):
            raise OSError('selector registration failed')
    monkeypatch.setattr(prerequisites.subprocess, 'Popen', spawn)
    if failure_stage == 'register':
        monkeypatch.setattr(prerequisites.selectors, 'DefaultSelector', Selector)
    try:
        with pytest.raises(OSError):
            native_command.main()
        assert not (intent.directory / 'complete.json').exists()
        with pytest.raises(ValueError, match='disposal remains unconfirmed'):
            with native_recovery._quiescent(intent.state):
                pytest.fail('post-spawn native command was not confirmed disposed')
    finally:
        for process in processes:
            finalize(process)
            output = process.stdout.pipe if isinstance(process.stdout, UnclosedPipe) else process.stdout
            output.close()
            process.stderr.close()
        for selector in selectors:
            selector.close()


def test_completion_is_bound_to_exact_command_intent(intent):
    native_recovery._write(intent.directory / 'complete.json',
        {'version': 1, 'request_sha256': '0'*64, 'disposed': True})
    with pytest.raises(ValueError, match='differs from its intent'):
        native_recovery._completed(intent.directory)
