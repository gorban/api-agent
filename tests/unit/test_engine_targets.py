"""Synthetic engine adapters prove routing and refusal, never runtime isolation."""
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.runtime.engine_targets import EngineTarget, EngineTargetError, EngineTargetRegistry
from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.runtime.ownership import StackScope


@pytest.fixture
def engines(tmp_path, monkeypatch):
    executable = tmp_path / 'engine'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    identities = {'unix:///engine-a.sock': 'a' * 64, 'unix:///engine-b.sock': 'b' * 64}
    calls = []

    def verify(environment, *, executable, cancelled):
        assert not cancelled()
        transport = environment.get('DOCKER_HOST', 'unix:///engine-a.sock')
        calls.append((str(executable), dict(environment)))
        identity = identities[transport]
        if identity is None:
            raise OSError('unavailable')
        return identity, transport

    monkeypatch.setattr('api_agent.runtime.reporting_engine.verified_engine_binding', verify)
    targets = tuple(EngineTarget.capture('engine-' + suffix, str(executable), {'DOCKER_HOST': transport})
                    for suffix, transport in zip(('a', 'b'), identities))
    return targets, identities, calls


def test_binding_is_immutable_and_context_changes_do_not_retarget(engines, monkeypatch):
    (first, second), _, calls = engines
    monkeypatch.setenv('DOCKER_HOST', 'unix:///new-default.sock')
    monkeypatch.setenv('PATH', '/does-not-exist')
    first.verify()
    assert calls[-1][1] == dict(first.environment)
    assert first.key != second.key
    assert repr(first).startswith('<api_agent.runtime.engine_targets.EngineTarget object')
    with pytest.raises(TypeError):
        first.environment['DOCKER_HOST'] = 'unix:///new-default.sock'
    with pytest.raises(FrozenInstanceError):
        first.identity = 'c' * 64


@pytest.mark.parametrize('change', ['identity', 'unavailable', 'binary', 'removed', 'transport'])
def test_revalidation_refuses_changed_target(engines, change):
    (first, _), identities, _ = engines
    if change == 'identity':
        identities[first.environment['DOCKER_HOST']] = 'c' * 64
    elif change == 'unavailable':
        identities[first.environment['DOCKER_HOST']] = None
    elif change == 'binary':
        Path(first.executable).write_text('changed')
    elif change == 'removed':
        Path(first.executable).unlink()
    else:
        with pytest.raises(EngineTargetError, match='identity changed'):
            first.verify(verifier=lambda *args, **kwargs: (first.identity, 'unix:///other.sock'))
        return
    with pytest.raises(EngineTargetError, match='identity changed'):
        first.verify()


@pytest.mark.parametrize('changes', [
    {'label': ''}, {'label': None}, {'executable': 'relative'}, {'executable': None},
    {'identity': 'bad'}, {'identity': None}, {'executable_digest': 'bad'}, {'executable_digest': None},
    {'environment': {}}, {'environment': {'DOCKER_HOST': 'tcp://remote'}},
    {'environment': {'DOCKER_HOST': 'unix:///a', 'CONTAINER_HOST': 'unix:///b'}},
    {'environment': {'DOCKER_HOST': 'unix:///a', 'PROVIDER_SECRET': 'hidden'}},
])
def test_invalid_record_rejected(engines, changes):
    (target, _), _, _ = engines
    with pytest.raises(EngineTargetError):
        replace(target, **changes)


def test_capture_refuses_missing_executable_and_failed_handshake(tmp_path):
    with pytest.raises(EngineTargetError, match='executable'):
        EngineTarget.capture('first', str(tmp_path / 'missing'), {})
    executable = tmp_path / 'engine'
    executable.write_text('engine')
    executable.chmod(0o700)
    with pytest.raises(EngineTargetError, match='verification failed'):
        EngineTarget.capture('first', str(executable), {}, verifier=Mock(side_effect=OSError()))


def test_bounded_child_uses_target_transport_and_returns_output(engines, tmp_path):
    (target, _), _, _ = engines
    runner = Mock(return_value='result')
    cancelled = lambda: False
    assert target.run(('info',), directory=tmp_path, deadline=12, cancelled=cancelled, runner=runner) == 'result'
    runner.assert_called_once_with((target.executable, 'info'), environment=target.environment,
                                   directory=tmp_path, deadline=12, cancelled=cancelled)


