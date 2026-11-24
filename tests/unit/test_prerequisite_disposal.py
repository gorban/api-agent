"""Temporary definitions are removed only after the final compatible lease."""
import pytest

from api_agent.runtime.prerequisites import PrerequisiteError
from test_prerequisites import setup  # noqa: F401


def removable_engine(engine):
    original = engine.run

    def run(command, **options):
        if command[1] == 'rm':
            engine.commands.append((command, options))
            for identifier in command[2:]:
                del engine.records[identifier]
            return ''
        return original(command, **options)

    return run


@pytest.mark.parametrize('creator_first', [True, False])
def test_temporary_definition_and_network_are_removed_after_last_lease(setup, creator_first):
    create, engine, _, scope, _ = setup
    creator = create(dispose_on_release=True)
    creator.runner = removable_engine(engine)
    lease = creator.acquire()
    consumer = create(dispose_on_release=True)
    consumer.runner = creator.runner
    consumer.acquire()
    first, last = (creator, consumer) if creator_first else (consumer, creator)

    first.release()
    assert set(engine.records) == set(lease.container_ids)
    assert scope.project + '_default' in engine.networks
    last.release()

    assert not engine.records
    assert scope.project + '_default' not in engine.networks
    assert not any(command[1:3] == ('volume', 'rm') or 'down' in command for command, _ in engine.commands)


def test_temporary_and_persistent_borrowers_cannot_mix(setup):
    create, engine, _, _, _ = setup
    temporary = create(dispose_on_release=True)
    lease = temporary.acquire()
    persistent = create()

    with pytest.raises(PrerequisiteError, match='disposal policy'):
        persistent.acquire()

    assert set(engine.records) == set(lease.container_ids)


def test_failed_removal_keeps_lease_for_retry(setup):
    create, engine, _, scope, _ = setup
    manager = create(dispose_on_release=True)
    remove = removable_engine(engine)
    lease = manager.acquire()

    def fail_once(command, **options):
        if command[1] == 'rm':
            raise PrerequisiteError('removal failed')
        return remove(command, **options)

    manager.runner = fail_once
    with pytest.raises(PrerequisiteError, match='removal failed'):
        manager.release()
    assert manager.lease == lease
    assert scope.project + '_default' in engine.networks

    manager.runner = remove
    manager.release()
    assert not engine.records
    assert scope.project + '_default' not in engine.networks


def test_failed_start_removes_only_new_temporary_definitions(setup):
    create, engine, _, scope, _ = setup
    manager = create(dispose_on_release=True)
    manager.runner = removable_engine(engine)
    engine.fail_up = True

    with pytest.raises(PrerequisiteError, match='up failed'):
        manager.acquire()

    assert not engine.records
    assert scope.project + '_default' not in engine.networks


def test_restarted_existing_definition_is_preserved(setup):
    create, engine, _, _, _ = setup
    manager = create(dispose_on_release=True)
    manager.runner = removable_engine(engine)
    existing = engine.add('jaeger', running=False)

    manager.acquire()
    manager.release()

    assert existing in engine.records
    assert not engine.records[existing]['State']['Running']


@pytest.mark.parametrize('value', [1, None, 'true'])
def test_disposal_policy_requires_boolean(setup, value):
    create, _, _, _, _ = setup
    with pytest.raises(PrerequisiteError, match='must be a boolean'):
        create(dispose_on_release=value)


def test_changed_fingerprint_preserves_definition_and_lease(setup):
    create, engine, _, _, _ = setup
    manager = create(dispose_on_release=True)
    manager.runner = removable_engine(engine)
    lease = manager.acquire()
    identifier, = lease.container_ids
    engine.records[identifier]['Config']['Labels']['api-agent.config'] = 'changed'

    with pytest.raises(PrerequisiteError):
        manager.release()

    assert manager.lease == lease
    assert engine.records[identifier]['State']['Running']


