"""Saved-container inventories must be complete before network removal."""
import json

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError
from test_prerequisites import setup


def _unreferenced_definitions(engine, manager, count):
    name = next(iter(manager.specs))
    identifiers = [engine.add(name, running=False, foreign=True) for _ in range(count)]
    for identifier in identifiers:
        engine.records[identifier]['NetworkSettings'] = {'Networks': {}}
        engine.records[identifier]['HostConfig'] = {'NetworkMode': 'bridge'}
    return identifiers


def _row(identifier):
    return json.dumps({'id': identifier, 'networks': {}, 'mode': 'bridge'})


@pytest.mark.parametrize('corruption', ['duplicate', 'substitute'])
def test_batch_response_must_account_for_each_requested_immutable_id(setup, corruption):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifiers = _unreferenced_definitions(engine, manager, 2)
    original = engine.run

    def inspect(command, **options):
        if command[1:3] == ('inspect', '--format'):
            second = identifiers[0] if corruption == 'duplicate' else 'f' * 64
            return '\n'.join((_row(identifiers[0]), _row(second)))
        return original(command, **options)

    manager.runner = inspect
    with pytest.raises(PrerequisiteError, match='Container network references could not be verified'):
        manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


def test_duplicate_inventory_ids_block_network_removal(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifier = _unreferenced_definitions(engine, manager, 1)[0]
    original = engine.run

    def inventory(command, **options):
        if command[1] == 'ps':
            return identifier + '\n' + identifier
        if command[1:3] == ('inspect', '--format'):
            return '\n'.join(_row(item) for item in command[4:])
        return original(command, **options)

    manager.runner = inventory
    with pytest.raises(PrerequisiteError, match='Container network references could not be verified'):
        manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


def test_many_saved_definitions_fit_network_cleanup_budget(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifiers = _unreferenced_definitions(engine, manager, 560)
    original = engine.run
    inspections = []

    def slow_command(command, **options):
        clock.sleep(0.5)
        if clock.now() > options['deadline']:
            raise PrerequisiteError('Network cleanup deadline expired')
        if command[1:3] == ('inspect', '--format'):
            inspections.append(command[4:])
            return '\n'.join(_row(item) for item in command[4:])
        return original(command, **options)

    manager.runner = slow_command
    deadline = clock.now() + 15
    manager._prune_networks(deadline)

    assert clock.now() <= deadline
    assert [len(batch) for batch in inspections] == [256, 256, 48]
    assert {item for batch in inspections for item in batch} == set(identifiers)
    assert scope.project + '_default' not in engine.networks
    assert ('docker', 'network', 'rm', 'a' * 64) in [command for command, _ in engine.commands]


def test_reference_inspection_template_supports_podman_id_normalization(setup):
    create, engine, clock, _, _ = setup
    manager = create()
    identifier = _unreferenced_definitions(engine, manager, 1)[0]
    original = engine.run

    def inspect(command, **options):
        if command[1:3] == ('inspect', '--format'):
            template = command[3]
            # Podman normalizes the standalone Docker-compatible Id action,
            # while its underlying container record exposes ID.
            assert '{{.Id}}' in template
            assert '{{json .Id}}' not in template
            return (template.replace('{{.Id}}', identifier)
                    .replace('{{json .NetworkSettings.Networks}}', '{}')
                    .replace('{{json .HostConfig.NetworkMode}}', '"bridge"'))
        return original(command, **options)

    manager.runner = inspect
    assert manager._container_network_references(clock.now() + 120, cleanup=True) == {'bridge'}
