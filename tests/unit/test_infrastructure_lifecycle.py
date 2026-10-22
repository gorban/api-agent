"""Synthetic captured pack conformance through the existing prerequisite manager."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration
from api_agent.runtime.compose import snapshot_compose
from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions
from api_agent.runtime.infrastructure_plan import ResolvedCheck, plan_infrastructure
from api_agent.runtime.infrastructure_session import InfrastructureSession
from api_agent.runtime.ownership import StackScope
import api_agent.runtime.prerequisites as p

FIXTURE = Path(__file__).parents[1] / 'fixtures/infrastructure_pack/synthetic_infrastructure'
SCHEMA = ConfigSchema({'USE_LOCAL_BACKING': FieldDeclaration('USE_LOCAL_BACKING', 'bool')})
TRANSPORT = {'DOCKER_HOST': 'unix:///run/synthetic/engine.sock'}


class Clock:
    value = 0
    def now(self): return self.value
    def sleep(self, amount): self.value += amount


class Engine:
    def __init__(self):
        self.records, self.calls, self.seeded = {}, [], set()
        self.manager = None
        self.fail_stop = False
        self.readiness = True
        self.seed_readiness = True

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        manager = self.manager
        if command[1] == 'ps': return '\n'.join(self.records)
        if command[1] == 'inspect': return json.dumps([self.records[key] for key in command[2:]])
        if command[1:3] == ('image', 'inspect'):
            return json.dumps([{'Id': 'sha256:' + '1' * 64, 'RepoDigests': [command[3]]}])
        if command[1:3] == ('volume', 'inspect'):
            labels = next(item['labels'] for name, item in manager.document['volumes'].items()
                if manager.scope.project + '_' + name == command[3])
            return json.dumps([{'Name': command[3], 'Labels': p._interpolate(labels, manager.environment)}])
        if command[1] == 'stop':
            if self.fail_stop: raise p.PrerequisiteError('Synthetic stop failure')
            for key in command[4:]: self.records[key]['State']['Running'] = False
            return ''
        if command[1] == 'logs': return 'stdout:\nsynthetic output\n\nstderr:\n'
        if 'up' in command:
            for name in command[command.index('--no-recreate') + 1:]:
                metadata = manager.conditional[name]
                assert set(metadata.dependencies) <= self.seeded, 'Dependency started before seed completion'
                present = next((item for item in self.records.values()
                    if item['Config']['Labels']['com.docker.compose.service'] == name), None)
                if present:
                    present['State']['Running'] = True
                    continue
                spec = p._interpolate(manager.document['services'][name], manager.environment)
                mounts = []
                for volume in spec['volumes']:
                    source, target, *options = volume.rsplit(':', 2)
                    if options:
                        mounts.append({'Type': 'bind', 'Source': source, 'Destination': target, 'RW': False})
                    else:
                        mounts.append({'Type': 'volume', 'Name': manager.scope.project + '_' + source,
                                       'Destination': target, 'RW': True})
                identifier = f'{len(self.records) + 1:064x}'
                self.records[identifier] = {'Id': identifier, 'Image': 'sha256:' + '1' * 64,
                    'Config': {'Image': spec['image'], 'Labels': {**spec['labels'],
                        'com.docker.compose.project': manager.scope.project,
                        'com.docker.compose.service': name, p.CONFIG_LABEL: manager.fingerprints[name]}},
                    'State': {'Running': True}, 'Mounts': mounts}
            return ''
        raise AssertionError(command)

    @contextmanager
    def open(self, request, timeout, **kwargs):
        url = request.full_url
        metadata = next(item for item in self.manager.conditional.values()
                        if item.readiness.url.rsplit('/', 1)[0] == url.rsplit('/', 1)[0])
        if url.endswith('/seed'):
            if self.seed_readiness: self.seeded.add(metadata.compose_name)
            value = {'seed': 'done' if self.seed_readiness else 'pending'}
        else:
            value = {'service': metadata.readiness.identity_value if self.readiness else 'pending'}
        yield SimpleNamespace(status=200, read=lambda limit: json.dumps(value).encode())


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    pack = tmp_path / 'external-pack'
    shutil.copytree(FIXTURE, pack)
    context = SimpleNamespace(pack_root=pack, owner_root=pack, pack_id='synthetic',
                              infrastructure_paths=(pack / 'infrastructure.toml',))
    captured = load_infrastructure_contributions(context)
    scope = StackScope('c' * 16)
    plan = plan_infrastructure({'USE_LOCAL_BACKING': True}, SCHEMA, captured, scope,
                              engine_environment=TRANSPORT)
    engine, clock = Engine(), Clock()
    manager = p.PrerequisiteManager(plan, scope, tmp_path / 'ownership',
        command_runner=engine.run, engine_environment=TRANSPORT,
        now=clock.now, sleep=clock.sleep, identity=lambda pid: 'synthetic:process')
    engine.manager = manager
    monkeypatch.setattr(p, '_identity_response', engine.open)
    return manager, engine, clock, pack, captured


def test_external_pack_capture_dag_seed_reuse_and_endpoints(synthetic):
    manager, engine, _, pack, _ = synthetic
    session = InfrastructureSession(manager)
    assert not session.endpoints
    shutil.rmtree(pack)  # execution cannot reread the source pack

    lease = session.acquire()

    assert len(lease.owned_ids) == 2
    assert len(session.endpoints) == 2
    starts = [call for call, _ in engine.calls if 'up' in call]
    assert [call[-1] for call in starts] == list(manager.conditional)
    assert all('--no-deps' in call for call in starts)
    assert all(options['environment']['DOCKER_HOST'] == TRANSPORT['DOCKER_HOST']
               for _, options in engine.calls)
    downstream = next(item for item in manager.conditional.values() if item.dependencies)
    assert manager.specs[downstream.compose_name]['environment']['UPSTREAM'] == next(
        item.endpoints[0].in_project for item in manager.conditional.values() if not item.dependencies)
    assert session.status() and 'synthetic output' in session.logs(lease.owned_ids[0])
    session.release(retain=True)
    assert not session.endpoints
    before = len(starts)
    reused = session.acquire()
    assert not reused.owned_ids and set(reused.reused_ids) == set(lease.owned_ids)
    assert len([call for call, _ in engine.calls if 'up' in call]) == before
    assert not any(call[1] == 'exec' for call, _ in engine.calls)
    session.release()
    assert all(item['State']['Running'] for item in engine.records.values())


def test_seed_timeout_prevents_downstream_start_and_rolls_back(synthetic):
    manager, engine, clock, _, _ = synthetic
    engine.seed_readiness = False

    with pytest.raises(p.PrerequisiteError, match='deadline'):
        manager.acquire()

    assert 15 <= clock.value < 16
    assert len([call for call, _ in engine.calls if 'up' in call]) == 1
    assert not manager.lease
    assert all(not item['State']['Running'] for item in engine.records.values())


def test_reuse_checks_seed_without_restart(synthetic):
    manager, engine, _, _, _ = synthetic
    manager.acquire()
    manager.release(retain=True)
    engine.seed_readiness = False
    before = len([call for call, _ in engine.calls if 'up' in call])

    with pytest.raises(p.PrerequisiteError, match='deadline'):
        manager.acquire()

    assert len([call for call, _ in engine.calls if 'up' in call]) == before
    assert all(item['State']['Running'] for item in engine.records.values())


@pytest.mark.parametrize('damage', ['bytes', 'writable', 'foreign_path', 'missing_mount', 'wrong_volume'])
def test_retained_storage_rejects_tampering(synthetic, damage):
    manager, engine, _, _, _ = synthetic
    manager.acquire()
    manager.release(retain=True)
    record = next(iter(engine.records.values()))
    bind = record['Mounts'][0]
    if damage == 'bytes':
        Path(bind['Source']).chmod(0o600)
        Path(bind['Source']).write_bytes(b'changed')
    elif damage == 'writable': bind['RW'] = True
    elif damage == 'foreign_path': bind['Source'] = '/unowned/script.py'
    elif damage == 'missing_mount': record['Mounts'].pop()
    else: record['Mounts'][1]['Name'] = 'unowned'

    with pytest.raises(p.PrerequisiteError, match='storage'):
        manager.acquire()


@pytest.mark.parametrize('identifier,tail,bound', [('bad',100,1024), ('1'*64,0,1024),
    ('1'*64,True,1024), ('1'*64,100,255), ('1'*64,100,65537)])
def test_invalid_log_selection_has_no_engine_work(synthetic, identifier, tail, bound):
    manager, engine, _, _, _ = synthetic
    with pytest.raises(p.PrerequisiteError, match='Invalid'):
        manager.logs(identifier, tail=tail, output_limit=bound)
    assert not engine.calls


def test_unowned_log_identifier_is_rejected(synthetic):
    manager, _, _, _, _ = synthetic
    with pytest.raises(p.PrerequisiteError, match='not owned'):
        manager.logs('f' * 64)


@pytest.mark.parametrize('status,body,expected', [(200,b'{"id":"fixture"}',True),
    (201,b'{"id":"fixture"}',False), (200,b'{"id":1}',False),
    (200,b'{"id":"wrong"}',False), (200,b'[]',False), (200,b'bad',False),
    (200,b'x'*65537,False)])
def test_http_identity_requires_bounded_matching_json(monkeypatch, status, body, expected):
    @contextmanager
    def open_response(request, timeout, **kwargs):
        yield SimpleNamespace(status=status, read=lambda limit: body)
    monkeypatch.setattr(p, '_identity_response', open_response)
    assert p._http_identity(ResolvedCheck('http','http://127.0.0.1:8090/ready',
                           identity_field='id',identity_value='fixture'), 1) is expected


def test_external_mode_keeps_explicit_external_backing_without_managed_dependency(synthetic):
    manager, _, _, _, captured = synthetic
    plan = plan_infrastructure({'USE_LOCAL_BACKING': False}, SCHEMA, captured,
                              manager.scope, engine_environment=TRANSPORT)
    metadata, = plan.conditional
    assert metadata.id == 'synthetic/external' and not metadata.dependencies
    assert json.loads(plan.assets['compose.json'])['services'][metadata.compose_name][
        'environment']['UPSTREAM'] == 'http://external-backing:8090'


def test_http_readiness_failure_is_not_ready(monkeypatch):
    def unavailable(*args, **kwargs): raise OSError('synthetic unavailable')
    monkeypatch.setattr(p, '_identity_response', unavailable)
    assert not p._http_identity(ResolvedCheck('http', 'http://127.0.0.1:8090/ready'), 1)


def test_health_readiness_requires_healthy_and_allows_no_seed(synthetic):
    manager, _, _, _, _ = synthetic
    name = next(iter(manager.conditional))
    manager.conditional = {name: replace(manager.conditional[name],
        readiness=ResolvedCheck('health'), seed_completion=None)}
    assert not manager._conditional_ready({}, 10)
    assert not manager._conditional_ready({name: SimpleNamespace(health=None)}, 10)
    assert manager._conditional_ready({name: SimpleNamespace(health='healthy')}, 10)


def test_stage_rejects_replaced_dependency_identity(synthetic, monkeypatch):
    manager, _, _, _, _ = synthetic
    manager.acquire()
    current = manager._compatible(manager._inventory(10), 10)
    name = next(iter(current))
    monkeypatch.setattr(manager, '_inventory', lambda deadline: {})
    with pytest.raises(p.PrerequisiteError, match='changed'):
        manager._wait_conditional(name, 10, current)


@pytest.mark.parametrize('damage', ['record_id', 'parent_mode', 'volume_labels'])
def test_storage_requires_identity_private_parent_and_owned_volume(synthetic, damage):
    manager, engine, _, _, _ = synthetic
    lease = manager.acquire()
    name = next(iter(manager.conditional))
    identifier = lease.owned_ids[0]
    original = manager.runner
    if damage == 'record_id':
        engine.records[identifier]['Id'] = 'f' * 64
    elif damage == 'parent_mode':
        Path(engine.records[identifier]['Mounts'][0]['Source']).parent.chmod(0o777)
    else:
        def wrong_labels(command, **kwargs):
            if command[1:3] == ('volume', 'inspect'):
                return json.dumps([{'Name': command[3], 'Labels': {}}])
            return original(command, **kwargs)
        manager.runner = wrong_labels
    with pytest.raises(p.PrerequisiteError, match='storage'):
        manager.verify_conditional_storage(name, identifier, 10)


def test_status_honors_explicit_deadline_and_default_bound(synthetic):
    manager, _, _, _, _ = synthetic
    assert manager.status() == ()
    with pytest.raises(p.PrerequisiteError, match='deadline'):
        manager.status(_deadline=0)


def test_generic_stopped_restart_preserves_captured_asset(synthetic):
    manager, engine, _, _, _ = synthetic
    first = manager.acquire()
    manager.release()
    engine.seeded.clear()
    second = manager.acquire()
    assert second.owned_ids == first.owned_ids
    assert len(engine.records) == 2
    manager.release()


@pytest.mark.parametrize('limit', [None, 256, 65536])
def test_log_runner_scrubs_before_bounding_each_stream(tmp_path, limit):
    import sys
    import time
    from api_agent.testing.diagnostics import CredentialScrubber
    secret = 'synthetic-log-secret'
    command = (sys.executable, '-c',
        "import sys; print('x'*1000+'synthetic-log-secret'); print('synthetic-log-secret', file=sys.stderr)")
    output = p.run_command(command, environment={'API_TOKEN': secret}, directory=tmp_path,
        deadline=time.monotonic()+5, cancelled=lambda: False, combine_output=True,
        output_limit=limit, output_scrubber=CredentialScrubber({'API_TOKEN': secret}))
    assert secret not in output and '[REDACTED]' in output
    assert 'stdout:' in output and 'stderr:' in output
    if limit == 256:
        assert '[Earlier output omitted]' in output
        assert len(output.encode()) <= limit


@pytest.mark.parametrize('limit', [True, 255])
def test_log_runner_rejects_invalid_output_bound(tmp_path, limit):
    with pytest.raises(ValueError, match='byte limit'):
        p.run_command((), environment={}, directory=tmp_path, deadline=1,
                      cancelled=lambda: False, output_limit=limit)


def test_conditional_manager_does_not_require_legacy_hook_environment(synthetic, tmp_path):
    manager, _, _, _, _ = synthetic
    plan = replace(manager.plan, environment={key: value for key, value in manager.plan.environment.items()
                                            if key != 'SA_LOCALSTACK_READY_DIGEST'})
    fresh = p.PrerequisiteManager(plan, manager.scope, tmp_path / 'fresh', engine_environment=TRANSPORT)
    assert fresh.fingerprints == manager.fingerprints


def test_logs_register_service_environment_credentials(synthetic):
    manager, engine, _, _, _ = synthetic
    lease = manager.acquire()
    name = next(iter(manager.specs))
    manager.specs[name].setdefault('environment', {})['PASSWORD'] = 'synthetic-private-value'
    manager.logs(lease.owned_ids[0], tail=7, output_limit=256)
    command, options = engine.calls[-1]
    assert command[-3:] == ('--tail', '7', lease.owned_ids[0])
    assert options['output_limit'] == 256
    assert options['output_scrubber']('synthetic-private-value') == '[REDACTED]'


def test_final_acquisition_rejects_late_staged_replacement(synthetic, monkeypatch):
    import copy
    manager, engine, _, _, _ = synthetic
    original = manager._wait_conditional
    final = tuple(manager.conditional)[-1]
    def replace_after_verification(name, deadline, verified):
        item = original(name, deadline, verified)
        if name == final:
            record = copy.deepcopy(engine.records.pop(item.id))
            record['Id'] = 'f' * 64
            engine.records['f' * 64] = record
        return item
    monkeypatch.setattr(manager, '_wait_conditional', replace_after_verification)
    with pytest.raises(p.PrerequisiteError, match='changed'):
        manager.acquire()
    assert manager.lease is None
    assert engine.records['f' * 64]['State']['Running']


def test_constructor_rejects_transport_different_from_captured_plan(synthetic, tmp_path):
    manager, engine, clock, _, _ = synthetic
    with pytest.raises(p.PrerequisiteError, match='engine configuration differs'):
        p.PrerequisiteManager(manager.plan, manager.scope, tmp_path / 'alternate',
            engine_environment={'DOCKER_HOST': 'unix:///run/synthetic/alternate.sock'},
            command_runner=engine.run, now=clock.now, sleep=clock.sleep)
    assert not engine.calls


def test_readiness_and_seed_completion_receive_independent_budgets(synthetic, monkeypatch):
    manager, _, clock, _, _ = synthetic
    first = next(iter(manager.conditional))
    record = manager.conditional[first]
    manager.conditional[first] = replace(record,
        readiness=replace(record.readiness, timeout=30),
        seed_completion=replace(record.seed_completion, timeout=1))
    original = p._http_identity
    def delayed(check, timeout, **kwargs):
        if check.url.endswith('/ready') and clock.value < 2:
            return False
        return original(check, timeout, **kwargs)
    monkeypatch.setattr(p, '_http_identity', delayed)
    manager.acquire()
    assert 2 <= clock.value < 3


def _second_manager(manager, engine, clock):
    second = p.PrerequisiteManager(manager.plan, manager.scope, manager.directory,
        command_runner=engine.run, engine_environment=TRANSPORT, now=clock.now,
        sleep=clock.sleep, identity=lambda pid: 'synthetic:process')
    engine.manager = second
    return second


def test_final_concurrent_releaser_inherits_conditional_cleanup(synthetic):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    second = _second_manager(manager, engine, clock)
    reused = second.acquire()
    assert not reused.owned_ids
    assert manager.release() == ()
    assert all(item['State']['Running'] for item in engine.records.values())
    assert set(second.release()) == set(lease.owned_ids)
    assert all(not item['State']['Running'] for item in engine.records.values())


def test_handed_off_cleanup_survives_final_stop_failure_and_retry(synthetic):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    second = _second_manager(manager, engine, clock)
    second.acquire()
    manager.release()
    engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError, match='stop failure'):
        second.release()
    assert second.lease is not None
    engine.fail_stop = False
    assert set(second.release()) == set(lease.owned_ids)


@pytest.mark.parametrize('retaining', ['creator', 'successor'])
def test_explicit_retain_preserves_conditional_services_after_concurrent_release(synthetic, retaining):
    manager, engine, clock, _, _ = synthetic
    manager.acquire()
    second = _second_manager(manager, engine, clock)
    second.acquire()
    manager.release(retain=retaining == 'creator')
    second.release(retain=retaining == 'successor')
    assert all(item['State']['Running'] for item in engine.records.values())


def test_wait_supports_readiness_without_seed_completion(synthetic):
    manager, _, _, _, _ = synthetic
    manager.acquire()
    name = next(iter(manager.conditional))
    manager.conditional[name] = replace(manager.conditional[name], seed_completion=None)
    current = manager._compatible(manager._inventory(10), 10)
    assert manager._wait_conditional(name, 10, current).id == current[name].id


def test_final_readiness_change_rejects_acquisition(synthetic, monkeypatch):
    manager, engine, _, _, _ = synthetic
    original = manager._wait_conditional
    final = tuple(manager.conditional)[-1]
    def lose_readiness(name, deadline, verified):
        item = original(name, deadline, verified)
        if name == final: engine.readiness = False
        return item
    monkeypatch.setattr(manager, '_wait_conditional', lose_readiness)
    with pytest.raises(p.PrerequisiteError, match='readiness changed'):
        manager.acquire()
    assert manager.lease is None


@pytest.mark.parametrize('invalid', ['wrong', ['not-a-leased-id']])
def test_journal_rejects_malformed_cleanup_duty(synthetic, invalid):
    manager, _, _, _, _ = synthetic
    lease = manager.acquire()
    path = manager.directory / f'{manager.scope.id}.json'
    journal = json.loads(path.read_text())
    journal[lease.invocation_id]['conditional_cleanup_ids'] = invalid
    path.write_text(json.dumps(journal))
    with pytest.raises(p.PrerequisiteError, match='Invalid ownership lease'):
        manager.release()


@pytest.mark.parametrize('argv,expected', [([], ()),
    (['--use-local-backing=False'], ('synthetic/external',)),
    (['--use-local-backing=True'], ('synthetic/backing', 'synthetic/managed'))])
def test_synthetic_modes_use_fresh_composed_configuration(synthetic, argv, expected):
    from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config
    manager, _, _, pack, captured = synthetic
    schema = compose_schema(load_shared_schema(), load_declarations(pack / 'config.toml'))
    config = resolve_config(schema, argv=argv, environ={}, dotenv={})
    assert not config.errors
    plan = plan_infrastructure(config, schema, captured, manager.scope, engine_environment=TRANSPORT)
    assert tuple(item.id for item in plan.conditional) == expected


def _stale_creator(manager, lease):
    path = manager.directory / f'{manager.scope.id}.json'
    journal = json.loads(path.read_text())
    journal[lease.invocation_id]['identity'] = 'previous-process-birth'
    path.write_text(json.dumps(journal))


@pytest.mark.parametrize('peer_first', [False, True])
def test_stale_creator_duty_reaches_final_compatible_peer(synthetic, peer_first):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    second = _second_manager(manager, engine, clock)
    if peer_first: second.acquire()
    _stale_creator(manager, lease)
    if not peer_first: second.acquire()
    assert set(second.release()) == set(lease.owned_ids)
    assert all(not item['State']['Running'] for item in engine.records.values())
    assert json.loads((manager.directory / f'{manager.scope.id}.json').read_text()) == {}


def test_stale_creator_cleanup_retries_after_stop_failure(synthetic):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    second = _second_manager(manager, engine, clock)
    second.acquire()
    _stale_creator(manager, lease)
    engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError, match='stop failure'):
        second.release()
    engine.fail_stop = False
    assert set(second.release()) == set(lease.owned_ids)


def test_unclaimed_stale_duty_remains_recoverable_without_active_lease(synthetic):
    manager, _, _, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    journal = p._read_journal(manager.directory, manager.scope, manager.identity)
    duty = journal[lease.invocation_id]
    assert duty['cleanup_only'] is True
    assert set(duty['conditional_cleanup_ids']) == set(lease.owned_ids)
    assert p.active_leased_ids(manager.directory, manager.scope, identity=manager.identity) == set()
    p._write_journal(manager.directory, manager.scope, journal)
    assert p._read_journal(manager.directory, manager.scope, manager.identity) == journal


def test_explicit_retain_does_not_become_stale_cleanup_duty(synthetic):
    manager, engine, clock, _, _ = synthetic
    manager.acquire()
    manager.release(retain=True)
    second = _second_manager(manager, engine, clock)
    second.acquire()
    assert second.release() == ()
    assert all(item['State']['Running'] for item in engine.records.values())


def test_absent_stale_duty_retires_only_with_matching_engine_inventory(synthetic):
    manager, engine, _, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    journal = p._read_journal(manager.directory, manager.scope, manager.identity)
    manager._retire_disposed_duties(journal, manager._inventory(10))
    assert lease.invocation_id in journal
    engine.records.clear()
    manager._retire_disposed_duties(journal, manager._inventory(10))
    assert not journal


def test_unknown_engine_stale_duty_never_hands_off_or_disappears(synthetic):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    path = manager.directory / f'{manager.scope.id}.json'
    journal = json.loads(path.read_text())
    journal[lease.invocation_id]['conditional_engine_id'] = 'd' * 64
    path.write_text(json.dumps(journal))
    second = _second_manager(manager, engine, clock)
    second.acquire()
    assert second.release() == ()
    pending = json.loads(path.read_text())
    assert pending[lease.invocation_id]['cleanup_only'] is True
    assert all(item['State']['Running'] for item in engine.records.values())


@pytest.mark.parametrize('field,value', [('conditional_engine_id', 'wrong'),
    ('conditional_engine_id', 1), ('cleanup_only', 'wrong'), ('conditional_cleanup_ids', [])])
def test_malformed_cleanup_only_record_is_rejected(synthetic, field, value):
    manager, _, _, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    path = manager.directory / f'{manager.scope.id}.json'
    journal = json.loads(path.read_text())
    journal[lease.invocation_id]['cleanup_only'] = True
    journal[lease.invocation_id][field] = value
    path.write_text(json.dumps(journal))
    with pytest.raises(p.PrerequisiteError, match='Invalid ownership'):
        p._read_journal(manager.directory, manager.scope, manager.identity)


def test_disabled_acquisition_preserves_pending_duty_without_engine_work(synthetic):
    manager, engine, clock, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    disabled = p.PrerequisiteManager(replace(manager.plan, profiles=(), services=(), conditional=()),
        manager.scope, manager.directory, command_runner=engine.run, engine_environment=TRANSPORT,
        now=clock.now, sleep=clock.sleep, identity=manager.identity)
    engine.calls.clear()
    disabled.acquire()
    disabled.release()
    assert not engine.calls
    pending = json.loads((manager.directory / f'{manager.scope.id}.json').read_text())
    assert pending[lease.invocation_id]['cleanup_only'] is True


def test_absent_duty_on_unknown_engine_remains_recoverable(synthetic):
    manager, engine, _, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    journal = p._read_journal(manager.directory, manager.scope, manager.identity)
    journal[lease.invocation_id]['conditional_engine_id'] = 'd' * 64
    engine.records.clear()
    manager._retire_disposed_duties(journal, manager._inventory(10))
    assert journal[lease.invocation_id]['conditional_cleanup_ids'] == list(lease.owned_ids)


def test_stale_duty_does_not_transfer_to_unrelated_live_lease(synthetic):
    manager, _, _, _, _ = synthetic
    lease = manager.acquire()
    _stale_creator(manager, lease)
    path = manager.directory / f'{manager.scope.id}.json'
    journal = json.loads(path.read_text())
    journal['unrelated'] = {**journal[lease.invocation_id], 'identity': 'synthetic:process',
        'ids': ['f' * 64], 'conditional_cleanup_ids': []}
    path.write_text(json.dumps(journal))
    recovered = p._read_journal(manager.directory, manager.scope, manager.identity)
    assert recovered['unrelated']['conditional_cleanup_ids'] == []
    assert recovered[lease.invocation_id]['cleanup_only'] is True


@contextmanager
def _loopback_response(mode):
    import socket
    import threading
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(1)
    stopped = threading.Event()
    failures = []
    body = json.dumps({'id': 'fixture', 'padding': 'x' * 64}).encode()
    headers = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Padding: '
               + b'x' * 64 + b'\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n')
    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(1)
                connection.recv(4096)
                if mode == 'quiet':
                    stopped.wait(2)
                    return
                if mode == 'headers':
                    fragments = [bytes([value]) for value in headers] + [body]
                elif mode == 'body':
                    fragments = [headers] + [bytes([value]) for value in body]
                elif mode == 'truncated_length':
                    fragments = [b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n' + body]
                elif mode == 'chunked':
                    fragments = [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                        + format(len(body), 'x').encode() + b'\r\n' + body + b'\r\n0\r\n\r\n']
                elif mode == 'close_delimited':
                    fragments = [b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n' + body]
                elif mode == 'truncated_chunked':
                    fragments = [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                        + b'1000\r\n' + body]
                elif mode == 'malformed':
                    fragments = [b'not-an-http-response\r\n\r\n']
                else:
                    fragments = [headers + body]
                for fragment in fragments:
                    if stopped.is_set(): return
                    connection.sendall(fragment)
                    if mode in {'headers', 'body'}: stopped.wait(.01)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError as error:
            if mode != 'no_request': failures.append(error)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=serve, name='owned-http-fixture')
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1]) + '/ready'
    finally:
        stopped.set()
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert not failures


@pytest.mark.parametrize('mode', ['headers', 'body'])
def test_real_progressing_http_response_cannot_extend_absolute_deadline(mode):
    import time
    with _loopback_response(mode) as url:
        started = time.monotonic()
        assert not p._http_identity(ResolvedCheck('http', url, identity_field='id', identity_value='fixture'), .05)
        assert time.monotonic() - started < .3


@pytest.mark.parametrize('mode,expected', [('fast', True), ('quiet', False), ('malformed', False)])
def test_real_http_response_success_idle_timeout_and_bad_headers(mode, expected):
    with _loopback_response(mode) as url:
        assert p._http_identity(ResolvedCheck('http', url, identity_field='id', identity_value='fixture'), .1) is expected


def test_real_http_wait_observes_cancellation_without_waiting_for_deadline():
    import time
    with _loopback_response('quiet') as url:
        started = time.monotonic()
        with pytest.raises(p.PrerequisiteCancelled):
            p._http_identity(ResolvedCheck('http', url), 5,
                             cancelled=lambda: time.monotonic() - started >= .03)
        assert time.monotonic() - started < .3


@pytest.mark.parametrize('url', ['https://127.0.0.1:8090/ready', 'http://example.invalid:8090/ready',
    'http://127.0.0.1/ready', 'http://user:pass@127.0.0.1:8090/ready', 'http://127.0.0.1:8090/ready#fragment'])
def test_identity_response_requires_captured_loopback_http_contract(url):
    assert not p._http_identity(ResolvedCheck('http', url), .1)


def test_identity_reader_checks_expired_budget_before_connecting():
    assert not p._http_identity(ResolvedCheck('http', 'http://127.0.0.1:8090/ready'), 0)


def test_identity_never_accepts_redirect_even_when_status_matches(monkeypatch):
    @contextmanager
    def redirect(*args, **kwargs):
        yield SimpleNamespace(status=302, read=lambda limit: b'{"id":"fixture"}')
    monkeypatch.setattr(p, '_identity_response', redirect)
    assert not p._http_identity(ResolvedCheck('http', 'http://127.0.0.1:8090/ready', status=302,
                                            identity_field='id', identity_value='fixture'), 1)


@pytest.mark.parametrize('suffix', ['', '/ready?probe=1'])
def test_owned_http_request_uses_default_path_and_bounded_query(suffix):
    with _loopback_response('fast') as url:
        target = url.rsplit('/ready', 1)[0] + suffix
        assert p._http_identity(ResolvedCheck('http', target, identity_field='id', identity_value='fixture'), .2)


@pytest.mark.parametrize('mode,expected', [('truncated_length', False), ('chunked', True),
    ('close_delimited', True), ('truncated_chunked', False)])
def test_real_identity_requires_completed_http_framing(mode, expected):
    with _loopback_response(mode) as url:
        assert p._http_identity(ResolvedCheck('http', url, identity_field='id',
                                            identity_value='fixture'), .2) is expected


@pytest.mark.parametrize('userinfo', ['@', ':@'])
def test_real_identity_rejects_present_empty_userinfo(userinfo):
    with _loopback_response('no_request') as url:
        target = url.replace('http://', 'http://' + userinfo, 1)
        assert not p._http_identity(ResolvedCheck('http', target, identity_field='id',
                                                identity_value='fixture'), .2)