def test_registry_recovers_only_explicit_targets_without_verifying_or_starting(engines, tmp_path):
    targets, _, calls = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    assert registry.targets() == ()
    for target in targets:
        registry.remember(target)
    registry.remember(targets[0])
    calls.clear()
    restored = EngineTargetRegistry(registry.directory).targets()
    assert [item.record() for item in restored] == [item.record() for item in targets]
    assert calls == []
    assert (registry.directory / 'engine-targets.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('document', [None, [], {}, {'version': True, 'targets': []}, {'version': 1.0, 'targets': []}, {'version': 2, 'targets': []},
    {'version': 1, 'targets': {}}, {'version': 1, 'targets': [{}]},
    {'version': 1, 'targets': [None]}, {'version': 1, 'targets': [] , 'extra': True},
    {'version': 1, 'targets': [{}] * 17}, 'invalid-json'])
def test_invalid_registry_is_not_an_empty_success(tmp_path, document):
    path = tmp_path / 'engine-targets.json'
    path.write_text('invalid-json' if document == 'invalid-json' else json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(EngineTargetError, match='Invalid'):
        EngineTargetRegistry(tmp_path).targets()


def test_duplicate_registry_targets_refuse(engines, tmp_path):
    (target, _), _, _ = engines
    path = tmp_path / 'engine-targets.json'
    path.write_text(json.dumps({'version': 1, 'targets': [target.record()] * 2}))
    path.chmod(0o600)
    with pytest.raises(EngineTargetError, match='Invalid'):
        EngineTargetRegistry(tmp_path).targets()


@pytest.mark.parametrize('mode', ['permissions', 'oversized', 'owner', 'nonregular'])
def test_registry_refuses_unsafe_record(tmp_path, monkeypatch, mode):
    path = tmp_path / 'engine-targets.json'
    path.write_text('{}')
    path.chmod(0o600)
    if mode == 'permissions':
        path.chmod(0o644)
    elif mode == 'oversized':
        path.write_bytes(b' ' * 131073)
    else:
        original = os.fstat
        def altered(descriptor):
            info = original(descriptor)
            return SimpleNamespace(st_mode=info.st_mode if mode == 'owner' else 0,
                                   st_uid=info.st_uid + 1 if mode == 'owner' else info.st_uid,
                                   st_size=info.st_size)
        monkeypatch.setattr(os, 'fstat', altered)
    with pytest.raises(EngineTargetError, match='Unsafe'):
        EngineTargetRegistry(tmp_path).targets()


@pytest.mark.parametrize('mode', ['permissions', 'owner', 'symlink'])
def test_registry_refuses_unsafe_directory(engines, tmp_path, monkeypatch, mode):
    (target, _), _, _ = engines
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    if mode == 'permissions':
        directory.chmod(0o755)
    elif mode == 'owner':
        monkeypatch.setattr(os, 'getuid', lambda: directory.stat().st_uid + 1)
    else:
        link = tmp_path / 'linked'
        link.symlink_to(directory)
        directory = link
    with pytest.raises(EngineTargetError, match='Unsafe'):
        EngineTargetRegistry(directory).remember(target)


def test_registry_bounds_known_targets(engines, tmp_path):
    (target, _), _, _ = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    for index in range(16):
        registry.remember(replace(target, identity=f'{index:064x}'))
    with pytest.raises(EngineTargetError, match='limit'):
        registry.remember(replace(target, identity='f' * 64))


def test_two_engines_identical_ids_route_inventory_stop_and_logs_to_selected_target(engines, tmp_path, monkeypatch):
    targets, identities, _ = engines
    scope = StackScope('0123456789abcdef')
    identifier = 'f' * 64
    record = {'Id': identifier, 'Name': '/service', 'Config': {'Labels': {**scope.labels('otel'), 'com.docker.compose.project': scope.project, 'com.docker.compose.service': 'service'}},
              'State': {'Running': True}}
    seen = []
    def output(arguments, environment):
        seen.append((arguments[0], environment['DOCKER_HOST']))
        return {'ps': identifier, 'inspect': json.dumps([record]), 'stop': '', 'logs': 'tail'}[arguments[0]]
    def subprocess_run(command, **options):
        return SimpleNamespace(returncode=0, stdout=output(command[1:], options['env']))
    def run(command, **options):
        return output(command[1:], options['environment'])
    monkeypatch.setattr('api_agent.runtime.containers.subprocess.run', subprocess_run)
    monkeypatch.setattr('api_agent.runtime.prerequisites.run_command', run)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    registry = EngineTargetRegistry(tmp_path / 'private')
    for target in targets:
        registry.remember(target)
    for target in EngineTargetRegistry(registry.directory).targets():
        runtime = ContainerRuntime(scope=scope, engine_target=target)
        assert runtime.inventory()[0].id == identifier
        runtime.stop((identifier,))
        assert runtime.logs(identifier) == 'tail'
        assert all(transport == target.environment['DOCKER_HOST'] for _, transport in seen)
        seen.clear()
    identities[targets[0].environment['DOCKER_HOST']] = None
    runtime = ContainerRuntime(scope=scope, engine_target=targets[0])
    with pytest.raises(ContainerError, match='unavailable'):
        runtime.inventory()
    with pytest.raises(ContainerError, match='unavailable'):
        runtime.logs(identifier)
    assert seen == []


def test_friendly_label_does_not_change_engine_namespace(engines):
    (target, _), _, _ = engines
    assert replace(target, label='another-label').key == target.key


@pytest.fixture
def bound_manager(engines, tmp_path):
    from test_prerequisites import Engine, Clock
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.prerequisites import PrerequisiteManager
    (target, _), identities, _ = engines
    clock, engine = Clock(), Engine()
    scope = StackScope('0123456789abcdef')
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, scope, executable=target.executable,
                        engine_environment=target.environment)
    registry = EngineTargetRegistry(tmp_path / 'private')
    manager = PrerequisiteManager(plan, scope, tmp_path / 'ownership', engine_target=target,
        engine_registry=registry, command_runner=engine.run, now=clock.now, sleep=clock.sleep,
        identity=lambda pid: 'boot:start', readiness_probe=lambda *args: True)
    engine.owner = manager
    return manager, engine, target, identities, registry


