"""Large saved-container inventories retain complete network safety checks."""
import json

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError
from test_prerequisites import setup


def saved_definitions(engine, manager, count=560):
    service = next(iter(manager.specs))
    identifiers = [engine.add(service, running=False, foreign=True) for _ in range(count)]
    for identifier in identifiers:
        engine.records[identifier]['NetworkSettings'] = {'Networks': {}}
        engine.records[identifier]['HostConfig'] = {'NetworkMode': 'bridge'}
    return identifiers


def inspection_batches(engine):
    return [command[4:] for command, _ in engine.commands if command[1:3] == ('inspect', '--format')]


def test_large_inventory_is_fully_inspected_in_bounded_batches_before_removal(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifiers = saved_definitions(engine, manager)

    manager._prune_networks(clock.now() + 120)

    batches = inspection_batches(engine)
    assert [len(batch) for batch in batches] == [256, 256, 48]
    assert [identifier for batch in batches for identifier in batch] == identifiers
    assert scope.project + '_default' not in engine.networks


def test_reference_in_final_batch_preserves_scoped_network(setup):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifiers = saved_definitions(engine, manager)
    network = scope.project + '_default'
    engine.records[identifiers[-1]]['NetworkSettings'] = {'Networks': {network: {'NetworkID': 'a' * 64}}}

    manager._prune_networks(clock.now() + 120)

    assert [len(batch) for batch in inspection_batches(engine)] == [256, 256, 48]
    assert network in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)


@pytest.mark.parametrize('corruption', ['missing', 'duplicate', 'substitute'])
def test_invalid_final_inspection_batch_preserves_network(setup, corruption):
    create, engine, clock, scope, _ = setup
    manager = create()
    manager._prepare_network(clock.now() + 120)
    identifiers = saved_definitions(engine, manager)
    original = engine.run

    def malformed(command, **options):
        result = original(command, **options)
        if command[1:3] == ('inspect', '--format') and identifiers[-1] in command:
            rows = result.splitlines()
            if corruption == 'missing':
                rows.pop()
            elif corruption == 'duplicate':
                rows[-1] = rows[0]
            else:
                row = json.loads(rows[-1])
                row['id'] = 'f' * 64
                rows[-1] = json.dumps(row)
            return '\n'.join(rows)
        return result

    manager.runner = malformed
    with pytest.raises(PrerequisiteError, match='Container network references could not be verified'):
        manager._prune_networks(clock.now() + 120)

    assert scope.project + '_default' in engine.networks
    assert not any(command[1:3] == ('network', 'rm') for command, _ in engine.commands)
