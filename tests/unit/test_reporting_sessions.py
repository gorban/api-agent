"""Session consumers share the original journal; no real engine is contacted."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from threading import Event

import pytest

from api_agent.runtime import prerequisites as p
from test_reporting_registry import storage, tmp_path, mutation_commands, binding


def stopped(storage, identifier):
    return not storage.engine.records[identifier]['State']['Running']


def test_last_reusing_consumer_stops_registered_id(storage):
    session, operation = storage.create(), storage.create()
    identifier, = session.acquire().container_ids
    assert operation.acquire().reused_ids == (identifier,)
    session.release()
    assert not stopped(storage, identifier)
    operation.release()
    assert stopped(storage, identifier)
    assert binding(storage)[1]['ids'] == [identifier]
    assert all(command[1] not in {'rm', 'down', 'volume'}
               for command in mutation_commands(storage.engine))


def test_restart_preserves_container_and_original_captured_assets(storage):
    first = storage.create()
    identifier, = first.acquire().container_ids
    original = json.loads(json.dumps(storage.engine.records[identifier]))
    first.release()
    before = len(storage.engine.commands)
    second = storage.create()
    assert second.acquire().owned_ids == (identifier,)
    commands = [command for command, _ in storage.engine.commands[before:]]
    assert ('docker', 'start', identifier) in commands
    assert not any('up' in command or command[1] == 'exec' for command in commands)
    assert storage.engine.records[identifier] == original
    second.release()


@pytest.mark.parametrize('change', ['image', 'fingerprint', 'volume', 'assets', 'registry', 'replacement'])
def test_final_release_refuses_changed_ownership_and_keeps_retry_lease(storage, change):
    owner = storage.create()
    lease = owner.acquire()
    record = storage.engine.records[lease.container_ids[0]]
    if change == 'image': record['Image'] = 'sha256:' + 'f' * 64
    if change == 'fingerprint': record['Config']['Labels'][p.CONFIG_LABEL] = 'changed'
    if change == 'volume': record['Mounts'][1]['Name'] = 'other-data'
    if change == 'assets':
        path = Path(record['Mounts'][0]['Source']); path.write_bytes(b'changed')
    if change == 'registry':
        path, value = binding(storage); value['assets'] = {}; path.write_text(json.dumps(value))
    if change == 'replacement': storage.engine.records.clear()
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError): owner.release()
    assert owner.lease is lease
    assert mutation_commands(storage.engine) == before


def test_stop_failure_keeps_exact_session_for_retry(storage):
    session = storage.create()
    lease = session.acquire()
    storage.engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError, match='stop failed'): session.release()
    assert session.lease is lease
    journal = session.manager.directory / (session.manager.scope.id + '.json')
    assert lease.invocation_id in json.loads(journal.read_text())
    storage.engine.fail_stop = False
    session.release()
    assert session.lease is None and json.loads(journal.read_text()) == {}
    assert stopped(storage, lease.container_ids[0])


def test_join_waits_until_final_stop_then_restarts_same_id(storage):
    first, joining = storage.create(), storage.create()
    identifier, = first.acquire().container_ids
    stopping, permit_stop, joined = Event(), Event(), Event()
    original = first.manager.runner
    def runner(command, **kwargs):
        if command[1] == 'stop':
            stopping.set()
            assert permit_stop.wait(10)
        return original(command, **kwargs)
    first.manager.runner = runner
    def join():
        value = joining.acquire()
        joined.set()
        return value
    with ThreadPoolExecutor(2) as executor:
        released = executor.submit(first.release)
        assert stopping.wait(10)
        acquired = executor.submit(join)
        assert not joined.wait(.15)
        permit_stop.set()
        released.result(timeout=10)
        assert acquired.result(timeout=10).container_ids == (identifier,)
    assert not stopped(storage, identifier)
    joining.release()


CHILD = '''
import json, os, sys, time
from pathlib import Path
from api_agent.runtime import prerequisites as p
from api_agent.runtime.ownership import StackScope
path, scope_id, identifier = sys.argv[1:]
directory, scope = Path(path), StackScope(scope_id)
with p._locked(directory, scope, deadline=time.monotonic()+10,
               cancelled=lambda:False, now=time.monotonic, sleep=time.sleep):
    journal=p._read_journal(directory, scope, p.process_identity)
    journal['controlled-child']={'pid':os.getpid(), 'identity':p.process_identity(os.getpid()), 'ids':[identifier]}
    p._write_journal(directory, scope, journal)
print('joined', flush=True)
sys.stdin.read()
'''


def test_real_process_death_prunes_session_without_exit_handler(storage):
    owner = storage.create()
    identifier, = owner.acquire().container_ids
    manager = owner.manager
    with subprocess.Popen([sys.executable, '-c', CHILD, str(manager.directory), manager.scope.id, identifier],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                          env=dict(os.environ)) as child:
        try:
            assert select.select([child.stdout], [], [], 10)[0]
            assert child.stdout.readline().strip() == 'joined'
            owner.release()
            assert not stopped(storage, identifier)
            child.kill(); child.wait(timeout=10)
            recovered = storage.create()
            assert recovered.acquire().container_ids == (identifier,)
            recovered.release()
            assert stopped(storage, identifier)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=10)


def test_pid_reuse_identity_does_not_protect_stale_session(storage):
    owner = storage.create()
    identifier, = owner.acquire().container_ids
    manager = owner.manager
    journal = manager.directory / (manager.scope.id + '.json')
    records = json.loads(journal.read_text())
    records['stale-session'] = {'pid': os.getpid(), 'identity': 'previous-process-birth', 'ids': [identifier]}
    p._write_journal(manager.directory, manager.scope, records)
    owner.release()
    assert stopped(storage, identifier) and json.loads(journal.read_text()) == {}


def test_stopped_registration_with_live_operation_lease_refuses_restart(storage):
    owner = storage.create()
    identifier, = owner.acquire().container_ids
    storage.engine.records[identifier]['State']['Running'] = False
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='active invocation lease'):
        storage.create().acquire()
    assert owner.lease is not None and mutation_commands(storage.engine) == before
    owner.release()


def test_canceled_session_cleanup_verifies_images_and_storage_with_independent_cancellation(storage):
    canceled = Event()
    owner = storage.create(cancelled=canceled.is_set)
    owner.acquire()
    original = owner.manager.runner
    def runner(command, **kwargs):
        assert not kwargs['cancelled'](), 'cleanup inherited the canceled operation token'
        return original(command, **kwargs)
    owner.manager.runner = runner
    canceled.set()
    owner.release()
    assert owner.lease is None


def test_uncertain_process_identity_refuses_final_stop_and_retains_session(storage):
    owner = storage.create()
    lease = owner.acquire()
    def uncertain(pid):
        raise p.PrerequisiteError('Cannot verify lease process identity.')
    owner.manager.identity = uncertain
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='Cannot verify'):
        owner.release()
    assert owner.lease is lease and mutation_commands(storage.engine) == before


def test_missing_registration_refuses_final_release_and_retains_session(storage):
    owner = storage.create()
    lease = owner.acquire()
    binding(storage)[0].unlink()
    with pytest.raises(p.PrerequisiteError, match='binding'):
        owner.release()
    assert owner.lease is lease


def test_additional_unregistered_service_in_registered_scope_refuses_final_release(storage):
    from copy import deepcopy
    owner = storage.create()
    lease = owner.acquire()
    extra = deepcopy(storage.engine.records[lease.container_ids[0]])
    extra['Id'] = 'f' * 64
    extra['Config']['Labels']['com.docker.compose.service'] = 'unrelated'
    storage.engine.records[extra['Id']] = extra
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='container changed'):
        owner.release()
    assert owner.lease is lease and mutation_commands(storage.engine) == before


def test_partial_restart_keeps_original_manager_and_retry_without_replacing_assets(storage):
    first = storage.create()
    identifier, = first.acquire().container_ids
    source = storage.engine.records[identifier]['Mounts'][0]['Source']
    first.release()
    factory = storage.factory
    failed = [False]
    def start_failure(plan, scope, directory, **options):
        manager = factory(plan, scope, directory, **options)
        original = manager.runner
        def runner(command, **kwargs):
            output = original(command, **kwargs)
            if command[1] == 'start' and not failed[0]:
                failed[0] = True
                raise p.PrerequisiteError('synthetic partial start')
            return output
        manager.runner = runner
        return manager
    owner = storage.create()
    owner.factory = start_failure
    storage.engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError) as caught: owner.acquire()
    assert caught.value.cleanup_pending.owners == (owner,)
    original_manager = owner.manager
    with pytest.raises(Exception): owner.acquire()
    assert owner.manager is original_manager
    storage.engine.fail_stop = False
    import asyncio
    asyncio.run(caught.value.cleanup_pending.retry())
    assert owner.pending_cleanup is None and stopped(storage, identifier)
    assert storage.engine.records[identifier]['Mounts'][0]['Source'] == source
    recovered = storage.create()
    assert recovered.acquire().container_ids == (identifier,)
    recovered.release()


def test_rebound_valid_registration_cannot_redirect_existing_cleanup_owner(storage, tmp_path):
    from api_agent.runtime.ownership import StackScope
    from api_agent.runtime.compose import rebind_reporting_plan
    owner = storage.create(); lease = owner.acquire()
    base, pack, directory = tmp_path / 'other-base', tmp_path / 'other-pack', tmp_path / 'other-journal'
    for path in (base, pack, directory): path.mkdir()
    scope = StackScope.from_roots(base, pack)
    alternative = storage.factory(rebind_reporting_plan(owner.plan, scope), scope, directory)
    path, record = binding(storage)
    record.update(scope=scope.id, directory=str(directory), fingerprints=alternative.fingerprints,
        directory_identity=[directory.stat().st_dev, directory.stat().st_ino],
        roots=[{'path':str(root), 'identity':[root.stat().st_dev, root.stat().st_ino]} for root in (base, pack)])
    path.write_text(json.dumps(record))
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='registration changed'):
        owner.release()
    assert owner.lease is lease and mutation_commands(storage.engine) == before


@pytest.mark.parametrize('action', ['restart', 'release'])
def test_changed_registered_service_refuses_without_compose_or_stop(storage, action):
    owner = storage.create()
    lease = owner.acquire()
    if action == 'restart': owner.release(retain=True)
    record = storage.engine.records[lease.container_ids[0]]
    record['Config']['Labels']['com.docker.compose.service'] = 'unrelated'
    if action == 'restart': record['State']['Running'] = False
    before = mutation_commands(storage.engine)
    operation = storage.create() if action == 'restart' else owner
    with pytest.raises(p.PrerequisiteError, match='reporting service changed'):
        operation.acquire() if action == 'restart' else operation.release()
    assert mutation_commands(storage.engine) == before


def test_changed_registry_ids_cannot_redirect_retained_cleanup_to_verified_replacement(storage):
    from copy import deepcopy
    owner = storage.create()
    lease = owner.acquire()
    identifier, = lease.container_ids
    replacement = deepcopy(storage.engine.records.pop(identifier))
    replacement['Id'] = 'f' * 64
    storage.engine.records[replacement['Id']] = replacement
    path, registration = binding(storage)
    registration['ids'] = [replacement['Id']]
    path.write_text(json.dumps(registration))
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='container registration changed'):
        owner.release()
    assert owner.lease is lease and mutation_commands(storage.engine) == before
    assert replacement['State']['Running']


def test_registry_only_id_drift_refuses_even_when_original_container_is_unchanged(storage):
    owner = storage.create()
    lease = owner.acquire()
    path, registration = binding(storage)
    registration['ids'] = ['f' * 64]
    path.write_text(json.dumps(registration))
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='container registration changed'):
        owner.release()
    assert owner.lease is lease and mutation_commands(storage.engine) == before


def test_released_owner_cannot_recover_stop_authority_from_current_registration(storage):
    owner = storage.create()
    identifier, = owner.acquire().container_ids
    owner.release(retain=True)
    before = mutation_commands(storage.engine)
    owner.release()
    assert owner.lease is None and storage.engine.records[identifier]['State']['Running']
    assert mutation_commands(storage.engine) == before


def test_canceled_failed_provision_retries_stop_and_final_intent_inventory(storage):
    cancel = Event()
    owner = storage.create(cancelled=cancel.is_set)
    factory = owner.factory
    def checked_factory(*args, **kwargs):
        manager = factory(*args, **kwargs)
        original = manager.runner
        def runner(command, **options):
            if options['cancelled']():
                raise p.PrerequisiteCancelled('synthetic canceled acquisition token')
            return original(command, **options)
        manager.runner = runner
        return manager
    owner.factory = checked_factory
    storage.engine.fail_up = storage.engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError) as failed: owner.acquire()
    receipt = failed.value.cleanup_pending
    cancel.set()
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    with pytest.raises(CleanupPendingError): asyncio.run(receipt.retry())
    assert owner.intent and owner.manager._rollback is not None
    storage.engine.fail_stop = False
    with pytest.raises(CleanupPendingError): asyncio.run(receipt.retry())
    assert owner.intent and owner.manager._rollback is None
    storage.engine.records.clear()  # independently recovered disposable fixture
    asyncio.run(receipt.retry())
    assert cancel.is_set() and not owner.intent and owner.pending_cleanup is None
    assert not list(storage.registry.glob('*.json'))
