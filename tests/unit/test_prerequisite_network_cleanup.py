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

    commands = [command for command, _ in engine.commands if command[1:3] == ('network', 'ls')
                and 'label=' + LABEL in command]
    assert commands == [('docker', 'network', 'ls', '--filter', 'label=' + LABEL, '--format', '{{.Name}}')]
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

    assert ('docker', 'network', 'ls', '--filter', 'label=' + LABEL + '=' + scope.id,
            '--format', '{{.Name}}') in [command for command, _ in engine.commands]
    assert not any(command[1] in {'rm', 'volume'} or 'down' in command for command, _ in engine.commands)
    assert engine.records and scope.project + '_default' in engine.networks


def test_retain_does_not_prune_the_current_scope(setup):
    create, engine, _, _, _ = setup
    manager = create()
    manager.acquire()
    engine.commands.clear()

    manager.release(retain=True)

    assert not any(command[1] == 'network' for command, _ in engine.commands)


def test_stopped_retained_container_keeps_its_restart_network(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    original = engine.run
    def endpoint_only_prune(command, **options):
        if command[1:3] == ('network', 'prune') and not any(
                record['State']['Running'] for record in engine.records.values()):
            engine.networks.clear()
        return original(command, **options)
    manager.runner = endpoint_only_prune
    manager.acquire()

    manager.release()

    assert engine.records and not any(record['State']['Running'] for record in engine.records.values())
    assert scope.project + '_default' in engine.networks


@pytest.mark.parametrize('aged,young,referenced,removed', [
    (False, False, False, True), (True, False, False, True),
    (True, True, False, False), (True, False, True, False),
    (False, False, True, False), (False, True, False, True)])
def test_only_unreferenced_owned_networks_are_reclaimed(setup, aged, young, referenced, removed):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    if young:
        from datetime import datetime, timezone
        engine.network_created[scope.project + '_default'] = datetime.now(timezone.utc).isoformat()
    if referenced:
        engine.add(next(iter(manager.specs)), running=False)
    engine.commands.clear()

    manager._prune_networks(clock.now() + 120, aged=aged)

    assert (scope.project + '_default' not in engine.networks) is removed
    assert not any(command[1:3] == ('network', 'prune') for command, _ in engine.commands)
    if removed:
        assert ('docker', 'network', 'rm', 'a' * 64) in [command for command, _ in engine.commands]


@pytest.mark.parametrize('field,value', [
    ('api-agent.managed', 'false'), ('api-agent.scope', 'foreign'),
    ('com.docker.compose.project', 'foreign'), ('com.docker.compose.network', 'other'),
    (LABEL, 'foreign')])
def test_cleanup_preserves_networks_with_unverified_ownership(setup, field, value):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    engine.network_labels[scope.project + '_default'][field] = value
    engine.commands.clear()

    manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


@pytest.mark.parametrize('binding', ['name', 'id', 'created-name', 'created-id'])
def test_saved_foreign_container_definitions_protect_network_without_live_endpoints(setup, binding):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifier = engine.add(next(iter(manager.specs)), running=False, foreign=True)
    network = scope.project + '_default'
    entry = engine.records[identifier]
    if binding.startswith('created'):
        entry['NetworkSettings'] = {'Networks': {}}
        entry['HostConfig'] = {'NetworkMode': network if binding == 'created-name' else 'a' * 64}
    else:
        entry['NetworkSettings'] = {'Networks': {network: {'NetworkID': ''}} if binding == 'name'
            else {'other-alias': {'NetworkID': 'a' * 64}}}
    engine.commands.clear()

    manager._prune_networks(clock.now() + 120)

    assert network in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)
    assert all(not any(value.startswith('network=') for value in command)
               for command, _ in engine.commands if command[1] == 'ps')


@pytest.mark.parametrize('response', ['', 'not-json', 'null', '[]', '{}',
    '{"networks":[],"mode":"bridge"}', '{"networks":{},"mode":false}',
    '{"networks":{"saved":[]},"mode":"bridge"}',
    '{"networks":{"saved":{"NetworkID":null}},"mode":"bridge"}'])