def test_bound_manager_acquire_readiness_and_cleanup_use_same_verified_target(bound_manager, monkeypatch):
    manager, engine, target, _, registry = bound_manager
    monkeypatch.setenv('DOCKER_HOST', 'unix:///changed-default.sock')
    lease = manager.acquire()
    assert lease.owned_ids
    assert manager.engine_id == target.lease_key
    assert registry.targets()[0].key == target.key
    manager.release()
    assert any(command[1] == 'stop' for command, _ in engine.commands)
    assert all(command[0] == target.executable and options['environment']['DOCKER_HOST']
               == target.environment['DOCKER_HOST'] for command, options in engine.commands)


@pytest.mark.parametrize('operation', ['command', 'cleanup', 'readiness'])
def test_bound_manager_refuses_changed_generation_before_dispatch(bound_manager, operation):
    from api_agent.runtime.prerequisites import PrerequisiteError
    manager, engine, target, identities, _ = bound_manager
    identities[target.environment['DOCKER_HOST']] = 'c' * 64
    action = {'command': lambda: manager._command((target.executable, 'ps'), 10),
              'cleanup': lambda: manager._command((target.executable, 'stop', 'f' * 64), 10, cleanup=True),
              'readiness': lambda: manager._probe('otel', 1, manager.environment)}[operation]
    with pytest.raises(PrerequisiteError, match='identity changed'):
        action()
    assert engine.commands == []


@pytest.mark.parametrize('mismatch', ['executable', 'environment'])
def test_manager_rejects_mismatched_bound_plan(bound_manager, mismatch, tmp_path):
    from api_agent.runtime.prerequisites import PrerequisiteManager, PrerequisiteError
    manager, _, target, _, _ = bound_manager
    plan = replace(manager.plan, command=('other-engine', *manager.plan.command[1:])) if mismatch == 'executable' else manager.plan
    options = {'engine_environment': {'DOCKER_HOST': 'unix:///other.sock'}} if mismatch == 'environment' else {}
    with pytest.raises(PrerequisiteError, match='differs from selected engine'):
        PrerequisiteManager(plan, manager.scope, tmp_path, engine_target=target, **options)


def test_target_run_default_runner_is_bound(engines, tmp_path, monkeypatch):
    (target, _), _, _ = engines
    runner = Mock(return_value='default result')
    monkeypatch.setattr('api_agent.runtime.prerequisites.run_command', runner)
    assert target.run(('info',), directory=tmp_path, deadline=3) == 'default result'
    assert runner.call_args.args[0][0] == target.executable


