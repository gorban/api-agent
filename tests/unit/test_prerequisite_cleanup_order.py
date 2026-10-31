"""Whole-stack cleanup budgets and consumer-before-store stop ordering."""
import math

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError, _check
from test_prerequisites import setup
from test_setup_backend import fake_host


def serial_engine(engine, clock):
    original = engine.run
    def run(command, **options):
        if command[1] != 'stop':
            return original(command, **options)
        engine.commands.append((command, options))
        for identifier in command[4:]:
            clock.value += 5
            _check(options['deadline'], lambda: False, clock.now)
            engine.records[identifier]['State']['Running'] = False
        return ''
    return run


def test_langfuse_stop_budget_covers_serial_engine_and_preserves_dependency_order(setup):
    create, engine, clock, _, _ = setup
    manager = create({'TRACE_MODE': 'LangFuse'}, cleanup_timeout_seconds=60)
    manager.runner = serial_engine(engine, clock)
    lease = manager.acquire()
    started = clock.now()

    stopped = manager.release()

    assert set(stopped) == set(lease.owned_ids)
    assert clock.now() - started == 5 * len(lease.owned_ids) > 15
    stops = [command for command, _ in engine.commands if command[1] == 'stop']
    positions = {engine.records[identifier]['Config']['Labels']['com.docker.compose.service']: index
                 for index, command in enumerate(stops) for identifier in command[4:]}
    for service, spec in manager.specs.items():
        if service in positions:
            for dependency in spec.get('depends_on', {}):
                assert positions[service] < positions[dependency]
    assert manager.lease is None
    assert all(not record['State']['Running'] for record in engine.records.values())
    assert all('down' not in command and 'rm' not in command for command, _ in engine.commands)


def test_short_parent_deadline_retains_lease_and_retry_skips_stopped_containers(setup):
    create, engine, clock, _, _ = setup
    manager = create({'TRACE_MODE': 'LangFuse'}, cleanup_timeout_seconds=60)
    manager.runner = serial_engine(engine, clock)
    lease = manager.acquire()
    with pytest.raises(PrerequisiteError, match='deadline exceeded'):
        manager.release(_deadline=clock.now() + 12)
    assert manager.lease is lease
    stopped = {identifier for identifier, record in engine.records.items() if not record['State']['Running']}
    assert stopped
    engine.commands.clear()

    manager.release()

    retried = {identifier for command, _ in engine.commands if command[1] == 'stop' for identifier in command[4:]}
    assert not retried.intersection(stopped)
    assert manager.lease is None


def test_dependency_cycle_is_rejected_before_any_stop(setup):
    create, engine, _, _, _ = setup
    manager = create({'TRACE_MODE': 'LangFuse'})
    lease = manager.acquire()
    manager.specs['postgres'] = {**manager.specs['postgres'], 'depends_on': {'langfuse-web': {}}}
    engine.commands.clear()

    with pytest.raises(PrerequisiteError, match='dependency cycle'):
        manager.release()

    assert manager.lease is lease
    assert not any(command[1] == 'stop' for command, _ in engine.commands)


def test_retained_consumer_protects_shared_collector_and_last_user_stops_it(setup):
    create, engine, _, _, _ = setup
    first = create({'TRACE_MODE': 'LangFuse'}, cleanup_timeout_seconds=60)
    original = first.acquire()
    second = create({'TRACE_MODE': 'LangFuse'}, cleanup_timeout_seconds=60)
    borrowed = second.acquire()
    assert set(borrowed.reused_ids) == set(original.owned_ids)
    engine.commands.clear()

    first.release()
    assert not any(command[1] == 'stop' for command, _ in engine.commands)
    second.release()

    assert all(not record['State']['Running'] for record in engine.records.values())


@pytest.mark.parametrize('value', [False, 0, -1, 301, 'bad', math.nan, math.inf])
def test_cleanup_budget_rejects_invalid_values(setup, value):
    create, _, _, _, _ = setup
    with pytest.raises(PrerequisiteError, match='Cleanup deadline'):
        create(cleanup_timeout_seconds=value)


@pytest.mark.parametrize('operation', ['close', 'finish_run', 'prepare_publication'])
def test_public_backend_cleanup_retains_underlying_failure_cause(fake_host, operation):
    from api_agent.providers.control import CleanupPendingError
    from test_setup_backend import checkpoint
    state = fake_host
    backend = state.backend
    backend.debug = operation == 'finish_run'
    backend.start()
    checkpoint(backend)
    state.server_failure = state.provider_failure = True

    with pytest.raises(CleanupPendingError) as caught:
        getattr(backend, operation)()

    causes = []
    current = caught.value.__cause__
    while current is not None:
        causes.append(current)
        current = current.__cause__
    assert any(isinstance(error, RuntimeError) and str(error) == 'synthetic private server' for error in causes)
    assert backend.pending and not backend.closed