def test_unverifiable_saved_container_networks_block_cleanup(setup, response):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    engine.add(next(iter(manager.specs)), running=False)
    original = engine.run
    def invalid_inspection(command, **options):
        if command[1:3] == ('inspect', '--format'):
            return response
        return original(command, **options)
    manager.runner = invalid_inspection

    with pytest.raises(PrerequisiteError, match='Container network references could not be verified'):
        manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks


def test_container_network_inspections_are_batched(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    for _ in range(35):
        engine.add(next(iter(manager.specs)), running=False)

    references = manager._container_network_references(clock.now() + 120, cleanup=True)

    assert scope.project + '_default' in references
    inspections = [command for command, _ in engine.commands if command[1:3] == ('inspect', '--format')]
    assert [len(command[4:]) for command in inspections] == [32, 3]


def test_old_network_candidates_are_inspected_in_batches(setup):
    from datetime import datetime, timezone
    create, _, clock, _, _ = setup
    manager, inspections = create(), []
    names = [f'sa-{index:016x}_default' for index in range(35)]
    def inventory(command, **options):
        if command[1:3] == ('network', 'ls'):
            return '\n'.join(names)
        if command[1] == 'ps':
            return ''
        assert command[1:3] == ('network', 'inspect')
        inspections.append(command[3:])
        return json.dumps([{'Name': name, 'Id': 'a' * 64,
            'Created': datetime.now(timezone.utc).isoformat(), 'Labels': {
                LABEL: name[3:19], 'api-agent.managed': 'true', 'api-agent.scope': name[3:19],
                'com.docker.compose.project': name.removesuffix('_default'),
                'com.docker.compose.network': 'default'}} for name in command[3:]])
    manager.runner = inventory

    manager._prune_networks(clock.now() + 120, aged=True)

    assert [len(batch) for batch in inspections] == [32, 3]


def test_old_unreferenced_networks_are_removed_in_pinned_batches(setup):
    create, _, clock, _, _ = setup
    manager, removals = create(), []
    names = [f'sa-{index:016x}_default' for index in range(35)]
    identifiers = {name: f'{index + 1:064x}' for index, name in enumerate(names)}
    def inventory(command, **options):
        if command[1:3] == ('network', 'ls'):
            return '\n'.join(names)
        if command[1] == 'ps':
            return ''
        if command[1:3] == ('network', 'rm'):
            removals.append(command[3:])
            return ''
        assert command[1:3] == ('network', 'inspect')
        return json.dumps([{'Name': name, 'Id': identifiers[name],
            'Created': '2000-01-01T00:00:00Z', 'Labels': {
                LABEL: name[3:19], 'api-agent.managed': 'true', 'api-agent.scope': name[3:19],
                'com.docker.compose.project': name.removesuffix('_default'),
                'com.docker.compose.network': 'default'}} for name in command[3:]])
    manager.runner = inventory

    manager._prune_networks(clock.now() + 120, aged=True)

    assert [len(batch) for batch in removals] == [32, 3]
    assert {identifier for batch in removals for identifier in batch} == set(identifiers.values())


def test_failed_network_cleanup_preserves_lease_for_retry(setup):
    create, engine, _, _, _ = setup
    manager = create()
    lease = manager.acquire()
    original = engine.run

    def fail_cleanup(command, **options):
        if command[1:3] == ('network', 'ls'):
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


@pytest.mark.parametrize('inspection,aged', [('null', False), ('[]', False), ('[{"Name": "other"}]', False),
                                             ('invalid-id', False), ('invalid-created', True)])
def test_unverifiable_network_inventory_preserves_networks(setup, inspection, aged):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    original = engine.run
    def inventory(command, **options):
        response = original(command, **options)
        if command[1:3] != ('network', 'inspect'):
            return response
        if inspection not in {'invalid-id', 'invalid-created'}:
            return inspection
        records = json.loads(response)
        records[0]['Id' if inspection == 'invalid-id' else 'Created'] = 'not-valid'
        return json.dumps(records)
    manager.runner = inventory

    with pytest.raises(PrerequisiteError, match='Network cleanup inventory could not be verified'):
        manager._prune_networks(clock.now() + 120, aged=aged)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


def test_unverifiable_saved_container_identifiers_block_cleanup(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    original = engine.run
    def listing(command, **options):
        if command[1:] == ('ps', '-a', '--no-trunc', '--format', '{{.ID}}'):
            return 'not-a-container-id\n'
        return original(command, **options)
    manager.runner = listing

    with pytest.raises(PrerequisiteError, match='Container network references could not be verified'):
        manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks


def test_saved_definition_without_network_details_still_protects_by_name(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    network = scope.project + '_default'
    entry = engine.records[engine.add(next(iter(manager.specs)), running=False, foreign=True)]
    entry['NetworkSettings'] = {'Networks': {network: None, 'other': {'NetworkID': 'b' * 64}}}
    entry['HostConfig'] = {'NetworkMode': ''}
    engine.commands.clear()

    manager._prune_networks(clock.now() + 120)

    assert network in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


@pytest.mark.parametrize('aged', [False, True])
def test_referenced_names_are_filtered_before_network_inspection(setup, aged):
    create, _, clock, scope, _ = setup
    manager = create()
    names = [f'sa-{index:016x}_default' for index in range(35)] if aged else [scope.project + '_default']
    # Current-scope cleanup can only remove its own scope, so exercise the mixed
    # inventory under aged cleanup and the same ordering for current scope.
    if aged:
        referenced_names, id_only, orphan = names[:33], names[33], names[34]
    else:
        referenced_names, id_only, orphan = [], None, names[0]
    identifiers = {name: f'{index + 1:064x}' for index, name in enumerate(names)}
    references = set(referenced_names)
    if id_only:
        references.add(identifiers[id_only])
    calls = []

    def inventory(command, **options):
        calls.append(command)
        if command[1:3] == ('network', 'ls'):
            return '\n'.join(names)
        if command[1] == 'ps':
            return 'f' * 64
        if command[1:3] == ('inspect', '--format'):
            networks = {name: {'NetworkID': identifiers[name]} for name in referenced_names}
            if id_only:
                networks['alias'] = {'NetworkID': identifiers[id_only]}
            return json.dumps({'networks': networks, 'mode': ''})
        if command[1:3] == ('network', 'inspect'):
            return json.dumps([{'Name': name, 'Id': identifiers[name],
                'Created': '2000-01-01T00:00:00Z', 'Labels': {
                    LABEL: name[3:19], 'api-agent.managed': 'true', 'api-agent.scope': name[3:19],
                    'com.docker.compose.project': name.removesuffix('_default'),
                    'com.docker.compose.network': 'default'}} for name in command[3:]])
        assert command[1:3] == ('network', 'rm')
        return ''

    manager.runner = inventory
    manager._prune_networks(clock.now() + 120, aged=aged)

    inspections = [command for command in calls if command[1:3] == ('network', 'inspect')]
    assert [name for command in inspections for name in command[3:]] == [name for name in names if name not in references]
    assert len([command for command in calls if command[1] == 'ps']) == 1
    assert next(i for i, command in enumerate(calls) if command[1] == 'ps') < calls.index(inspections[0])
    assert [command[3:] for command in calls if command[1:3] == ('network', 'rm')] == [(identifiers[orphan],)]


def test_all_name_referenced_networks_skip_inspection(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    engine.add(next(iter(manager.specs)), running=False)
    engine.commands.clear()

    manager._prune_networks(clock.now() + 120, aged=True)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] in {('network', 'inspect'), ('network', 'rm')}
                   for command, _ in engine.commands)


@pytest.mark.parametrize('listing', ['', 'unmanaged-network'])
def test_empty_filtered_network_inventory_skips_reference_scan(setup, listing):
    create, _, clock, _, _ = setup
    manager = create()
    calls = []
    def inventory(command, **options):
        calls.append(command)
        assert command[1:3] == ('network', 'ls')
        return listing
    manager.runner = inventory

    manager._prune_networks(clock.now() + 120, aged=True)

    assert len(calls) == 1
