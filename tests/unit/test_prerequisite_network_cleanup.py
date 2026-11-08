"""Empty managed networks must not exhaust the engine's address pools."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError
from api_agent.runtime.ownership import StackScope
from test_prerequisites import setup


LABEL = 'api-agent.network-cleanup-v1'


def test_new_network_is_marked_without_changing_the_compose_declaration(setup):
    create, engine, _, scope, _ = setup
    manager = create()

    manager.acquire()

    command = next(command for command, _ in engine.commands if command[1:3] == ('network', 'create'))
    assert command == ('docker', 'network', 'create',
        '--label', LABEL + '=' + scope.id,
        '--label', 'api-agent.managed=true', '--label', 'api-agent.scope=' + scope.id,
        '--label', 'com.docker.compose.project=' + scope.project,
        '--label', 'com.docker.compose.network=default', scope.project + '_default')
    assert 'networks' not in json.loads(manager.plan.assets['compose.json'])


def test_existing_network_is_preserved_without_relabelling_or_recreation(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    engine.networks.add(scope.project + '_default')

    manager.acquire()

    assert not any(command[1:3] == ('network', 'create') for command, _ in engine.commands)


def test_unexpected_network_inventory_is_rejected_before_creation(setup):
    create, _, clock, _, _ = setup
    manager = create()
    manager.runner = lambda *args, **kwargs: 'foreign-network'

    with pytest.raises(PrerequisiteError, match='Unexpected prerequisite network inventory'):
        manager._prepare_network(clock.now() + 120)


@pytest.mark.parametrize('inspection', ['not-json', 'null', '[]', '[{}, {}]', '[{}]',
    '[{"Name": "foreign", "Labels": {}}]',
    '[{"Name": "EXPECTED", "Labels": null}]',
    '[{"Name": "EXPECTED", "Labels": {"com.docker.compose.project": "foreign"}}]',
    '[{"Name": "EXPECTED", "Labels": {"com.docker.compose.project": "PROJECT", "com.docker.compose.network": "foreign"}}]'])
def test_unverifiable_existing_network_is_rejected_without_mutation(setup, inspection):
    create, engine, clock, scope, _ = setup
    manager = create()
    commands = []

    def inspect(command, **options):
        commands.append(command)
        return (scope.project + '_default' if command[2] == 'ls' else
                inspection.replace('EXPECTED', scope.project + '_default').replace('PROJECT', scope.project))

    manager.runner = inspect
    with pytest.raises(PrerequisiteError, match='network ownership could not be verified'):
        manager._prepare_network(clock.now() + 120)
    assert [command[2] for command in commands] == ['ls', 'inspect']
    assert not engine.records


def test_podman_existing_network_metadata_is_accepted_without_recreation(setup):
    create, _, clock, scope, _ = setup
    manager = create()
    name = scope.project + '_default'

    def inspect(command, **options):
        if command[2] == 'ls':
            return name
        assert command[2] == 'inspect'
        return json.dumps([{'name': name, 'labels': {
            'com.docker.compose.project': scope.project,
            'com.docker.compose.network': 'default'}}])

    manager.runner = inspect
    manager._prepare_network(clock.now() + 120)


def test_acquisition_reclaims_only_aged_explicitly_managed_networks(setup):
    create, engine, _, _, _ = setup
    manager = create()

    manager.acquire()

    commands = [command for command, _ in engine.commands if command[1:3] == ('network', 'prune')]
    assert commands == [('docker', 'network', 'prune', '--force', '--filter', 'label=' + LABEL,
                         '--filter', 'until=24h')]
    assert engine.commands.index(next(item for item in engine.commands if item[0] == commands[0])) < next(
        index for index, (command, _) in enumerate(engine.commands) if 'up' in command)


@pytest.mark.parametrize('fail_start', [False, True])
def test_release_and_failed_start_prune_only_current_scope_without_removing_containers_or_volumes(setup, fail_start):
    create, engine, _, scope, _ = setup
    manager = create()
    engine.fail_up = fail_start

    if fail_start:
        with pytest.raises(PrerequisiteError, match='up failed'):
            manager.acquire()
    else:
        manager.acquire()
        engine.commands.clear()
        manager.release()

    assert ('docker', 'network', 'prune', '--force', '--filter', 'label=' + LABEL + '=' + scope.id) in [command for command, _ in engine.commands]
    assert not any(command[1] in {'rm', 'volume'} or 'down' in command for command, _ in engine.commands)
    assert engine.records


def test_retain_does_not_prune_the_current_scope(setup):
    create, engine, _, _, _ = setup
    manager = create()
    manager.acquire()
    engine.commands.clear()

    manager.release(retain=True)

    assert not any(command[1] == 'network' for command, _ in engine.commands)


def test_failed_network_cleanup_preserves_lease_for_retry(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    original = engine.run

    def fail_cleanup(command, **options):
        if command[1:3] == ('network', 'prune'):
            raise PrerequisiteError('network cleanup failed')
        return original(command, **options)

    manager.runner = fail_cleanup
    with pytest.raises(PrerequisiteError, match='network cleanup failed'):
        manager.release()
    assert manager.lease is lease

    manager.runner = original
    manager.release()
    assert manager.lease is None


@pytest.mark.parametrize('different_environment', [False, True])
def test_other_scope_pruning_waits_for_network_creation_on_same_engine(setup, monkeypatch, different_environment):
    create, _, clock, _, tmp_path = setup
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    first = create()
    second = create(engine_environment={'PATH': '/other/client'} if different_environment else None)
    assert (first.engine_id != second.engine_id) is different_environment
    second.scope = StackScope('f' * 16)
    creating, finish, pruning = threading.Event(), threading.Event(), threading.Event()

    def create_network(command, **options):
        if command[1] == 'compose':
            creating.set()
            assert finish.wait(5)
        return ''

    def prune_network(command, **options):
        pruning.set()
        return ''

    first.runner, second.runner = create_network, prune_network
    with ThreadPoolExecutor(max_workers=2) as pool:
        startup = pool.submit(first._command, ('docker', 'compose', 'up', '-d'), clock.now() + 120)
        assert creating.wait(5)
        cleanup = pool.submit(second._prune_networks, clock.now() + 120, aged=True)
        try:
            assert not pruning.wait(.1)
        finally:
            finish.set()
        startup.result(timeout=5)
        cleanup.result(timeout=5)
    assert pruning.is_set()