def test_unrelated_persistent_service_preserves_shared_network(setup):
    create, engine, _, scope, _ = setup
    temporary = create(dispose_on_release=True)
    temporary.runner = removable_engine(engine)
    temporary.acquire()
    persistent = create({'S3_USE_LOCALSTACK': True})
    persistent.runner = temporary.runner
    retained = persistent.acquire()

    temporary.release()

    assert set(engine.records) == set(retained.container_ids)
    assert scope.project + '_default' in engine.networks
    persistent.release()
    assert scope.project + '_default' in engine.networks


def test_network_duty_follows_disjoint_temporary_service(setup):
    create, engine, _, scope, _ = setup
    first = create(dispose_on_release=True)
    first.runner = removable_engine(engine)
    first.acquire()
    second = create({'S3_USE_LOCALSTACK': True}, dispose_on_release=True)
    second.runner = first.runner
    second.acquire()

    first.release()
    assert scope.project + '_default' in engine.networks
    second.release()

    assert not engine.records
    assert scope.project + '_default' not in engine.networks


def test_network_cleanup_duty_survives_creator_death_after_removal_failure(setup):
    import json

    create, engine, _, scope, _ = setup
    creator = create(dispose_on_release=True)
    remove = removable_engine(engine)
    creator.runner = remove
    lease = creator.acquire()

    def fail_network(command, **options):
        if command[1:3] == ('network', 'rm'):
            raise PrerequisiteError('network removal failed')
        return remove(command, **options)

    creator.runner = fail_network
    with pytest.raises(PrerequisiteError, match='network removal failed'):
        creator.release()
    assert not engine.records
    journal_path = creator.directory / f'{scope.id}.json'
    assert json.loads(journal_path.read_text())[lease.invocation_id]['disposal_network_id'] == 'a' * 64

    successor = create(dispose_on_release=True)
    successor.identity = lambda pid: 'new process identity'
    successor.runner = remove
    successor.acquire()

    assert ('docker', 'network', 'rm', 'a' * 64) in [command for command, _ in engine.commands]
    assert lease.invocation_id not in json.loads(journal_path.read_text())
    successor.release()


def test_pending_network_duty_is_cleared_when_exact_id_was_already_removed(setup):
    import json
    create, engine, _, scope, _ = setup
    creator = create(dispose_on_release=True)
    remove = removable_engine(engine)
    creator.runner = remove
    creator.acquire()
    engine.fail_up = False
    original = creator.runner

    def removed_then_interrupted(command, **options):
        result = original(command, **options)
        if command[1:3] == ('network', 'rm'):
            raise PrerequisiteError('interrupted after network removal')
        return result

    creator.runner = removed_then_interrupted
    with pytest.raises(PrerequisiteError, match='interrupted'):
        creator.release()
    assert not engine.networks
    successor = create(dispose_on_release=True)
    successor.identity = lambda pid: 'new identity'
    successor.runner = remove
    successor.acquire()
    journal = json.loads((successor.directory / f'{scope.id}.json').read_text())
    assert creator.lease.invocation_id not in journal
    successor.release()


def test_network_with_changed_labels_is_not_acknowledged_as_absent(setup):
    create, engine, clock, scope, _ = setup
    manager = create(dispose_on_release=True)
    manager._prepare_network(clock.now() + 120)
    engine.network_labels[scope.project + '_default']['api-agent.managed'] = 'false'

    removed = manager._prune_networks(clock.now() + 120, only_id='a'*64)

    assert removed == ()
    assert scope.project + '_default' in engine.networks


def test_temporary_acquisition_never_adopts_outsider_created_during_up(setup):
    create, engine, _, _, _ = setup
    manager = create(dispose_on_release=True)
    original = engine.run

    def concurrent_create(command, **options):
        if 'up' in command:
            outsider = engine.add('jaeger')
            engine.records[outsider]['Config']['Labels']['api-agent.acquisition'] = 'b'*32
        return original(command, **options)

    manager.runner = concurrent_create
    with pytest.raises(PrerequisiteError, match='creation'):
        manager.acquire()
    assert len(engine.records) == 1
    assert next(iter(engine.records.values()))['State']['Running']
    assert not any(command[1] in {'stop', 'rm'} for command, _ in engine.commands)


