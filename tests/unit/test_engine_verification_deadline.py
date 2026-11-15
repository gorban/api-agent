"""Cleanup deadlines constrain engine verification as well as commands."""
from hashlib import sha256
from types import SimpleNamespace
import time

import pytest

from api_agent.runtime import containers, reporting_engine
from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.runtime.engine_targets import EngineTarget
from test_reporting_engine import engine, tmp_path


def _verified_runtime(engine, tmp_path, monkeypatch):
    executable = tmp_path / 'engine-cli'
    executable.write_text('synthetic engine')
    environment = {'DOCKER_HOST': 'unix://' + str(engine.path)}
    identity, transport = reporting_engine.verified_engine_binding(
        environment, executable=str(executable), runner=engine.runner, connection=engine.connection)
    target = EngineTarget('test', str(executable), sha256(executable.read_bytes()).hexdigest(),
                          identity, {'DOCKER_HOST': transport})
    deadlines = []
    class Connection(engine.connection):
        def request(self, method, route):
            deadlines.append(self.deadline)
            return super().request(method, route)
    def runner(command, **kwargs):
        deadlines.append(kwargs['deadline'])
        return engine.runner(command, **kwargs)
    real_binding = reporting_engine.verified_engine_binding
    def binding(environment, *, executable, cancelled):
        return real_binding(environment, executable=executable, cancelled=cancelled,
                            runner=runner, connection=Connection)
    monkeypatch.setattr(reporting_engine, 'verified_engine_binding', binding)
    runtime = ContainerRuntime(engine_target=target)
    return runtime, deadlines


def test_expired_cleanup_deadline_skips_verification_and_engine_command(monkeypatch):
    runtime = ContainerRuntime('docker')
    verified = []
    monkeypatch.setattr(runtime, '_verify_engine', lambda: verified.append(True))
    monkeypatch.setattr(containers.subprocess, 'run', lambda *args, **kwargs: pytest.fail('engine command'))

    with pytest.raises(ContainerError, match='deadline expired'):
        runtime._run(['network', 'ls'], deadline=time.monotonic() - 1)

    assert verified == []


def test_engine_handshake_inherits_remaining_cleanup_deadline(engine, tmp_path, monkeypatch):
    runtime, deadlines = _verified_runtime(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(containers.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=''))
    deadline = time.monotonic() + 2

    runtime._run(['network', 'ls'], timeout=10, deadline=deadline)

    assert len(deadlines) >= 4
    assert all(value <= deadline + .02 for value in deadlines)


def test_timed_verification_still_rejects_changed_engine(engine, tmp_path, monkeypatch):
    runtime, _ = _verified_runtime(engine, tmp_path, monkeypatch)
    engine.direct['ID'] = 'changed-engine'
    engine.cli['ID'] = 'changed-engine'
    monkeypatch.setattr(containers.subprocess, 'run', lambda *args, **kwargs: pytest.fail('engine command'))

    with pytest.raises(ContainerError, match='identity changed'):
        runtime._run(['network', 'ls'], timeout=10, deadline=time.monotonic() + 2)