def test_recovered_selection_is_explicit_and_scopes_ownership_journals(engines, tmp_path, monkeypatch):
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    targets, _, _ = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    for target in targets:
        registry.remember(target)
    runtime = RecoveredContainerRuntime(registry, StackScope('0123456789abcdef'), tmp_path / 'ownership')
    assert runtime.known_targets() == tuple((target.key, target.label) for target in targets)
    with pytest.raises(EngineTargetError, match='Select'):
        runtime.inventory()
    with pytest.raises(EngineTargetError, match='Select'):
        runtime.select_target('unknown')
    runtime.select_target(targets[0].key)
    first = runtime.selected_runtime
    runtime.select_target(targets[1].key)
    assert runtime.selected_key == targets[1].key
    assert runtime.selected_label == targets[1].label
    assert first.ownership_directory != runtime.selected_runtime.ownership_directory
    monkeypatch.setattr(runtime.selected_runtime, 'inventory', lambda: ('selected',))
    stopped = Mock()
    logged = Mock(return_value='selected logs')
    monkeypatch.setattr(runtime.selected_runtime, 'stop', stopped)
    monkeypatch.setattr(runtime.selected_runtime, 'logs', logged)
    assert runtime.inventory() == ('selected',)
    runtime.stop(('f' * 64,))
    stopped.assert_called_once_with(('f' * 64,))
    assert runtime.logs('f' * 64, scrubber='scrubber') == 'selected logs'
    logged.assert_called_once_with('f' * 64, scrubber='scrubber')


def test_manager_and_recovery_guard_share_engine_specific_directory(bound_manager):
    manager, _, target, _, _ = bound_manager
    runtime = ContainerRuntime(scope=manager.scope, ownership_directory=manager.directory.parent.parent,
                               engine_target=target)
    assert runtime.ownership_directory == manager.directory