def test_stale_creator_transfers_network_and_container_duty_to_live_borrower(setup):
    import json
    create, engine, _, scope, _ = setup
    creator = create(dispose_on_release=True)
    creator.runner = removable_engine(engine)
    created = creator.acquire()
    borrower = create(dispose_on_release=True)
    borrower.runner = creator.runner
    borrower.acquire()
    path = creator.directory / f'{scope.id}.json'
    journal = json.loads(path.read_text())
    journal[created.invocation_id]['pid'] = 99999999
    path.write_text(json.dumps(journal))
    borrower.identity = lambda pid: None if pid == 99999999 else 'boot:start'

    borrower.release()

    assert not engine.records
    assert scope.project + '_default' not in engine.networks


@pytest.mark.parametrize('value', ['bad', 9, 'g'*32])
def test_acquisition_nonce_is_validated(setup, value):
    create, _, _, _, _ = setup
    with pytest.raises(PrerequisiteError, match='Acquisition nonce'):
        create(dispose_on_release=True, acquisition_nonce=value)


def test_network_creation_requires_immutable_id(setup):
    create, engine, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    original = engine.run
    def invalid_id(command, **options):
        if command[1:3] == ('network', 'create'):
            return 'invalid'
        return original(command, **options)
    manager.runner = invalid_id
    with pytest.raises(PrerequisiteError, match='identifier could not be verified'):
        manager._prepare_network(clock.now() + 120)
    assert not engine.networks


def test_network_absence_requires_valid_global_inventory(setup):
    create, _, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    manager.runner = lambda *args, **kwargs: 'not-an-id'
    with pytest.raises(PrerequisiteError, match='Network absence cannot be verified'):
        manager._absent_network('a'*64, clock.now() + 120, cleanup=True)


def test_cleanup_rejects_changed_creator_nonce_before_stopping(setup):
    create, engine, _, _, _ = setup
    manager = create(dispose_on_release=True)
    lease = manager.acquire()
    identifier, = lease.container_ids
    engine.records[identifier]['Config']['Labels']['api-agent.acquisition'] = 'e'*32
    with pytest.raises(PrerequisiteError, match='creation evidence changed'):
        manager.release()
    assert engine.records[identifier]['State']['Running']
    assert manager.lease == lease


def test_unverified_lease_keeps_temporary_definition_running(setup):
    create, engine, _, scope, _ = setup
    manager = create(dispose_on_release=True)
    lease = manager.acquire()
    path = manager.directory / f'{scope.id}.json'
    path.write_text('{}')
    with pytest.raises(PrerequisiteError, match='lease cannot be verified'):
        manager.release()
    assert set(engine.records) == set(lease.container_ids)
    assert manager.lease == lease


def test_precreate_failure_does_not_claim_network_cleanup(setup):
    create, engine, _, _, _ = setup
    manager = create(dispose_on_release=True)
    original = engine.run
    def failure(command, **options):
        if command[1:3] == ('network', 'create'):
            raise PrerequisiteError('creation refused')
        return original(command, **options)
    manager.runner = failure
    with pytest.raises(PrerequisiteError, match='creation refused'):
        manager.acquire()
    assert not engine.records
    assert not engine.networks
    assert manager._rollback is None


@pytest.mark.parametrize('condition,expected', [
    ('invalid-inventory', 'inventory cannot be verified'),
    ('foreign-scope', 'ownership changed'),
    ('wrong-nonce', 'creation evidence changed'),
    ('running', 'ownership changed'),
])
def test_disposal_refuses_unsafe_container_changes(setup, condition, expected):
    create, engine, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    lease = manager.acquire()
    identifier, = lease.container_ids
    nonce = manager.acquisition_nonce
    engine.records[identifier]['State']['Running'] = condition == 'running'
    if condition == 'wrong-nonce':
        engine.records[identifier]['Config']['Labels']['api-agent.acquisition'] = 'e'*32
    if condition == 'foreign-scope':
        manager._inventory = lambda *args, **kwargs: {}
    if condition == 'invalid-inventory':
        original = manager.runner
        def invalid(command, **options):
            if command[1:3] == ('ps', '-a') and '--filter' not in command:
                return 'bad-id'
            return original(command, **options)
        manager.runner = invalid
    with pytest.raises(PrerequisiteError, match=expected):
        manager._dispose_new((identifier,), {}, clock.now() + 120, nonces={identifier: nonce})
    assert identifier in engine.records
    assert not any(command[1] == 'rm' for command, _ in engine.commands)


