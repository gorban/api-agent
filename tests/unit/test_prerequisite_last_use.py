"""Cleanup ownership follows the last lease across independent managers."""

from dataclasses import replace

import pytest

import api_agent.runtime.prerequisites as prerequisites
from test_prerequisites import setup  # noqa: F401
from test_infrastructure_lifecycle import synthetic  # noqa: F401


def stopped_ids(engine):
    return [identifier for command, _ in engine.commands if command[1] == 'stop'
            for identifier in command[4:]]


@pytest.mark.parametrize('creator_releases_first', [True, False])
def test_only_last_normal_manager_stops_shared_created_service(setup, creator_releases_first):
    create, engine, _, _, _ = setup
    creator = create()
    created = creator.acquire()
    consumer = create()
    reused = consumer.acquire()
    assert creator is not consumer
    assert creator.directory == consumer.directory
    assert reused.reused_ids == created.owned_ids
    assert reused.owned_ids == ()
    first, last = (creator, consumer) if creator_releases_first else (consumer, creator)

    assert first.release() == ()
    assert stopped_ids(engine) == []
    assert all(engine.records[item]['State']['Running'] for item in created.owned_ids)
    assert last.release() == created.owned_ids
    assert last.release() == ()
    assert first.release() == ()
    assert stopped_ids(engine) == list(created.owned_ids)


def test_last_consumer_can_retry_cleanup_after_creator_has_left(setup):
    create, engine, _, _, _ = setup
    creator = create()
    created = creator.acquire()
    consumer = create()
    consumer.acquire()
    creator.release()
    engine.fail_stop = True

    with pytest.raises(prerequisites.PrerequisiteError, match='stop failed'):
        consumer.release()
    assert consumer.lease is not None
    assert all(engine.records[item]['State']['Running'] for item in created.owned_ids)
    engine.fail_stop = False
    assert consumer.release() == created.owned_ids
    assert consumer.release() == ()
    stop_attempts = [command for command, _ in engine.commands if command[1] == 'stop']
    assert len(stop_attempts) == 2  # One failed attempt and one successful retry.


def test_last_consumer_cleanup_ignores_cancelled_work(setup):
    create, engine, _, _, _ = setup
    creator = create()
    created = creator.acquire()
    consumer = create()
    consumer.acquire()
    creator.release()
    engine.cancel = True

    assert consumer.release() == created.owned_ids
    assert stopped_ids(engine) == list(created.owned_ids)


def test_explicit_retain_on_last_consumer_keeps_shared_service(setup):
    create, engine, _, _, _ = setup
    creator = create()
    created = creator.acquire()
    consumer = create()
    consumer.acquire()
    creator.release()

    assert consumer.release(retain=True) == ()
    assert stopped_ids(engine) == []
    assert all(engine.records[item]['State']['Running'] for item in created.owned_ids)


def test_passive_unselected_neighbor_never_becomes_last_use_cleanup_owned(setup):
    create, engine, _, _, _ = setup
    create({'ALLURE_S3_USE_LOCALSTACK': True})
    neighbor = engine.add('localstack')
    creator = create()
    created = creator.acquire()
    consumer = create()
    reused = consumer.acquire()
    assert neighbor not in created.container_ids + reused.container_ids

    assert creator.release() == ()
    assert consumer.release() == created.owned_ids
    assert stopped_ids(engine) == list(created.owned_ids)
    assert engine.records[neighbor]['State']['Running']


@pytest.mark.parametrize('creator_releases_first', [True, False])
def test_declared_api_and_backing_stop_only_after_last_normal_manager(synthetic, creator_releases_first):
    creator, engine, clock, _, _ = synthetic
    created = creator.acquire()
    consumer = prerequisites.PrerequisiteManager(
        creator.plan, creator.scope, creator.directory,
        command_runner=engine.run, engine_environment=creator.engine_environment,
        now=clock.now, sleep=clock.sleep, identity=creator.identity)
    engine.manager = consumer
    reused = consumer.acquire()
    assert len(created.owned_ids) == 2
    assert set(reused.reused_ids) == set(created.owned_ids)
    assert not reused.owned_ids
    first, last = (creator, consumer) if creator_releases_first else (consumer, creator)

    assert first.release() == ()
    assert not any(command[1] == 'stop' for command, _ in engine.calls)
    assert all(engine.records[item]['State']['Running'] for item in created.owned_ids)
    assert set(last.release()) == set(created.owned_ids)
    assert last.release() == ()
    stops = [item for command, _ in engine.calls if command[1] == 'stop' for item in command[4:]]
    assert sorted(stops) == sorted(created.owned_ids)


def test_only_shared_backing_waits_for_the_narrower_consumers_last_lease(synthetic):
    creator, engine, clock, _, _ = synthetic
    creator.acquire()
    backing = next(item for item in creator.plan.conditional if not item.dependencies)
    identifiers = {row['Config']['Labels']['com.docker.compose.service']: identifier
                   for identifier, row in engine.records.items()}
    backing_id = identifiers[backing.compose_name]
    api_id = next(identifier for name, identifier in identifiers.items()
                  if name != backing.compose_name)
    selected = replace(creator.plan, services=(backing.compose_name,), conditional=(backing,))
    consumer = prerequisites.PrerequisiteManager(
        selected, creator.scope, creator.directory,
        command_runner=engine.run, engine_environment=creator.engine_environment,
        now=clock.now, sleep=clock.sleep, identity=creator.identity)
    engine.manager = consumer
    reused = consumer.acquire()
    assert reused.reused_ids == (backing_id,)
    assert not reused.owned_ids

    assert creator.release() == (api_id,)
    assert not engine.records[api_id]['State']['Running']
    assert engine.records[backing_id]['State']['Running']
    assert consumer.release() == (backing_id,)
    assert not engine.records[backing_id]['State']['Running']
    stops = [item for command, _ in engine.calls if command[1] == 'stop'
             for item in command[4:]]
    assert stops == [api_id, backing_id]
