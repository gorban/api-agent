"""Parent recovery must retain only predeclared native acquisition intents."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.testing.native_recovery import NativeRecoveryOwner
from api_agent.runtime.ownership import StackScope


def test_killed_worker_recovery_pins_ids_and_ignores_foreign(tmp_path, monkeypatch):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    code = '''
import json, os, signal, sys
from pathlib import Path
root = Path(sys.argv[1])
state = root / 'acquisition'
state.mkdir(mode=0o700)
from api_agent.runtime.ownership import StackScope
(state / 'recovery.json').write_text(json.dumps({'version': 1, 'scope': StackScope.from_roots(root, state).id,
 'nonce': 'a'*32, 'catalog': {}, 'fingerprints': {'service': 'f'*64}}))
os.chmod(state / 'recovery.json', 0o600)
os.kill(os.getpid(), signal.SIGKILL)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(owner.directory)])
    assert process.wait() == -signal.SIGKILL
    calls = []
    ids = ['1'*64, '2'*64, '3'*64]
    class Runtime:
        def inventory(self):
            return [SimpleNamespace(id=value, running=True) for value in ids]
        def _inspect(self, selected):
            return [{'Id': value, 'Config': {'Labels': {'api-agent.acquisition': nonce,
                'api-agent.config': 'f'*64, 'com.docker.compose.service': 'service'}}}
                for value, nonce in zip(ids, ['a'*32, 'b'*32, None], strict=True)]
        def stop(self, selected):
            receipt = json.loads((owner.directory / 'acquisition' / 'selected-ids.json').read_text())
            assert receipt == ['1'*64]
            calls.append(tuple(selected))
    monkeypatch.setattr(owner, '_runtimes', lambda state, record: (Runtime(),))
    owner.recover()
    assert calls == [('1'*64,)]


def test_recovery_rejects_symlink_descriptor(tmp_path):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    state = owner.directory / 'acquisition'
    state.mkdir(mode=0o700)
    foreign = tmp_path / 'foreign'
    foreign.write_text('{}')
    (state / 'recovery.json').symlink_to(foreign)
    with pytest.raises(ValueError, match='private'):
        owner.recover()


def test_recovery_uses_pinned_engine_scope_and_canonical_stop(tmp_path, monkeypatch):
    from hashlib import sha256
    from api_agent.runtime.engine_targets import EngineTarget, EngineTargetRegistry
    from api_agent.runtime.inventory import InventoryCatalog
    from api_agent.runtime.ownership import StackScope
    from api_agent.runtime.containers import ContainerRuntime
    from api_agent.testing.native_recovery import record_intent
    from api_agent.testing import native_recovery
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    state = owner.directory / 'acquisition'
    state.mkdir(mode=0o700)
    scope = StackScope.from_roots(owner.directory, state)
    body = 'version = 1\nowner = "native"\n[[services]]\nid = "tool"\n'
    authority = [{'owner': 'native', 'root': str(tmp_path), 'path': 'inventory.toml'}]
    catalog = InventoryCatalog.from_wire({'scope': scope.id, 'authority': authority,
        'declarations': [{'body': body, 'sha256': sha256(body.encode()).hexdigest()}]},
        scope=scope, authority=authority)
    service, profile = catalog.pairs[0]
    monkeypatch.setattr(native_recovery, 'admitted_directory', lambda: owner.directory)
    record_intent(state, scope, catalog, {service: 'f'*64}, 'a'*32)
    target = EngineTarget('isolated', '/usr/bin/true',
        sha256(Path('/usr/bin/true').read_bytes()).hexdigest(), 'b'*64,
        {'DOCKER_HOST': 'unix:///recorded.sock'})
    assert tuple(owner._runtimes(state, json.loads((state / 'recovery.json').read_text()))) == ()
    EngineTargetRegistry(state / 'registry').remember(target)
    stopped = []
    labels = {**scope.labels(profile, profiles=(profile,)),
        'com.docker.compose.project': scope.project, 'com.docker.compose.service': service,
        'api-agent.acquisition': 'a'*32, 'api-agent.config': 'f'*64}
    row = {'Id': '1'*64, 'Name': '/owned', 'Config': {'Image': 'image', 'Labels': labels},
        'State': {'Status': 'running', 'Running': True}}
    foreign = {**row, 'Id': '2'*64, 'Config': {**row['Config'],
        'Labels': {**labels, 'api-agent.scope': 'fedcba9876543210'}}}
    def run(runtime, args, **kwargs):
        assert runtime.engine_target.environment['DOCKER_HOST'] == 'unix:///recorded.sock'
        if args[0] == 'ps':
            return '1'*64 + '\n' + '2'*64
        if args[0] == 'inspect':
            return json.dumps([item for item in (row, foreign) if item['Id'] in args[1:]])
        assert args == ['stop', '--time', '10', '1'*64]
        stopped.append(args[-1])
        return ''
    monkeypatch.setattr(ContainerRuntime, '_run', run)
    monkeypatch.setattr(EngineTarget, 'verify', lambda *a, **kw: None)
    owner.recover()
    assert stopped == ['1'*64]


def test_recovery_refuses_unreaped_worker_before_engine_access(tmp_path):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    (owner.directory / 'unstarted').mkdir(mode=0o700)
    owner.workers.append(SimpleNamespace())
    with pytest.raises(ValueError, match='worker disposal'):
        owner.recover()


def test_suite_allocates_recovery_before_work_and_disposes_afterward(tmp_path):
    from api_agent.testing.integration import _admit_operation
    observed = []
    @_admit_operation
    def execute(*, suite_directory):
        root = Path(suite_directory)
        assert (root / 'native-recovery').is_dir()
        observed.append(root)
        return SimpleNamespace(inspections=())
    execute()
    assert not observed[0].exists()


def test_interruption_keeps_primary_and_retryable_recovery_storage(monkeypatch):
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.integration import _admit_operation
    from api_agent.testing import native_recovery
    calls = []
    original = native_recovery.NativeRecoveryOwner.recover
    def recover(owner):
        calls.append(owner.directory)
        if len(calls) == 1:
            raise OSError('temporarily unavailable')
        original(owner)
    monkeypatch.setattr(native_recovery.NativeRecoveryOwner, 'recover', recover)
    @_admit_operation
    def execute(*, suite_directory):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt) as caught:
        execute()
    assert isinstance(caught.value.cleanup_pending, CleanupPendingError)
    assert calls[0].exists()
    asyncio.run(caught.value.cleanup_pending.retry())
    assert not calls[0].exists()


def test_debug_worker_transfers_recovery_until_inspection_closes(monkeypatch):
    from api_agent.testing.integration import _admit_operation
    observed = []
    class Inspection:
        closed = False
        def _when_closed(self, callback):
            self.callback = callback
    inspection = Inspection()
    @_admit_operation
    def execute(*, suite_directory):
        observed.append(Path(suite_directory))
        return SimpleNamespace(inspections=(inspection,))
    execute()
    assert observed[0].is_dir()
    inspection.closed = True
    inspection.callback()
    inspection.callback()  # Acknowledgements can be replayed safely.
    assert not observed[0].exists()


def test_native_recovery_admission_uses_existing_protected_snapshot(tmp_path, monkeypatch):
    from api_agent.testing import native_recovery, provider_setup
    monkeypatch.delenv('API_AGENT_INTEGRATION_CONTEXT', raising=False)
    assert native_recovery.admitted_directory() is None
    native_recovery.record_intent(tmp_path, None, None, None, None)
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(tmp_path / 'launch.json'))
    monkeypatch.setattr(provider_setup, 'integration_context_directory', lambda: tmp_path)
    assert native_recovery.admitted_directory() == owner.directory
    native_recovery.record_intent(tmp_path, None, None, None, None)


@pytest.mark.parametrize('record', [None, {}, {'version': 2, 'scope': '0123456789abcdef',
    'nonce': 'a'*32, 'catalog': {}, 'fingerprints': {'service': 'f'*64}}])
def test_malformed_intent_never_accesses_engines(tmp_path, record):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    state = owner.directory / 'acquisition'
    state.mkdir(mode=0o700)
    path = state / 'recovery.json'
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    with pytest.raises(ValueError, match='intent'):
        owner.recover()


def test_recovery_ignores_allocation_interrupted_before_intent(tmp_path):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    (owner.directory / 'unstarted').mkdir(mode=0o700)
    owner.recover()


@pytest.fixture
def pending_intent(tmp_path, monkeypatch):
    owner = NativeRecoveryOwner(tmp_path)
    owner.prepare()
    state = owner.directory / 'acquisition'
    state.mkdir(mode=0o700)
    record = {'version': 1, 'scope': StackScope.from_roots(owner.directory, state).id,
        'nonce': 'a'*32, 'catalog': {}, 'fingerprints': {'service': 'f'*64}}
    path = state / 'recovery.json'
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    calls = []
    labels = {'api-agent.acquisition': 'a'*32, 'api-agent.config': 'f'*64,
        'com.docker.compose.service': 'service'}
    class Runtime:
        candidates = [SimpleNamespace(id='1'*64, running=True)]
        def inventory(self):
            return self.candidates
        def _inspect(self, selected):
            return [{'Id': '1'*64, 'Config': {'Labels': labels}}]
        def stop(self, selected):
            calls.append(selected)
    runtime = Runtime()
    monkeypatch.setattr(owner, '_runtimes', lambda state, record: (runtime,))
    return SimpleNamespace(owner=owner, state=state, labels=labels, calls=calls, runtime=runtime)


def test_recovery_accepts_no_remaining_containers(pending_intent):
    pending_intent.runtime.candidates = []
    pending_intent.owner.recover()
    assert pending_intent.calls == []


@pytest.mark.parametrize('labels', [
    {'api-agent.config': 'e'*64},
    {'com.docker.compose.service': 'unknown', 'api-agent.config': None},
    {'api-agent.acquisition': 'b'*32},
])
def test_recovery_ignores_foreign_intent_labels(pending_intent, labels):
    pending_intent.labels.update(labels)
    pending_intent.owner.recover()
    assert pending_intent.calls == []


def test_recovery_retries_same_pinned_id(pending_intent):
    receipt = pending_intent.state / 'selected-ids.json'
    receipt.write_text(json.dumps(['1'*64]))
    receipt.chmod(0o600)
    pending_intent.owner.recover()
    assert pending_intent.calls == [['1'*64]]


def test_recovery_rejects_invalid_id_receipt(pending_intent):
    receipt = pending_intent.state / 'selected-ids.json'
    receipt.write_text(json.dumps({'bad': True}))
    receipt.chmod(0o600)
    with pytest.raises(ValueError, match='immutable identities'):
        pending_intent.owner.recover()


def test_recovery_never_adopts_replacement_after_ids_are_pinned(pending_intent):
    receipt = pending_intent.state / 'selected-ids.json'
    receipt.write_text(json.dumps(['2'*64]))
    receipt.chmod(0o600)
    pending_intent.owner.recover()
    assert pending_intent.calls == []


def test_recovery_rejects_scope_outside_parent_allocation(pending_intent):
    path = pending_intent.state / 'recovery.json'
    record = json.loads(path.read_text())
    record['scope'] = '0'*16
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='scope differs'):
        pending_intent.owner.recover()


def test_descriptor_reader_rejects_unsafe_open_file(tmp_path):
    from api_agent.testing.native_recovery import _read
    path = tmp_path / 'record.json'
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        _read(path)


def test_descriptor_reader_rejects_metadata_change(tmp_path, monkeypatch):
    from api_agent.testing import native_recovery
    path = tmp_path / 'record.json'
    path.write_text('{}')
    path.chmod(0o600)
    original = os.fstat
    calls = []
    def fstat(descriptor):
        value = original(descriptor)
        calls.append(descriptor)
        return value if len(calls) == 1 else SimpleNamespace(
            st_size=value.st_size + 1, st_mtime_ns=value.st_mtime_ns)
    monkeypatch.setattr(native_recovery.os, 'fstat', fstat)
    with pytest.raises(ValueError, match='changed while reading'):
        native_recovery._read(path)


def test_recovery_refuses_multiple_recorded_engines(tmp_path, monkeypatch):
    from api_agent.testing import native_recovery
    owner = NativeRecoveryOwner(tmp_path)
    state = tmp_path / 'acquisition'
    state.mkdir(mode=0o700)
    monkeypatch.setattr(native_recovery.InventoryCatalog, 'from_wire', lambda *a, **kw: object())
    monkeypatch.setattr(native_recovery.RecoveredContainerRuntime, 'known_targets',
        lambda self: (('a'*64, 'first'), ('b'*64, 'second')))
    with pytest.raises(ValueError, match='multiple recorded engines'):
        tuple(owner._runtimes(state, {'scope': 'a'*16, 'catalog': {'authority': []}}))


def test_recovery_refuses_registry_directory_symlink(tmp_path, monkeypatch):
    from api_agent.testing import native_recovery
    owner = NativeRecoveryOwner(tmp_path)
    state = tmp_path / 'acquisition'
    state.mkdir(mode=0o700)
    (state / 'registry').symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(native_recovery.InventoryCatalog, 'from_wire', lambda *a, **kw: object())
    with pytest.raises(ValueError, match='private'):
        tuple(owner._runtimes(state, {'scope': 'a'*16, 'catalog': {'authority': []}}))


def test_debug_registration_race_retains_storage_and_suite_lease(tmp_path, monkeypatch):
    import asyncio
    import gc
    from threading import RLock
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.integration import _admit_operation
    from api_agent.testing import native_recovery, suite_lock
    monkeypatch.setattr(suite_lock, 'suite_lock_path', lambda: tmp_path / 'suite.lock')
    class RacingInspection(suite_lock.CleanupLeaseOwner):
        closed = False
        _lock = RLock()
        def _when_closed(self, callback):
            self.closed = True
            super()._when_closed(callback)
    calls = []
    original = native_recovery.NativeRecoveryOwner.recover
    def recover(owner):
        calls.append(owner.directory)
        if len(calls) == 1:
            raise OSError('engine temporarily unavailable')
        original(owner)
    monkeypatch.setattr(native_recovery.NativeRecoveryOwner, 'recover', recover)
    @suite_lock.suite_locked
    @_admit_operation
    def execute(*, suite_directory):
        return SimpleNamespace(inspections=(RacingInspection(),))
    with pytest.raises(CleanupPendingError) as caught:
        execute()
    gc.collect()
    assert calls[0].is_dir()
    with pytest.raises(suite_lock.SuiteBusyError):
        suite_lock.suite_locked(lambda: None)()
    asyncio.run(caught.value.retry())
    assert not calls[0].exists()
    assert suite_lock.suite_locked(lambda: 'released')() == 'released'


def test_recovery_retry_survives_interruption_after_directory_deletion(monkeypatch):
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.integration import _admit_operation
    from api_agent.testing import integration
    original = integration.tempfile.TemporaryDirectory.cleanup
    calls = []
    def cleanup(temporary):
        original(temporary)
        calls.append(temporary.name)
        if len(calls) == 1:
            raise InterruptedError('interrupted after deletion')
    monkeypatch.setattr(integration.tempfile.TemporaryDirectory, 'cleanup', cleanup)
    @_admit_operation
    def execute(*, suite_directory):
        return SimpleNamespace(inspections=())
    with pytest.raises(CleanupPendingError) as caught:
        execute()
    assert not Path(calls[0]).exists()
    asyncio.run(caught.value.retry())
    assert len(calls) == 2


def test_recovery_retry_replays_interrupted_closed_acknowledgement(monkeypatch):
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.integration import _admit_operation
    from api_agent.testing import native_recovery
    original = native_recovery.NativeRecoveryOwner._acknowledge_closed
    calls = []
    def acknowledge(owner):
        assert owner.closed
        calls.append(owner.directory)
        if len(calls) == 1:
            raise InterruptedError('interrupted before acknowledgement')
        original(owner)
    monkeypatch.setattr(native_recovery.NativeRecoveryOwner, '_acknowledge_closed', acknowledge)
    @_admit_operation
    def execute(*, suite_directory):
        return SimpleNamespace(inspections=())
    with pytest.raises(CleanupPendingError) as caught:
        execute()
    asyncio.run(caught.value.retry())
    assert len(calls) == 2


@pytest.mark.parametrize('descriptors', [[0], (True,), (-1,), ([],), (0, 0)])
def test_native_command_rejects_invalid_descriptor_contract(tmp_path, descriptors):
    import time
    from api_agent.runtime.prerequisites import run_command
    with pytest.raises(ValueError, match='inherited prerequisite descriptors'):
        run_command(('/usr/bin/true',), environment={}, directory=tmp_path,
            deadline=time.monotonic()+5, cancelled=lambda: False, inherited_fds=descriptors)


def test_detached_native_command_keeps_recovery_pending_until_actual_cli_finishes(pending_intent):
    import time
    from api_agent.testing.processes import finalize, wait_owned
    from api_agent.testing import native_recovery
    owner, state = pending_intent.owner, pending_intent.state
    ready, late = state / 'ready', state / 'late-creation'
    child_code = ('import os,signal,sys,time;from pathlib import Path;'
        'Path(sys.argv[1]).write_text(str(os.getpgrp()));'
        'os.kill(int(sys.argv[3]),signal.SIGKILL);time.sleep(.8);'
        'Path(sys.argv[2]).write_text("created")')
    worker_code = '''
import os,sys,time
from api_agent.runtime.prerequisites import run_command
from api_agent.testing.native_recovery import run_native_command
run_native_command(sys.argv[1], run_command,
    (sys.executable,'-c',sys.argv[2],sys.argv[3],sys.argv[4],str(os.getpid())),
    environment=os.environ, directory=sys.argv[1],
    deadline=time.monotonic()+10, cancelled=lambda:False)
'''
    worker = subprocess.Popen([sys.executable, '-c', worker_code, str(state), child_code,
        str(ready), str(late)], start_new_session=True)
    owner.workers.append(worker)
    assert wait_owned(worker, timeout=10) == -signal.SIGKILL
    finalize(worker)
    assert int(ready.read_text()) != worker.pid
    assert not late.exists()
    with pytest.raises(ValueError, match='acquisition remains active'):
        owner.recover()
    assert pending_intent.calls == []
    # The orphan CLI retains its inherited duty. Wait for that exact lock to
    # become available, rather than guessing or signalling an unreserved PID.
    deadline = time.monotonic()+5
    while True:
        try:
            with native_recovery._quiescent(state):
                break
        except ValueError:
            assert time.monotonic() < deadline
            time.sleep(.01)
    assert late.read_text() == 'created'
    owner.recover()
    assert pending_intent.calls == [['1'*64]]


def test_native_command_runner_holds_activity_lock_and_passes_exact_descriptor(tmp_path):
    from api_agent.testing.native_recovery import run_native_command, _quiescent
    state = tmp_path / 'acquisition'
    state.mkdir(mode=0o700)
    def runner(command, *, inherited_fds, on_not_started):
        from api_agent.testing.native_recovery import _read, _write, _request_digest
        intent = Path(command[-2])
        request = _read(intent / 'request.json')
        assert request['command'] == ['selected-command']
        assert len(inherited_fds) == 1
        os.fstat(inherited_fds[0])
        with pytest.raises(ValueError, match='acquisition remains active'):
            with _quiescent(state):
                pytest.fail('running native command lost its activity duty')
        _write(intent / 'complete.json', {'version': 1,
            'request_sha256': _request_digest(request), 'disposed': True})
        return 'completed'
    assert run_native_command(state, runner, ('selected-command',)) == 'completed'
    with _quiescent(state):
        assert (state / 'activity.lock').is_file()


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_intent_is_acknowledged_when_supervisor_is_rejected_before_spawn(tmp_path, monkeypatch, cancelled):
    import time
    from api_agent.runtime import prerequisites
    from api_agent.testing.native_recovery import run_native_command, _quiescent
    state = tmp_path / 'acquisition'
    state.mkdir(mode=0o700)
    monkeypatch.setattr(prerequisites.subprocess, 'Popen',
        lambda *args, **kwargs: pytest.fail('rejected supervisor launched'))

    with pytest.raises(prerequisites.PrerequisiteError):
        run_native_command(state, prerequisites.run_command, ('selected-command',),
            environment={}, directory=tmp_path,
            deadline=time.monotonic() + (5 if cancelled else -1), cancelled=lambda: cancelled)

    with _quiescent(state):
        assert len(list((state / 'commands').glob('*/complete.json'))) == 1


def test_canonical_command_inherits_only_explicit_native_descriptor(tmp_path):
    import time
    from api_agent.runtime.prerequisites import run_command
    descriptor = os.open(tmp_path / 'activity.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        output = run_command((sys.executable, '-c',
            'import os,sys;print(os.fstat(int(sys.argv[1])).st_ino)', str(descriptor)),
            environment={}, directory=tmp_path, deadline=time.monotonic()+5,
            cancelled=lambda: False, inherited_fds=(descriptor,))
        assert int(output) == os.fstat(descriptor).st_ino
    finally:
        os.close(descriptor)


def test_supervisor_disposes_fd_closing_plugin_after_cli_leader_and_worker_die(pending_intent):
    import time
    from api_agent.testing.processes import finalize, wait_owned
    from api_agent.testing.native_recovery import _quiescent
    owner, state = pending_intent.owner, pending_intent.state
    ready, release, late = (state / name for name in ('plugin-ready', 'plugin-release', 'plugin-late'))
    plugin = '''
import os,sys,time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpgrp()))
while not Path(sys.argv[2]).exists(): time.sleep(.01)
Path(sys.argv[3]).write_text('late creation')
'''
    cli = '''
import os,signal,subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:5]],
    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
while not Path(sys.argv[2]).exists(): time.sleep(.01)
os.kill(int(sys.argv[5]),signal.SIGKILL)
os._exit(0)
'''
    worker_code = '''
import os,sys,time
from api_agent.runtime.prerequisites import run_command
from api_agent.testing.native_recovery import run_native_command
run_native_command(sys.argv[1], run_command,
    (sys.executable,'-c',sys.argv[2],sys.argv[3],*sys.argv[4:7],str(os.getpid())),
    environment=os.environ,directory=sys.argv[1],deadline=time.monotonic()+5,cancelled=lambda:False)
'''
    worker = subprocess.Popen([sys.executable, '-c', worker_code, str(state), cli, plugin,
        str(ready), str(release), str(late)], start_new_session=True)
    owner.workers.append(worker)
    assert wait_owned(worker, timeout=10) == -signal.SIGKILL
    finalize(worker)
    deadline = time.monotonic()+5
    while True:
        try:
            with _quiescent(state):
                break
        except ValueError:
            assert time.monotonic() < deadline
            time.sleep(.01)
    owner.recover()
    assert pending_intent.calls == [['1'*64]]
    assert int(ready.read_text()) != worker.pid
    release.write_text('would allow a leaked plugin to create resources')
    time.sleep(.1)
    assert not late.exists()


def test_native_command_intent_rejects_payload_over_budget_before_launch(tmp_path):
    from api_agent.testing.native_recovery import run_native_command
    with pytest.raises(ValueError, match='state budget'):
        run_native_command(tmp_path, lambda *a, **kw: pytest.fail('oversized command launched'),
            ('x'*131073,))


def test_recovery_accepts_empty_command_directory_before_first_launch(tmp_path):
    from api_agent.testing.native_recovery import _quiescent
    (tmp_path / 'commands').mkdir(mode=0o700)
    descriptor = os.open(tmp_path / 'activity.lock', os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    with _quiescent(tmp_path):
        assert tuple((tmp_path / 'commands').iterdir()) == ()


def test_invalid_disposal_observer_cannot_launch_native_command(tmp_path):
    from api_agent.runtime.prerequisites import run_command
    import time
    with pytest.raises(ValueError, match='disposal observer'):
        run_command(('/usr/bin/true',), environment={}, directory=tmp_path,
            deadline=time.monotonic()+5, cancelled=lambda: False, on_disposed=True)


def test_recovery_accepts_activity_file_before_command_directory_exists(tmp_path):
    from api_agent.testing.native_recovery import _quiescent
    descriptor = os.open(tmp_path / 'activity.lock', os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    with _quiescent(tmp_path):
        assert not (tmp_path / 'commands').exists()


def test_canonical_command_observes_confirmed_native_group_disposal(tmp_path):
    from api_agent.runtime.prerequisites import run_command
    import time
    observed = []
    assert run_command(('/usr/bin/true',), environment={}, directory=tmp_path,
        deadline=time.monotonic()+5, cancelled=lambda: False,
        on_disposed=lambda: observed.append('disposed')) == ''
    assert observed == ['disposed']


def test_preparation_without_container_intent_blocks_recovery_while_command_active(pending_intent):
    import fcntl
    (pending_intent.state / 'recovery.json').unlink()
    descriptor = os.open(pending_intent.state / 'activity.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='acquisition remains active'):
            pending_intent.owner.recover()
        assert pending_intent.calls == []
    finally:
        os.close(descriptor)


def test_preparation_without_activity_file_requires_prelaunch_acknowledgement(pending_intent):
    from api_agent.testing.native_recovery import _write
    (pending_intent.state / 'recovery.json').unlink()
    command = pending_intent.state / 'commands' / 'prelaunch'
    command.mkdir(mode=0o700, parents=True)
    command.parent.chmod(0o700)
    _write(command / 'request.json', {'prelaunch': True})
    with pytest.raises(ValueError, match='disposal remains unconfirmed'):
        pending_intent.owner.recover()
    assert pending_intent.calls == []


def test_suite_ignores_inspection_without_cleanup_subscription_capability():
    from api_agent.testing.integration import _admit_operation
    observed = []
    @_admit_operation
    def execute(*, suite_directory):
        observed.append(Path(suite_directory))
        return SimpleNamespace(inspections=(SimpleNamespace(),))
    assert len(execute().inspections) == 1
    assert not observed[0].exists()


@pytest.mark.parametrize('already_disposed', [False, True])
def test_suite_disposes_actual_native_worker_before_recovery(monkeypatch, already_disposed):
    from api_agent.testing import integration, native_recovery, processes
    worker = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
        start_new_session=True)
    original_finalize = processes.finalize
    original_recover = native_recovery.NativeRecoveryOwner.recover
    finalized, recovered = [], []
    if already_disposed:
        original_finalize(worker)
    def finalize(owned):
        assert owned is worker
        finalized.append(owned)
        return original_finalize(owned)
    def recover(owner):
        assert processes.disposal_complete(worker)
        recovered.append(owner)
        original_recover(owner)
    monkeypatch.setattr(integration, 'finalize', finalize)
    monkeypatch.setattr(native_recovery.NativeRecoveryOwner, 'recover', recover)
    @integration._admit_operation
    def execute(*, suite_directory):
        state = Path(suite_directory) / 'native-recovery' / 'unstarted'
        state.mkdir(mode=0o700)
        integration._RUNNER_OPERATION.recovery.workers.append(worker)
        return SimpleNamespace(inspections=())
    try:
        execute()
        assert len(finalized) == int(not already_disposed)
        assert len(recovered) == 1
        assert processes.disposal_complete(worker)
    finally:
        original_finalize(worker)


def test_suite_keeps_native_storage_until_all_retained_inspections_close():
    from api_agent.testing.integration import _admit_operation
    class Inspection:
        closed = False
        def _when_closed(self, callback):
            self.callback = callback
        def close(self):
            self.closed = True
            self.callback()
    first, second = Inspection(), Inspection()
    observed = []
    @_admit_operation
    def execute(*, suite_directory):
        observed.append(Path(suite_directory))
        return SimpleNamespace(inspections=(first, second))
    execute()
    first.close()
    assert observed[0].is_dir()
    second.close()
    assert not observed[0].exists()


def test_single_wave_executor_disposes_worker_without_parent_recovery_registry(tmp_path, monkeypatch):
    from api_agent.testing import integration, processes
    from api_agent.testing.collection import collect_tests
    suite = tmp_path / 'tests' / 'integration'
    suite.mkdir(parents=True)
    (suite / 'test_sample.py').write_text('def test_passes():\n    assert True\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,))
    selected = collect_tests(context, 'integration')
    owned = []
    original_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        worker = original_popen(*args, **kwargs)
        owned.append(worker)
        return worker
    monkeypatch.delattr(integration._RUNNER_OPERATION, 'recovery', raising=False)
    monkeypatch.setattr(integration, 'subprocess', SimpleNamespace(
        **(vars(subprocess) | {'Popen': spawn})))
    result = integration._run_integration_tests(context, selected, accepted=True)
    assert result.exit_code == 0
    assert [row.status for row in result.tests] == ['passed']
    assert len(owned) == 1
    assert processes.disposal_complete(owned[0])
    assert owned[0].stdout.closed
    assert not hasattr(integration._RUNNER_OPERATION, 'recovery')