def test_already_removed_temporary_definition_is_idempotent(setup):
    create, engine, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    lease = manager.acquire()
    identifier, = lease.container_ids
    del engine.records[identifier]
    manager._dispose_new((identifier,), {}, clock.now() + 120,
                         nonces={identifier: manager.acquisition_nonce})
    assert not any(command[1] == 'rm' for command, _ in engine.commands)


def test_stop_refuses_unselected_service_before_mutation(setup):
    from dataclasses import replace
    create, engine, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    lease = manager.acquire()
    identifier, = lease.container_ids
    current = manager._inventory(clock.now() + 120)
    manager._inventory = lambda *args, **kwargs: {identifier: replace(current[identifier], service='other')}
    with pytest.raises(PrerequisiteError, match='ownership changed'):
        manager._stop_new((identifier,), {}, clock.now() + 120)
    assert engine.records[identifier]['State']['Running']


def test_network_receipt_keeps_remaining_container_duty(setup):
    create, engine, clock, _, _ = setup
    manager = create(dispose_on_release=True)
    manager._prepare_network(clock.now() + 120)
    identifier = '1'*64
    journal = {'duty': {'cleanup_only': True, 'disposal_network_id': 'a'*64,
        'conditional_engine_id': manager.engine_id, 'conditional_cleanup_ids': [identifier]}}
    manager._cleanup_pending_networks(journal, clock.now() + 120)
    assert journal['duty']['conditional_cleanup_ids'] == [identifier]
    assert journal['duty']['disposal_network_id'] is None
    assert not engine.networks


def adopting_pair(setup):
    """A creator that left without handing over its disposal duty, and a later reusing consumer."""
    create, engine, _, _, _ = setup
    creator = create(dispose_on_release=True)
    creator.runner = removable_engine(engine)
    created = creator.acquire()
    creator.release(retain=True)
    consumer = create(dispose_on_release=True)
    consumer.runner = creator.runner
    reused = consumer.acquire()
    assert reused.reused_ids == created.owned_ids and not reused.owned_ids
    return create, engine, consumer, reused


def test_a_verified_registry_chain_lets_the_final_consumer_dispose_of_what_it_reused(setup):
    _, engine, consumer, reused = adopting_pair(setup)

    consumer.release()
    assert set(engine.records) == set(reused.reused_ids)

    consumer.acquire()
    consumer.release(adopted_ids=reused.reused_ids)

    assert not engine.records
    assert not any(command[1:3] == ('volume', 'rm') or 'down' in command for command, _ in engine.commands)


def test_adoption_is_ignored_for_a_retained_release_and_for_ids_that_are_gone(setup):
    _, engine, consumer, reused = adopting_pair(setup)

    consumer.release(retain=True, adopted_ids=reused.reused_ids)
    assert set(engine.records) == set(reused.reused_ids)
    consumer.acquire()
    consumer.release(adopted_ids=('f' * 64,))

    assert set(engine.records) == set(reused.reused_ids)


def test_an_adopted_id_still_leased_by_another_consumer_is_left_for_that_consumer(setup):
    create, engine, first, reused = adopting_pair(setup)
    second = create(dispose_on_release=True)
    second.runner = first.runner
    second.acquire()

    first.release(adopted_ids=reused.reused_ids)
    assert set(engine.records) == set(reused.reused_ids)
    second.release(adopted_ids=reused.reused_ids)

    assert not engine.records
