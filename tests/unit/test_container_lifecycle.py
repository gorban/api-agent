"""Containers are disposable and storage is not: release removes containers and keeps volumes."""
import json
import shutil

import pytest

import api_agent.runtime.prerequisites as p
from test_prerequisites import setup  # noqa: F401


def test_release_stops_then_removes_the_containers_it_started_and_never_touches_storage(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    engine.commands.clear()

    assert manager.release() == lease.owned_ids

    seen = [command for command, _ in engine.commands]
    assert engine.records == {}
    assert [command[1] for command in seen if command[1] in {'stop', 'rm'}] == ['stop', 'rm']
    assert next(command for command in seen if command[1] == 'rm')[2:] == lease.owned_ids
    assert not any(command[1:3] in {('volume', 'rm'), ('volume', 'prune')} or 'down' in command
                   or {'-v', '-f', '--volumes', '--force'} & set(command) for command in seen)


def test_retaining_release_keeps_running_containers_and_removes_nothing(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    engine.commands.clear()

    assert manager.release(retain=True) == ()

    assert all(engine.records[item]['State']['Running'] for item in lease.container_ids)
    assert not any(command[1] in {'stop', 'rm'} for command, _ in engine.commands)


def test_release_removes_an_already_stopped_container_without_stopping_it_again(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    engine.records[lease.owned_ids[0]]['State']['Running'] = False
    engine.commands.clear()

    manager.release()

    assert engine.records == {}
    assert [command[1] for command, _ in engine.commands if command[1] in {'stop', 'rm'}] == ['rm']


def test_failed_removal_keeps_the_lease_and_a_later_release_completes_it(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    lease = manager.acquire()
    engine.fail_rm = True

    with pytest.raises(p.PrerequisiteError, match='rm failed'):
        manager.release()

    assert manager.lease is lease
    assert p.active_leased_ids(manager.directory, scope, identity=lambda pid: 'boot:start') == set(lease.container_ids)
    engine.fail_rm = False
    manager.release()
    assert manager.lease is None and engine.records == {}


def test_a_container_that_is_somehow_still_running_is_never_force_removed(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    original = engine.run
    def stop_that_does_not_stop(command, **kwargs):
        if command[1] == 'stop':
            engine.commands.append((command, kwargs))
            return ''
        return original(command, **kwargs)
    manager.runner = stop_that_does_not_stop

    with pytest.raises(p.PrerequisiteError, match='rm refused a running container'):
        manager.release()

    assert manager.lease is lease and engine.records[lease.owned_ids[0]]['State']['Running']


def test_each_acquire_recreates_the_container_and_mounts_the_same_volume_name(setup):
    create, engine, _, _, _ = setup
    names = []
    for _ in range(2):
        manager = create()
        lease = manager.acquire()
        names.append([mount['Name'] for mount in engine.records[lease.owned_ids[0]]['Mounts']
                      if mount['Type'] == 'volume'])
        manager.release()

    assert names[0] == names[1] and names[0][0].endswith('_jaeger-badger')
    assert sum('up' in command for command, _ in engine.commands) == 2
    assert not any(command[1:3] == ('volume', 'rm') for command, _ in engine.commands)


def test_removal_is_batched_by_container_id(setup):
    create, engine, clock, _, _ = setup
    manager = create()
    identifiers = sorted(engine.add('jaeger', running=False) for _ in range(70))

    manager._remove_stopped(identifiers, clock.now() + 60)

    batches = [len(command) - 2 for command, _ in engine.commands if command[1] == 'rm']
    assert batches == [32, 32, 6] and engine.records == {}


def test_snapshots_are_forgotten_only_for_directories_of_this_scope(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    manager.directory.mkdir(parents=True)
    own = manager.directory / f'{scope.id}-assets-abc12345'
    foreign = manager.directory / ('f' * 16 + '-assets-abc12345')
    unrelated = manager.directory / f'{scope.id}-labels.json'
    link = manager.directory / f'{scope.id}-assets-link'
    for directory in (own, foreign):
        directory.mkdir()
    unrelated.write_text('{}')
    link.symlink_to(foreign)

    manager._forget_snapshots()

    assert not own.exists() and foreign.is_dir() and unrelated.exists() and link.is_symlink()


def test_forgetting_snapshots_is_best_effort(setup, monkeypatch):
    create, _, _, scope, _ = setup
    manager = create()
    manager._forget_snapshots()  # no ownership directory yet
    manager.directory.mkdir(parents=True)
    blocked = manager.directory / f'{scope.id}-assets-aaaaaaaa'
    other = manager.directory / f'{scope.id}-assets-bbbbbbbb'
    blocked.mkdir()
    other.mkdir()
    real = shutil.rmtree
    def refuse(path, *args, **kwargs):
        if str(path).endswith('aaaaaaaa'):
            raise PermissionError('private')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(shutil, 'rmtree', refuse)

    manager._forget_snapshots()

    assert blocked.is_dir() and not other.exists()


def test_a_scope_stays_clean_across_failed_and_successful_attempts(setup):
    create, engine, _, _, _ = setup
    failing = create()
    engine.fail_up = True
    with pytest.raises(p.PrerequisiteError, match='up failed'):
        failing.acquire()
    engine.fail_up = False

    succeeding = create()
    succeeding.acquire()
    succeeding.release()

    assert engine.records == {}
    assert not list(succeeding.directory.glob('*-assets-*'))
    assert json.loads((succeeding.directory / f'{succeeding.scope.id}.json').read_text()) == {}


def project_filtering(engine):
    original = engine.run
    def run(command, **kwargs):
        if 'up' in command:  # Compose creates the project's own container, whatever else exists.
            engine.commands.append((command, kwargs))
            for name in command[command.index('--no-recreate') + 1:]:
                engine.add(name)
            return ''
        output = original(command, **kwargs)
        wanted = next((part.rsplit('=', 1)[1] for part in command
                       if part.startswith('label=com.docker.compose.project=')), None)
        if command[1] == 'ps' and wanted is not None:
            return '\n'.join(item for item in output.split()
                             if engine.records[item]['Config']['Labels']['com.docker.compose.project'] == wanted)
        return output
    engine.run = run


def test_acquire_names_its_project_after_the_checkout_and_never_adopts_a_legacy_named_container(setup):
    create, engine, _, scope, _ = setup
    project_filtering(engine)
    manager = create()
    legacy = engine.add('jaeger')
    labels = engine.records[legacy]['Config']['Labels']
    labels['com.docker.compose.project'] = 'sa-' + scope.id
    del labels['api-agent.checkout']

    lease = manager.acquire()

    assert legacy not in lease.container_ids and lease.reused_ids == () and legacy in engine.records
    up = next(command for command, _ in engine.commands if 'up' in command)
    assert up[up.index('--project-name') + 1] == scope.project == f'sa-pack-{scope.id[:8]}'
    created, = lease.owned_ids
    assert engine.records[created]['Config']['Labels']['com.docker.compose.project'] == scope.project
    manager.release()
    assert set(engine.records) == {legacy}