def test_killed_controlled_process_leaves_explicit_target_recoverable(engines, tmp_path):
    """SIGKILL proves record durability; no claim about an actual engine runtime."""
    import subprocess
    import sys
    targets, _, _ = engines
    directory = tmp_path / 'private'
    script = '''
import json, sys, time
from api_agent.runtime.engine_targets import EngineTarget, EngineTargetRegistry
registry = EngineTargetRegistry(sys.argv[1])
registry.remember(EngineTarget(**json.loads(sys.argv[2])))
print('recorded', flush=True)
time.sleep(60)
'''
    process = subprocess.Popen((sys.executable, '-c', script, str(directory), json.dumps(targets[0].record())),
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'recorded'
        process.kill()
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
    restored = EngineTargetRegistry(directory).targets()
    assert len(restored) == 1 and restored[0].key == targets[0].key
    restored[0].verify()


def test_tui_restart_selects_recorded_engine_and_displays_unavailable_state(engines, tmp_path):
    import asyncio
    from test_tui import fixture
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    targets, identities, _ = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    for target in targets:
        registry.remember(target)
    async def scenario():
        app = fixture(tmp_path / 'app', 'cleanup-containers')
        runtime = RecoveredContainerRuntime(registry, StackScope('0123456789abcdef'), tmp_path / 'ownership')
        app.container_runtime = runtime
        app.container_marked.add('f' * 64)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert 'unavailable' in app.container_summary
            identities[targets[0].environment['DOCKER_HOST']] = None
            await app.choose('container-engine-' + targets[0].key)
            await pilot.pause()
            assert app.container_marked == set()
            assert 'Selected engine: engine-a' in app.container_summary
            assert 'unavailable' in app.container_summary
            assert runtime.selected_key == targets[0].key
    asyncio.run(scenario())


def test_registry_symlink_does_not_disclose_private_path(tmp_path):
    path = tmp_path / 'engine-targets.json'
    path.symlink_to(tmp_path / 'other')
    with pytest.raises(EngineTargetError, match='unavailable') as caught:
        EngineTargetRegistry(tmp_path).targets()
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize('failed', [False, True])
def test_late_inventory_cannot_replace_new_engine_selection(monkeypatch, failed):
    import asyncio
    from api_agent.tui.container_management import ContainerManagement
    runtime = SimpleNamespace(selected_key='first', selected_label='first')
    def inventory():
        runtime.selected_key = 'second'
        if failed:
            raise ContainerError('old engine unavailable')
        return ()
    runtime.inventory = inventory
    app = SimpleNamespace(container_runtime=runtime, screen=object(), containers=('new-selection',))
    asyncio.run(ContainerManagement._refresh_managed_containers(app))
    assert app.containers == ('new-selection',)


def test_active_lease_on_one_engine_does_not_protect_identical_id_on_other(bound_manager, engines, monkeypatch):
    manager, engine, target, _, _ = bound_manager
    targets, _, _ = engines
    from api_agent.runtime.prerequisites import process_identity
    manager.identity = process_identity
    lease = manager.acquire()
    identifier = lease.owned_ids[0]
    record = {**engine.records[identifier], 'Name': '/service'}
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    seen = []
    def run(command, **options):
        seen.append((command[1], options['env']['DOCKER_HOST']))
        return SimpleNamespace(returncode=0, stdout=json.dumps([record]) if command[1] == 'inspect' else '')
    monkeypatch.setattr('api_agent.runtime.containers.subprocess.run', run)
    root = manager.directory.parent.parent
    active = ContainerRuntime(scope=manager.scope, ownership_directory=root, engine_target=target)
    other = ContainerRuntime(scope=manager.scope, ownership_directory=root, engine_target=targets[1])
    with pytest.raises(ContainerError, match='active invocation'):
        active.stop((identifier,))
    assert seen == []
    other.stop((identifier,))
    assert seen == [('inspect', targets[1].environment['DOCKER_HOST']), ('stop', targets[1].environment['DOCKER_HOST'])]


@pytest.mark.parametrize('changed', ['path', 'executable'])
def test_same_binding_execution_variants_share_active_lease_exclusion(bound_manager, monkeypatch, changed):
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    from api_agent.runtime.prerequisites import PrerequisiteManager, process_identity
    from api_agent.runtime.compose import plan_compose
    manager, engine, target, _, registry = bound_manager
    manager.identity = process_identity
    lease = manager.acquire()
    identifier = lease.owned_ids[0]
    if changed == 'path':
        variant = replace(target, environment={**target.environment, 'PATH': '/alternate:/usr/bin'})
    else:
        executable = Path(target.executable).with_name('alternate-engine')
        executable.write_bytes(Path(target.executable).read_bytes())
        executable.chmod(0o700)
        variant = replace(target, executable=str(executable))
    variant.verify()
    assert variant.key != target.key
    root = manager.directory.parent.parent
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, manager.scope, executable=variant.executable,
                        engine_environment=variant.environment)
    alternate = PrerequisiteManager(plan, manager.scope, root, engine_target=variant)
    assert alternate.directory == manager.directory
    assert alternate.engine_id == manager.engine_id
    registry.remember(variant)
    recovered = RecoveredContainerRuntime(registry, manager.scope, root)
    recovered.select_target(variant.key)
    assert recovered.selected_runtime.ownership_directory == manager.directory
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    dispatch = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps([
        {**engine.records[identifier], 'Name': '/service'}])))
    monkeypatch.setattr('api_agent.runtime.containers.subprocess.run', dispatch)
    with pytest.raises(ContainerError, match='active invocation'):
        recovered.stop((identifier,))
    dispatch.assert_not_called()


def test_bound_manager_does_not_require_recovery_registry(bound_manager, tmp_path):
    from api_agent.runtime.prerequisites import PrerequisiteManager
    manager, _, target, _, _ = bound_manager
    copy = PrerequisiteManager(manager.plan, manager.scope, tmp_path, engine_target=target,
                               engine_environment=target.environment)
    assert copy.engine_id == target.lease_key


