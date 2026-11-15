"""Recovery must carry a bounded cleanup budget to remote engine calls."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from api_agent.runtime import containers, prerequisites
from api_agent.runtime.containers import ContainerRuntime
from api_agent.runtime.ownership import StackScope
from test_native_recovery import pending_intent


def test_recovery_passes_admitted_cleanup_budget_to_disposal(pending_intent):
    path = pending_intent.state / 'recovery.json'
    record = json.loads(path.read_text())
    record['dispose_on_release'] = True
    path.write_text(json.dumps(record))
    pending_intent.runtime.candidates = []
    calls = []
    pending_intent.runtime.dispose = lambda identifiers, **kwargs: calls.append((identifiers, kwargs))

    pending_intent.owner.recover(cleanup_timeout_seconds=60)

    assert calls == [([], {'nonce': 'a' * 32, 'fingerprints': {'service': 'f' * 64},
                           'cleanup_timeout_seconds': 60})]


def test_remote_network_cleanup_uses_admitted_budget(tmp_path, monkeypatch):
    scope = StackScope.from_roots(tmp_path / 'one', tmp_path / 'two')
    name, network_id, foreign_id = scope.project + '_default', 'a' * 64, '1' * 64
    clock = SimpleNamespace(value=0.0)
    commands, verified = [], []
    runtime = ContainerRuntime('docker', scope, ownership_directory=tmp_path / 'ownership')

    def verify():
        verified.append(clock.value)
        clock.value += 4

    def run(args, *, timeout, **kwargs):
        commands.append(tuple(args[1:]))
        clock.value += 1
        if timeout < 1:
            raise containers.subprocess.TimeoutExpired(args, timeout)
        if args[1:3] == ['ps', '--all']:
            output = foreign_id
        elif args[1:3] == ['network', 'ls']:
            output = name
        elif args[1] == 'ps':
            output = foreign_id
        elif args[1:3] == ['inspect', '--format']:
            output = json.dumps({'id': foreign_id, 'networks': {}, 'mode': 'bridge'})
        elif args[1:3] == ['network', 'inspect']:
            output = json.dumps([{'Name': name, 'Id': network_id, 'Labels': {
                'api-agent.network-cleanup-v1': scope.id, 'api-agent.managed': 'true',
                'api-agent.scope': scope.id, 'com.docker.compose.project': scope.project,
                'com.docker.compose.network': 'default', 'api-agent.acquisition': 'b' * 32}}])
        elif args[1:3] == ['network', 'rm']:
            output = ''
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(containers, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(runtime, '_verify_engine', verify)
    monkeypatch.setattr(containers.subprocess, 'run', run)
    monkeypatch.setattr(prerequisites.PrerequisiteManager, '_network_guard',
                        lambda *args, **kwargs: nullcontext())

    runtime.dispose([], nonce='b' * 32, fingerprints={'service': 'f' * 64},
                    cleanup_timeout_seconds=60)

    assert ('network', 'rm', network_id) in commands
    assert len(verified) == len(commands)
    assert 15 < clock.value < 60
    assert not any(command[0] in {'volume', 'image'} or command[:2] == ('network', 'prune')
                   for command in commands)


@pytest.mark.parametrize('budget', [0, -1, True, float('inf'), float('nan'), 301, '60'])
def test_invalid_recovery_budget_is_rejected_before_engine_access(pending_intent, budget):
    with pytest.raises(ValueError, match='Cleanup deadline'):
        pending_intent.owner.recover(cleanup_timeout_seconds=budget)


@pytest.mark.parametrize('budget', [0, -1, True, float('inf'), float('nan'), 301, '60'])
def test_invalid_disposal_budget_is_rejected_before_engine_access(tmp_path, monkeypatch, budget):
    runtime = ContainerRuntime('docker', StackScope.from_roots(tmp_path / 'one', tmp_path / 'two'),
                               ownership_directory=tmp_path / 'ownership')
    monkeypatch.setattr(runtime, '_run', lambda *args, **kwargs: pytest.fail('engine access'))
    with pytest.raises(containers.ContainerError, match='Cleanup deadline'):
        runtime.dispose([], nonce='a' * 32, fingerprints={'service': 'f' * 64},
                        cleanup_timeout_seconds=budget)


def test_engine_verification_cannot_extend_network_deadline(tmp_path, monkeypatch):
    runtime = ContainerRuntime('docker', StackScope.from_roots(tmp_path / 'one', tmp_path / 'two'))
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(containers, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(runtime, '_verify_engine', lambda: setattr(clock, 'value', 16))
    monkeypatch.setattr(containers.subprocess, 'run', lambda *args, **kwargs: pytest.fail('engine command'))

    with pytest.raises(containers.ContainerError, match='deadline expired'):
        runtime._run(['network', 'ls'], timeout=15, deadline=15)


def test_suite_forwards_contribution_budget_to_recovery(monkeypatch):
    from api_agent.testing import integration, native_recovery
    observed = []
    original = native_recovery.NativeRecoveryOwner.recover

    def recover(owner, **kwargs):
        observed.append(kwargs['cleanup_timeout_seconds'])
        return original(owner, **kwargs)

    monkeypatch.setattr(native_recovery.NativeRecoveryOwner, 'recover', recover)
    contribution = SimpleNamespace(cleanup_timeout=60, close=lambda **kwargs: None)

    @integration._admit_operation
    def execute(*, suite_directory):
        integration._RUNNER_OPERATION.prerequisites = contribution

    execute()
    assert observed == [60]