def test_stop_confirmation_captures_engine_before_selection_changes(engines, tmp_path, monkeypatch):
    import asyncio
    from test_tui import fixture
    from api_agent.runtime.containers import Container
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    recorded, _, _ = engines
    targets = tuple(replace(target, label="ordinary") for target in recorded)
    registry = EngineTargetRegistry(tmp_path / 'private')
    for target in targets:
        registry.remember(target)
    async def scenario():
        app = fixture(tmp_path / 'app')
        runtime = RecoveredContainerRuntime(registry, StackScope('0123456789abcdef'), tmp_path / 'ownership')
        app.container_runtime = runtime
        runtime.select_target(targets[0].key)
        first = runtime.selected_runtime
        runtime.select_target(targets[1].key)
        second = runtime.selected_runtime
        runtime.select_target(targets[0].key)
        # Keep the concrete first object so the confirmation can be observed.
        runtime.selected_runtime = first
        selected = Container('f' * 64, 'service', 'image', 'running')
        app.containers = (selected,)
        app.container_marked = {selected.id}
        calls = []
        monkeypatch.setattr(first, 'stop', lambda ids: calls.append(('first', ids)))
        monkeypatch.setattr(second, 'stop', lambda ids: calls.append(('second', ids)))
        async def refresh():
            pass
        monkeypatch.setattr(app, '_refresh_container_inventory', refresh)
        callbacks = []
        monkeypatch.setattr(app, 'push_screen', lambda screen, callback: callbacks.append((screen, callback)))
        workers = []
        monkeypatch.setattr(app, 'run_worker', lambda coroutine, **options: workers.append(coroutine))
        app.request_container_stop()
        dialog, confirm = callbacks[0]
        display = 'ordinary [' + targets[0].key[:12] + ']'
        assert dialog.engine_label == display
        assert 'Selected engine: ' + display in str(next(dialog.compose()).content)
        runtime.selected_runtime = second
        runtime.selected_key = targets[1].key
        runtime.selected_label = 'ordinary'
        assert dialog.engine_label == display
        assert targets[1].key[:12] not in str(next(dialog.compose()).content)
        confirm(True)
        await workers[0]
        assert calls == [('first', (selected.id,))]
    asyncio.run(scenario())



def test_busy_registry_rejects_immediately_without_mutation(engines, tmp_path):
    import fcntl
    targets, _, _ = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    registry.remember(targets[0])
    before = (registry.directory / 'engine-targets.json').read_bytes()
    with (registry.directory / 'engine-targets.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EngineTargetError, match='busy'):
            registry.remember(targets[1])
    assert (registry.directory / 'engine-targets.json').read_bytes() == before


@pytest.fixture
def tool_inventory(tmp_path):
    from hashlib import sha256
    from api_agent.runtime.inventory import InventoryCatalog
    scope = StackScope('0123456789abcdef')
    body = 'version = 1\nowner = "example"\n[[services]]\nid = "service"\n'
    authority = [{'owner': 'example', 'root': str(tmp_path), 'path': 'services.toml'}]
    catalog = InventoryCatalog.from_wire({'scope': scope.id, 'authority': authority,
        'declarations': [{'body': body, 'sha256': sha256(body.encode()).hexdigest()}]},
        scope=scope, authority=authority)
    return scope, catalog


def test_named_tool_inventory_authority_admits_only_exact_declared_pair(tool_inventory):
    scope, catalog = tool_inventory
    name, profile = catalog.pairs[0]
    labels = {**scope.labels(profile, profiles={profile}), 'com.docker.compose.project': scope.project,
              'com.docker.compose.service': name}
    record = {'Id': 'a' * 64, 'Name': '/service', 'Config': {'Labels': labels}}
    runtime = ContainerRuntime(scope=scope, inventory_catalog=catalog)
    assert runtime._eligible(record, set())
    labels['com.docker.compose.service'] = 'undeclared'
    assert not runtime._eligible(record, set())
    labels['com.docker.compose.service'] = name
    labels['api-agent.profile'] = 'infra-unknown'
    assert not runtime._eligible(record, set())
    labels['api-agent.profile'] = profile
    assert not ContainerRuntime(scope=scope)._eligible(record, set())


@pytest.mark.parametrize('mode', ['wrong-type', 'scope-missing', 'scope-mismatch'])
def test_named_tool_inventory_rejects_invalid_authority(tool_inventory, mode):
    scope, catalog = tool_inventory
    options = {'wrong-type': (scope, object()), 'scope-missing': (None, catalog),
               'scope-mismatch': (StackScope('f' * 16), catalog)}
    selected_scope, selected_catalog = options[mode]
    with pytest.raises(ContainerError, match='inventory authority'):
        ContainerRuntime(scope=selected_scope, inventory_catalog=selected_catalog)


def test_recovered_named_tool_runtime_retains_admitted_inventory(engines, tmp_path, tool_inventory):
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    (target, _), _, _ = engines
    scope, catalog = tool_inventory
    registry = EngineTargetRegistry(tmp_path / 'private')
    registry.remember(target)
    runtime = RecoveredContainerRuntime(registry, scope, tmp_path / 'ownership', inventory_catalog=catalog)
    runtime.select_target(target.key)
    assert runtime.selected_runtime.inventory_pairs == frozenset(catalog.pairs)


def test_same_label_recovered_generations_have_distinct_captions_and_selected_status(engines, tmp_path, monkeypatch):
    import asyncio
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    from api_agent.tui.container_management import ContainerManagement
    targets, identities, _ = engines
    registry = EngineTargetRegistry(tmp_path / 'private')
    generations = tuple(replace(target, label='ordinary') for target in targets)
    for target in generations:
        registry.remember(target)
    recovered = RecoveredContainerRuntime(EngineTargetRegistry(registry.directory),
        StackScope('0123456789abcdef'), tmp_path / 'ownership')
    views = []
    class View(ContainerManagement):
        container_runtime = recovered
        containers = ()
        container_marked = set()
        container_summary = ''
        screen = SimpleNamespace(kind='containers')
        def container_log_scrubber(self):
            return lambda text: text
        def show(self, *arguments):
            views.append(arguments)
        def refresh_managed_containers(self):
            pass
    app = View()
    app.show_container_management()
    actions = dict(views[-1][3])
    captions = [actions['container-engine-' + target.key] for target in generations]
    assert captions == ['Select engine: ordinary [' + target.key[:12] + ']' for target in generations]
    assert captions[0] != captions[1]
    selected = generations[1]
    display = 'ordinary [' + selected.key[:12] + ']'
    app.select_container_engine(selected.key)
    assert app.container_summary == 'Selected engine: ' + display + '; checking…'
    actions = dict(views[-1][3])
    assert actions['container-engine-' + selected.key] == '✓ Select engine: ' + display
    assert actions['container-engine-' + generations[0].key] == captions[0]
    assert recovered.selected_key == selected.key
    monkeypatch.setattr('api_agent.runtime.containers.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=''))
    asyncio.run(app._refresh_managed_containers())
    assert app.container_summary == 'Selected engine: ' + display + '\nManaged containers: 0 running, 0 stopped'
    identities[selected.environment['DOCKER_HOST']] = None
    asyncio.run(app._refresh_managed_containers())
    assert app.container_summary == 'Selected engine: ' + display + '\nContainer inventory unavailable.'
    assert selected.environment['DOCKER_HOST'] not in app.container_summary
    assert selected.identity not in app.container_summary



def test_log_title_and_loader_capture_generation_before_same_label_selection_changes(engines, tmp_path, monkeypatch):
    from api_agent.runtime.containers import Container
    from api_agent.runtime.engine_targets import RecoveredContainerRuntime
    from api_agent.tui.container_management import ContainerManagement
    recorded, _, _ = engines
    targets = tuple(replace(target, label='ordinary') for target in recorded)
    registry = EngineTargetRegistry(tmp_path / 'private')
    for target in targets:
        registry.remember(target)
    runtime = RecoveredContainerRuntime(registry, StackScope('0123456789abcdef'), tmp_path / 'ownership')
    runtime.select_target(targets[0].key)
    first = runtime.selected_runtime
    first_logs = Mock(return_value='first generation logs')
    monkeypatch.setattr(first, 'logs', first_logs)
    item = Container('f' * 64, 'same-service', 'image', 'running')
    table = SimpleNamespace(cursor_coordinate=None,
        coordinate_to_cell_key=lambda coordinate: SimpleNamespace(row_key=SimpleNamespace(value=item.id)))
    opened = []
    scrubber = lambda text: text
    class View(ContainerManagement):
        container_runtime = runtime
        containers = (item,)
        screen = SimpleNamespace(query=lambda cls: [table], query_one=lambda cls: table)
        def container_log_scrubber(self):
            return scrubber
        def push_screen(self, screen):
            opened.append(screen)
    app = View()
    app.request_container_logs()
    logs = opened[0]
    title = 'Container logs: same-service | Engine: ordinary [' + targets[0].key[:12] + ']'
    assert logs.title_text == title
    runtime.select_target(targets[1].key)
    second_logs = Mock(return_value='second generation logs')
    monkeypatch.setattr(runtime.selected_runtime, 'logs', second_logs)
    cancelled = lambda: False
    assert logs.loader(cancelled=cancelled) == 'first generation logs'
    first_logs.assert_called_once_with(item.id, cancelled=cancelled, scrubber=scrubber)
    second_logs.assert_not_called()
    assert logs.title_text == title
    assert targets[1].key[:12] not in logs.title_text
    assert all(target.environment['DOCKER_HOST'] not in logs.title_text for target in targets)
