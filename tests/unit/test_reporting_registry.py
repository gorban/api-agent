"""Shared reporting uses synthetic engines and real disposable ownership journals."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from types import MappingProxyType

import pytest

from api_agent.runtime.compose import plan_compose, rebind_reporting_plan
from api_agent.runtime.ownership import StackScope
from api_agent.runtime import prerequisites as p
from api_agent.runtime import reporting_registry as r
from api_agent.runtime import reporting_metadata as metadata
from test_prerequisites import Engine
from test_reporting_engine import engine as identity_engine, identify as identify_engine

CONFIG = {'ALLURE_S3_USE_LOCALSTACK': True}


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix='api-agent-reporting-test-', dir=Path.home()) as directory:
        yield Path(directory)


class ReportingEngine(Engine):
    def add(self, *args, **kwargs):
        identifier = super().add(*args, **kwargs)
        directory = self.owner.snapshot_directory
        document = json.loads(self.owner.plan.assets['compose.json'])
        data_mount, = [value for value in document['services']['localstack']['volumes']
                       if value.startswith('localstack-data:')]
        assert data_mount == 'localstack-data:/persisted-data'
        self.records[identifier]['Mounts'] = [
            {'Destination': '/etc/localstack/init/ready.d/10-api-agent-s3.sh', 'Type': 'bind', 'RW': False,
             'Source': str(directory / 'localstack-ready.sh')},
            {'Destination': data_mount.split(':')[1], 'Type': 'volume',
             'Name': self.owner.scope.project + '_localstack-data'}]
        return identifier

    def run(self, command, **kwargs):
        if command[1] == 'start':
            self.commands.append((command, kwargs))
            for identifier in command[2:]:
                self.records[identifier]['State']['Running'] = True
            return ''
        if command[1:3] == ('volume', 'inspect'):
            self.commands.append((command, kwargs))
            return json.dumps([{'Name': command[3], 'Labels': self.owner.scope.labels('localstack')}])
        return super().run(command, **kwargs)


@pytest.fixture
def storage(tmp_path):
    engine = ReportingEngine()
    state = SimpleNamespace(engine=engine, generation='engine', registry=tmp_path / 'registry')
    def factory(plan, scope, directory, **kwargs):
        manager = p.PrerequisiteManager(plan, scope, directory, command_runner=engine.run,
            readiness_probe=lambda *args: True, **kwargs)
        engine.owner = manager
        return manager
    state.factory = factory
    def create(config=None, **kwargs):
        plan = plan_compose(config or CONFIG, StackScope.from_roots(tmp_path / 'checkout', tmp_path / 'pack'))
        return r.SharedReportingManager(plan, registry_directory=state.registry,
            identity=lambda *args, **kwargs: state.generation, manager_factory=factory,
            occupied=lambda port: False, **kwargs)
    state.create = create
    return state


def mutation_commands(engine):
    return [command for command, _ in engine.commands if 'up' in command or command[1] in {'exec', 'stop', 'start'}]


def binding(storage):
    path, = storage.registry.glob('*.json')
    return path, json.loads(path.read_text())


def test_two_checkouts_share_one_server_and_journal(storage, tmp_path):
    first = storage.create()
    one = first.acquire()
    plan = plan_compose(CONFIG, StackScope.from_roots(tmp_path / 'other', tmp_path / 'other-pack'))
    second = r.SharedReportingManager(plan, registry_directory=storage.registry,
        identity=lambda *args, **kwargs: storage.generation, manager_factory=storage.factory,
        occupied=lambda port: pytest.fail('registered server must not be discovered by port'))
    two = second.acquire()
    assert one.owned_ids == two.reused_ids and first.key == second.key
    journal = first.manager.directory / (first.manager.scope.id + '.json')
    assert len(json.loads(journal.read_text())) == 2
    assert first.release() is None
    assert storage.engine.records[two.reused_ids[0]]['State']['Running']
    assert len(json.loads(journal.read_text())) == 1
    second.release(retain=True)
    assert json.loads(journal.read_text()) == {}
    assert len([command for command, _ in storage.engine.commands if 'up' in command]) == 1
    assert first.acquire() is not None  # retained server remains reusable
    first.release(retain=True)


@pytest.mark.parametrize('stopped', [False, True])
@pytest.mark.parametrize('legacy_three_assets', [False, True])
def test_retained_localstack_reuses_exact_object(storage, stopped, legacy_three_assets):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    if legacy_three_assets:
        old_assets.pop('jaeger-config.json')
    else:
        old_assets['jaeger-config.json'] += b'\n'
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    path, record = binding(storage)
    saved_record = path.read_bytes()
    snapshot = dict(old_assets)
    if stopped:
        storage.engine.records[identifier]['State']['Running'] = False
    before = mutation_commands(storage.engine)

    joined = storage.create()
    lease = joined.acquire()

    assert lease.container_ids == (identifier,)
    assert path.read_bytes() == saved_record
    assert dict(joined.manager.plan.assets) == snapshot
    assert set(record['assets']) == set(snapshot)
    delta = mutation_commands(storage.engine)[len(before):]
    assert delta == ([('docker', 'start', identifier)] if stopped else [])
    joined.release(retain=True)
    assert path.read_bytes() == saved_record


@pytest.mark.parametrize('during_cleanup', [False, True])
def test_malformed_registered_retained_compose_is_wrapped_without_losing_owner(storage, during_cleanup):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    joined = storage.create()
    if during_cleanup:
        joined.acquire()
    path, record = binding(storage)
    saved_record = path.read_bytes()
    snapshot = Path(next(iter(storage.engine.records.values()))['Mounts'][0]['Source']).parent
    compose_path = snapshot / 'compose.json'
    saved_compose = compose_path.read_bytes()
    invalid = b'{broken-json'
    compose_path.unlink()
    compose_path.write_bytes(invalid)
    compose_path.chmod(0o444)
    record['assets']['compose.json'] = sha256(invalid).hexdigest()
    metadata._write(path, record)
    altered_record = path.read_bytes()
    before = mutation_commands(storage.engine)

    try:
        with pytest.raises(p.PrerequisiteError, match=(
                'cleanup remains owned' if during_cleanup else 'no initialization was performed')):
            if during_cleanup:
                joined.release(retain=True)
            else:
                joined.acquire()
        assert mutation_commands(storage.engine) == before
        assert path.read_bytes() == altered_record
        if during_cleanup:
            assert joined.lease is not None
    finally:
        compose_path.unlink()
        compose_path.write_bytes(saved_compose)
        compose_path.chmod(0o444)
        path.write_bytes(saved_record)
    if during_cleanup:
        joined.release(retain=True)
        assert joined.lease is None


@pytest.mark.parametrize('drift', ['missing-object', 'changed-hook', 'changed-compose',
                                   'changed-fingerprint', 'writable-bind', 'dotdot-bind'])
def test_retained_plan_refuses_drift_without_engine_mutation(storage, drift):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    path, record = binding(storage)
    saved_record = path.read_bytes()
    snapshot = original.manager.snapshot_directory
    if snapshot is None:
        snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    if drift == 'missing-object':
        storage.engine.records.clear()
    elif drift == 'changed-hook':
        (snapshot / 'localstack-ready.sh').write_bytes(b'changed')
    elif drift == 'changed-compose':
        (snapshot / 'compose.json').unlink()
        (snapshot / 'compose.json').write_bytes(b'changed')
        (snapshot / 'compose.json').chmod(0o444)
    elif drift == 'changed-fingerprint':
        storage.engine.records[identifier]['Config']['Labels'][p.CONFIG_LABEL] = '0' * 64
    elif drift == 'dotdot-bind':
        hook = storage.engine.records[identifier]['Mounts'][0]
        source = Path(hook['Source'])
        hook['Source'] = str(source.parent / '..' / source.parent.name / source.name)
    else:
        storage.engine.records[identifier]['Mounts'][0]['RW'] = True
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError):
        storage.create().acquire()

    assert mutation_commands(storage.engine) == before
    assert path.read_bytes() == saved_record


def test_invalid_registered_original_directory_refuses_before_lock_or_engine(storage):
    original = storage.create()
    original.acquire()
    original.release(retain=True)
    path, record = binding(storage)
    record.pop('directory_identity')
    metadata._write(path, record)
    before = list(storage.engine.commands)

    with pytest.raises(p.PrerequisiteError, match='Invalid reporting binding'):
        storage.create().acquire()

    assert storage.engine.commands == before


def test_registered_scope_cannot_create_lock_in_other_directory(storage, tmp_path):
    original = storage.create()
    original.acquire()
    original.release(retain=True)
    path, record = binding(storage)
    outside = tmp_path / 'unrelated-owner'
    outside.mkdir(mode=0o700)
    info = outside.stat()
    record['directory'] = str(outside)
    record['directory_identity'] = [info.st_dev, info.st_ino]
    metadata._write(path, record)
    before = list(storage.engine.commands)

    with pytest.raises(p.PrerequisiteError, match='Invalid reporting binding'):
        storage.create().acquire()

    assert list(outside.iterdir()) == []
    assert storage.engine.commands == before


def test_same_normalized_fingerprint_cannot_replace_exact_selected_spec(storage):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    compose = json.loads((snapshot / 'compose.json').read_bytes())
    volumes = compose['services']['localstack']['volumes']
    hook_mount = next(value for value in volumes if '/etc/localstack/init/ready.d/' in value)
    volumes[volumes.index(hook_mount)] = ('sha256:' + original.plan.environment['SA_LOCALSTACK_READY_DIGEST']
        + ':/etc/localstack/init/ready.d/10-api-agent-s3.sh:ro,z')
    body = json.dumps(compose).encode()
    (snapshot / 'compose.json').unlink()
    (snapshot / 'compose.json').write_bytes(body)
    (snapshot / 'compose.json').chmod(0o444)
    path, record = binding(storage)
    record['assets']['compose.json'] = sha256(body).hexdigest()
    metadata._write(path, record)
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError, match='Retained LocalStack service differs'):
        storage.create().acquire()

    assert mutation_commands(storage.engine) == before


def test_shared_reporting_rejects_other_selected_service(storage):
    plan = storage.create().plan
    mixed = replace(plan, services=('localstack', 'jaeger'))
    with pytest.raises(p.PrerequisiteError, match='only the LocalStack profile'):
        r.SharedReportingManager(mixed, registry_directory=storage.registry,
            identity=lambda *args, **kwargs: storage.generation, manager_factory=storage.factory)
    assert storage.engine.commands == []


@pytest.mark.parametrize('drift', ['extra-snapshot-entry', 'missing-snapshot-entry',
                                   'group-writable-asset', 'symlink-asset'])
def test_retained_snapshot_requires_exact_trusted_files(storage, drift):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    if drift == 'extra-snapshot-entry':
        (snapshot / 'unexpected.txt').write_bytes(b'unreviewed')
    elif drift == 'missing-snapshot-entry':
        (snapshot / 'empty.env').unlink()
    elif drift == 'group-writable-asset':
        (snapshot / 'compose.json').chmod(0o664)
    else:
        target = snapshot / 'compose.json'
        target.unlink()
        target.symlink_to(snapshot / 'empty.env')
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError):
        storage.create().acquire()

    assert mutation_commands(storage.engine) == before


def test_retained_snapshot_parent_replacement_during_read_refuses(storage, monkeypatch):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    scanned = [False]
    scandir = os.scandir
    stat = Path.stat
    def after_listing(directory):
        entries = scandir(directory)
        if isinstance(directory, int):
            scanned[0] = True
        return entries
    def replaced_parent(path, *args, **kwargs):
        info = stat(path, *args, **kwargs)
        if path == snapshot and scanned[0]:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    monkeypatch.setattr(r.os, 'scandir', after_listing)
    monkeypatch.setattr(Path, 'stat', replaced_parent)
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError, match='Retained reporting assets could not be verified'):
        storage.create().acquire()

    assert scanned[0] and mutation_commands(storage.engine) == before


def test_retained_snapshot_parent_permission_change_before_open_refuses(storage, monkeypatch):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    opened = os.open
    changed = [False]
    def change_mode_at_open(path, flags, *args, **kwargs):
        if path == snapshot and flags & os.O_DIRECTORY:
            snapshot.chmod(0o770)
            changed[0] = True
        return opened(path, flags, *args, **kwargs)
    monkeypatch.setattr(r.os, 'open', change_mode_at_open)
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError, match='Retained reporting assets could not be verified'):
        storage.create().acquire()

    assert changed[0] and mutation_commands(storage.engine) == before


def test_container_identity_change_between_inventory_and_bind_inspection_refuses(storage):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    before = mutation_commands(storage.engine)
    factory = storage.factory
    inspected = [0]
    def inconsistent_factory(*args, **kwargs):
        manager = factory(*args, **kwargs)
        runner = manager.runner
        def changed_second_inspect(command, **options):
            result = runner(command, **options)
            if len(command) == 3 and command[1] == 'inspect':
                inspected[0] += 1
                if inspected[0] == 2:
                    rows = json.loads(result)
                    rows[0]['Id'] = 'f' * 64
                    return json.dumps(rows)
            return result
        manager.runner = changed_second_inspect
        return manager
    joined = storage.create()
    joined.factory = inconsistent_factory

    with pytest.raises(p.PrerequisiteError, match='Retained reporting assets could not be verified'):
        joined.acquire()

    assert inspected[0] == 2 and mutation_commands(storage.engine) == before


def test_current_selected_service_drift_refuses_legacy_reuse(storage):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    joined = storage.create()
    assets = dict(joined.plan.assets)
    compose = json.loads(assets['compose.json'])
    compose['services']['localstack']['image'] = 'unreviewed:image'
    assets['compose.json'] = json.dumps(compose).encode()
    joined.plan = replace(joined.plan, assets=MappingProxyType(assets))
    before = mutation_commands(storage.engine)

    with pytest.raises(p.PrerequisiteError, match='Selected LocalStack configuration changed'):
        joined.acquire()

    assert mutation_commands(storage.engine) == before


def test_original_directory_dotdot_is_not_lock_authority(storage):
    original = storage.create()
    original.acquire()
    original.release(retain=True)
    path, record = binding(storage)
    directory = Path(record['directory'])
    record['directory'] = str(directory / '..' / directory.name)
    metadata._write(path, record)
    before = list(storage.engine.commands)

    with pytest.raises(p.PrerequisiteError, match='Invalid reporting binding'):
        storage.create().acquire()

    assert storage.engine.commands == before


def test_selected_jaeger_requires_its_captured_config_asset(tmp_path):
    scope = StackScope.from_roots(tmp_path / 'checkout', tmp_path / 'pack')
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, scope)
    assets = dict(plan.assets)
    assets.pop('jaeger-config.json')

    with pytest.raises(p.PrerequisiteError, match='Selected Jaeger configuration is missing'):
        p.PrerequisiteManager(replace(plan, assets=MappingProxyType(assets)), scope, tmp_path / 'journal')


def test_retained_read_uses_remaining_parent_deadline(storage, monkeypatch):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    before = mutation_commands(storage.engine)
    read_assets = r._retained_assets
    def expire_after_read(*args, **kwargs):
        assets = read_assets(*args, **kwargs)
        import time
        time.sleep(.2)
        return assets
    monkeypatch.setattr(r, '_retained_assets', expire_after_read)

    with pytest.raises(p.PrerequisiteError, match='cancelled or timed out'):
        storage.create(deadline_seconds=.1).acquire()

    assert mutation_commands(storage.engine) == before


def test_cancelled_retained_asset_read_never_starts_or_leases(storage, monkeypatch):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    before = mutation_commands(storage.engine)
    cancelled = [False]
    opened = os.open
    def cancel_at_compose(path, flags, *args, **kwargs):
        descriptor = opened(path, flags, *args, **kwargs)
        if path == 'compose.json' and kwargs.get('dir_fd') is not None:
            cancelled[0] = True
        return descriptor
    monkeypatch.setattr(r.os, 'open', cancel_at_compose)

    with pytest.raises(p.PrerequisiteError, match='cancelled or timed out'):
        storage.create(cancelled=lambda: cancelled[0]).acquire()

    assert mutation_commands(storage.engine) == before
    path, _ = binding(storage)
    assert path.exists()


def test_retained_snapshot_change_after_read_refuses_before_reuse(storage):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    initial = original.acquire()
    original.release(retain=True)
    identifier, = initial.container_ids
    snapshot = Path(storage.engine.records[identifier]['Mounts'][0]['Source']).parent
    before = mutation_commands(storage.engine)
    factory = storage.factory
    calls = [0]
    def change_after_capture(*args, **kwargs):
        manager = factory(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            path = snapshot / 'compose.json'
            path.unlink()
            path.write_bytes(b'changed after captured read')
            path.chmod(0o444)
        return manager
    joined = storage.create()
    joined.factory = change_after_capture

    with pytest.raises(p.PrerequisiteError, match='captured assets'):
        joined.acquire()

    assert calls[0] == 2
    assert mutation_commands(storage.engine) == before


def test_cancelled_owner_can_release_recovered_legacy_binding(storage):
    original = storage.create()
    old_assets = dict(original.plan.assets)
    old_assets.pop('jaeger-config.json')
    old_assets['compose.json'] += b'\n'
    original.plan = replace(original.plan, assets=MappingProxyType(old_assets))
    original.acquire()
    original.release(retain=True)
    cancelled = [False]
    factory = storage.factory
    def checked_factory(*args, **kwargs):
        manager = factory(*args, **kwargs)
        runner = manager.runner
        def reject_canceled(command, **options):
            if options['cancelled']():
                raise p.PrerequisiteError('Synthetic canceled command')
            return runner(command, **options)
        manager.runner = reject_canceled
        return manager
    joined = storage.create(cancelled=lambda: cancelled[0])
    joined.factory = checked_factory
    joined.acquire()
    cancelled[0] = True

    joined.release(retain=True)

    assert joined.lease is None and joined.pending_cleanup is None


@pytest.mark.parametrize('change', ['bucket', 'region', 'assets', 'replaced', 'directory'])
def test_incompatible_binding_refuses_before_mutation(storage, change):
    first = storage.create()
    lease = first.acquire()
    first.release(retain=True)
    before = mutation_commands(storage.engine)
    config = dict(CONFIG)
    if change == 'bucket': config['ALLURE_S3_BUCKET'] = 'other-results'
    if change == 'region': config['ALLURE_S3_REGION'] = 'eu-west-1'
    if change == 'replaced': storage.engine.records.clear()
    if change in {'assets', 'directory'}:
        path, record = binding(storage)
        if change == 'assets': record['assets'] = {}
        else: record['directory_identity'] = [0, 0]
        metadata._write(path, record)
    with pytest.raises(p.PrerequisiteError):
        storage.create(config).acquire()
    assert mutation_commands(storage.engine) == before


@pytest.mark.parametrize('corruption', [None, [], {'bad': 'schema'}, {'roots': [None]}, {'ids': 9}])
def test_corrupt_registry_never_becomes_ownership(storage, corruption):
    owner = storage.create()
    owner.acquire()
    owner.release(retain=True)
    path, record = binding(storage)
    if isinstance(corruption, dict) and 'bad' not in corruption:
        record.update(corruption)
        corruption = record
    path.write_text(json.dumps(corruption))
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError):
        storage.create().acquire()
    assert mutation_commands(storage.engine) == before


def test_unregistered_occupied_endpoint_has_no_engine_mutation(storage):
    owner = storage.create()
    owner.occupied = lambda port: True
    with pytest.raises(p.PrerequisiteError, match='occupied and unregistered'):
        owner.acquire()
    assert storage.engine.commands == []


def test_unregistered_stopped_canonical_container_is_not_adopted(storage):
    first = storage.create()
    first.acquire()
    first.release(retain=True)
    path, record = binding(storage)
    path.unlink()
    for value in storage.engine.records.values(): value['State']['Running'] = False
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='changed or stopped'):
        storage.create().acquire()
    assert mutation_commands(storage.engine) == before


def test_failed_provisioning_preserves_retry_owner_and_clears_settled_intent(storage):
    owner = storage.create()
    storage.engine.fail_up = storage.engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError) as caught:
        owner.acquire()
    assert caught.value.cleanup_pending.owners == (owner,)
    assert owner.intent and binding(storage)[1]['state'] == 'provisioning'
    with pytest.raises(p.PrerequisiteError): owner.release()
    storage.engine.fail_stop = False
    with pytest.raises(p.PrerequisiteError, match='cleanup remains pending'):
        owner.release()
    # Stopped retained objects still require explicit review; controlled fixture
    # removal represents that independent verified recovery, not auto-adoption.
    storage.engine.records.clear()
    owner.release()
    assert not owner.intent
    assert not list(storage.registry.glob('*.json'))


def test_engine_generation_change_preserves_existing_lease(storage):
    owner = storage.create()
    owner.acquire()
    storage.generation = 'different'
    with pytest.raises(p.PrerequisiteError, match='generation changed'):
        owner.release()
    assert owner.lease is not None
    storage.generation = 'engine'
    owner.release(retain=True)


def test_explicit_enrollment_joins_original_journal_without_engine_writes(storage, tmp_path):
    base, pack = tmp_path / 'legacy-base', tmp_path / 'legacy-pack'
    base.mkdir(); pack.mkdir()
    scope = StackScope.from_roots(base, pack)
    selected = plan_compose(CONFIG, scope)
    original = storage.factory(selected, scope, tmp_path / 'original-journal')
    lease = original.acquire()
    original.release(retain=True)
    before = mutation_commands(storage.engine)
    key = r.enroll_reporting(plan_compose(CONFIG, StackScope('a' * 16)), base_root=base,
        pack_root=pack, ownership_directory=original.directory, registry_directory=storage.registry,
        identity=lambda *args, **kwargs: 'engine', manager_factory=storage.factory)
    assert mutation_commands(storage.engine) == before
    joined = storage.create()
    assert joined.acquire().reused_ids == lease.container_ids and joined.key == key
    journal = json.loads((original.directory / (scope.id + '.json')).read_text())
    assert len(journal) == 1
    joined.release(retain=True)
    assert json.loads((original.directory / (scope.id + '.json')).read_text()) == {}



def test_runtime_anonymous_volume_is_not_the_controlled_persisted_volume(storage):
    owner = storage.create()
    lease = owner.acquire()
    owner.release(retain=True)
    record = storage.engine.records[lease.container_ids[0]]
    volume, = [item for item in record['Mounts'] if item['Type'] == 'volume']
    assert volume['Destination'] == '/persisted-data'
    volume['Destination'] = '/var/lib/localstack'
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='storage ownership'):
        storage.create().acquire()
    assert mutation_commands(storage.engine) == before


@pytest.mark.parametrize('kind', ['missing', 'active', 'absent', 'already'])
def test_enrollment_requires_original_settled_verified_installation(storage, tmp_path, kind):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    scope = StackScope.from_roots(base, pack)
    original = storage.factory(plan_compose(CONFIG, scope), scope, tmp_path / 'journal')
    original.directory.mkdir()
    if kind != 'missing':
        original.acquire()
        if kind != 'active': original.release(retain=True)
    if kind == 'absent': storage.engine.records.clear()
    arguments = dict(base_root=base, pack_root=pack, ownership_directory=original.directory,
        registry_directory=storage.registry, identity=lambda *args, **kwargs: 'engine',
        manager_factory=storage.factory)
    if kind == 'already': r.enroll_reporting(original.plan, **arguments)
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError):
        r.enroll_reporting(original.plan, **arguments)
    assert mutation_commands(storage.engine) == before
    if kind == 'active':
        assert original.lease is not None
        original.release(retain=True)


@pytest.mark.parametrize('change', ['root-shape', 'root-identity', 'root-scope', 'canonical-scope', 'fingerprint'])
def test_binding_registration_integrity_is_checked(storage, tmp_path, change):
    owner = storage.create()
    owner.acquire(); owner.release(retain=True)
    path, record = binding(storage)
    if change.startswith('root'):
        base, pack = tmp_path / 'base', tmp_path / 'pack'
        base.mkdir(); pack.mkdir()
        record['roots'] = [dict(path=str(root), identity=[root.stat().st_dev, root.stat().st_ino])
                           for root in (base, pack)]
        if change == 'root-shape': record['roots'] = [None, None]
        if change == 'root-identity': record['roots'][0]['identity'] = [0, 0]
    if change == 'canonical-scope': record['scope'] = 'e' * 16
    if change == 'fingerprint': record['fingerprints'] = {}
    metadata._write(path, record)
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError): storage.create().acquire()
    assert mutation_commands(storage.engine) == before


def test_inert_release_idempotent_acquire_and_changed_directory_preserve_owner(storage):
    owner = storage.create(); owner.release()
    lease = owner.acquire()
    assert owner.acquire() is lease
    original = owner.ownership_identity
    owner.ownership_identity = [0, 0]
    with pytest.raises(p.PrerequisiteError, match='cleanup remains owned'): owner.release()
    assert owner.lease is lease
    owner.ownership_identity = original
    owner.release(retain=True)


def test_private_registry_and_supported_profile_are_required(storage):
    owner = storage.create()
    storage.registry.mkdir(mode=0o755)
    storage.registry.chmod(0o755)
    with pytest.raises(p.PrerequisiteError, match='must be private'): owner.acquire()
    storage.registry.chmod(0o700)
    with pytest.raises(p.PrerequisiteError, match='deadline'): storage.create(deadline_seconds=0)
    with pytest.raises(p.PrerequisiteError, match='only the LocalStack'):
        r.SharedReportingManager(plan_compose({}, StackScope('a' * 16)))


def test_metadata_size_limit_preserves_previous_registry_and_lease_journal(storage, tmp_path):
    path = tmp_path / 'metadata.json'
    metadata._write(path, {'prior': True})
    before = path.read_bytes()
    with pytest.raises(p.PrerequisiteError, match='bounded size'):
        metadata._write(path, {'large': 'x' * metadata.LIMIT})
    assert path.read_bytes() == before
    scope = StackScope('a' * 16)
    p._write_journal(tmp_path, scope, {})
    journal = tmp_path / (scope.id + '.json')
    with pytest.raises(p.PrerequisiteError, match='bounded size'):
        p._write_journal(tmp_path, scope, {'large': 'x' * metadata.LIMIT})
    assert journal.read_text() == '{}'


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory', 'oversize', 'shape', 'writable'])
def test_bounded_regular_metadata_refuses_invalid_files(tmp_path, kind):
    path = tmp_path / 'entry.json'
    if kind == 'symlink':
        target = tmp_path / 'valid.json'; target.write_text('{}'); path.symlink_to(target)
    if kind == 'fifo': os.mkfifo(path)
    if kind == 'directory': path.mkdir()
    if kind == 'oversize': path.write_text('x' * (metadata.LIMIT + 1))
    if kind == 'shape': path.write_text('[]')
    if kind == 'writable': path.write_text('{}'); path.chmod(0o666)
    with pytest.raises(p.PrerequisiteError): metadata._read(path)


@pytest.mark.parametrize('kind', ['symlink', 'writable', 'foreign-owner'])
def test_untrusted_ownership_directories_are_refused(tmp_path, monkeypatch, kind):
    target = tmp_path / 'target'; target.mkdir()
    path = target
    if kind == 'symlink':
        path = tmp_path / 'alias'; path.symlink_to(target)
    if kind == 'writable': target.chmod(0o777)
    if kind == 'foreign-owner':
        original = Path.stat
        def stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return os.stat_result((*value[:4], 0, *value[5:])) if path == target else value
        monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(p.PrerequisiteError): metadata._directory(path)
    target.chmod(0o700)


def test_metadata_growth_after_stat_is_still_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'growing.json'; path.write_text('{}')
    original = os.fstat
    def fstat(descriptor):
        info = original(descriptor)
        path.write_bytes(b'{}' + b' ' * (metadata.LIMIT - 1))
        return info
    monkeypatch.setattr(metadata, 'os', SimpleNamespace(**(vars(metadata.os) | {'fstat': fstat})))
    with pytest.raises(p.PrerequisiteError, match='Invalid reporting'):
        metadata._read(path)


def test_endpoint_occupancy_probe_observes_only_selected_loopback_port():
    import socket
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen()
        port = listener.getsockname()[1]
        assert r._occupied(port)
    assert not r._occupied(port)


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'writable'])
def test_ownership_lock_is_bounded_regular_and_private(tmp_path, kind):
    import time
    scope = StackScope('a' * 16)
    path = tmp_path / (scope.id + '.lock')
    if kind == 'fifo': os.mkfifo(path)
    if kind == 'directory': path.mkdir()
    if kind == 'writable': path.write_text(''); path.chmod(0o666)
    with pytest.raises(p.PrerequisiteError):
        with p._locked(tmp_path, scope, deadline=time.monotonic() + 5,
                       cancelled=lambda: False, now=time.monotonic, sleep=time.sleep):
            pytest.fail('Untrusted lock admitted')


def test_publication_only_loopback_performs_zero_engine_or_initialization_commands(tmp_path, monkeypatch):
    from api_agent.testing.prerequisites import local_recording_prerequisites
    from api_agent.runtime import dependencies
    from unittest.mock import Mock
    monkeypatch.setattr(dependencies, 'active_environment', lambda path: True)
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *args, **kwargs: None)
    monkeypatch.setattr(r, 'SharedReportingManager', lambda *args, **kwargs: pytest.fail('external storage queried registry'))
    original = p.PrerequisiteManager
    def factory(*args, **kwargs):
        return original(*args, command_runner=lambda *a, **k: pytest.fail('publication-only ran engine command'), **kwargs)
    monkeypatch.setattr(p, 'PrerequisiteManager', factory)
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    owner = local_recording_prerequisites(context, scope=StackScope('a' * 16), ownership_directory=tmp_path / 'owners')
    lease = owner.prepare(('selected',), {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'reports',
        'ALLURE_S3_ENDPOINT': 'http://localhost:4566', 'ALLURE_S3_USE_LOCALSTACK': False}, accepted=True)
    assert lease.profiles == () and lease.container_ids == ()
    owner.finish()


def test_canceled_operation_can_release_and_retry_while_stop_remains_set(storage, monkeypatch):
    import asyncio
    from threading import Event
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.prerequisites import RecordingPrerequisites
    stopped = Event()
    shared = storage.create(cancelled=stopped.is_set)
    owner = RecordingPrerequisites(prepare_dependencies=lambda: None, plan=lambda config: shared.plan,
                                  manager=lambda plan, cancelled: shared, cancel_event=stopped)
    owner.prepare(('selected',), {**CONFIG, 'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'allure-results'}, accepted=True)
    stopped.set()
    release = shared.manager.release
    calls = []
    def once(**kwargs):
        calls.append(True)
        if len(calls) == 1: raise p.PrerequisiteError('synthetic transient cleanup fault')
        return release(**kwargs)
    monkeypatch.setattr(shared.manager, 'release', once)
    with pytest.raises(CleanupPendingError) as caught: owner.finish()
    assert caught.value.owners == (shared,) and shared.lease is not None
    asyncio.run(caught.value.retry())
    assert stopped.is_set() and shared.lease is None and owner.owned_manager is None
    assert owner.pending_cleanup is None and len(calls) == 2


def test_canceled_acquisition_does_not_mutate_engine(storage):
    owner = storage.create(cancelled=lambda: True)
    with pytest.raises(p.PrerequisiteCancelled): owner.acquire()
    assert storage.engine.commands == []



def test_pending_creation_reacquire_keeps_exact_owner_and_retry(storage):
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    owner = storage.create()
    storage.engine.fail_up = storage.engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError) as caught: owner.acquire()
    manager = owner.manager
    receipt = caught.value.cleanup_pending
    with pytest.raises(CleanupPendingError) as repeated: owner.acquire()
    assert repeated.value is receipt and owner.manager is manager
    storage.engine.fail_stop = False
    with pytest.raises(CleanupPendingError): asyncio.run(receipt.retry())
    assert owner.manager is manager and owner.pending_cleanup is receipt
    storage.engine.records.clear()
    asyncio.run(receipt.retry())
    assert owner.pending_cleanup is None and not owner.intent


def test_cleanup_has_its_own_budget_after_expired_callers_deadline(storage, monkeypatch):
    from api_agent.providers import progress
    owner = storage.create()
    owner.acquire()
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    with progress.report_timeout_sync('Synthetic caller deadline', 5):
        clock[0] = 10.0
        owner.release(retain=True)
    assert owner.lease is None


def test_mixed_scope_enrollment_is_refused_before_registering_unusable_binding(storage, tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'; base.mkdir(); pack.mkdir()
    scope = StackScope.from_roots(base, pack)
    original = storage.factory(plan_compose(CONFIG, scope), scope, tmp_path / 'original')
    lease = original.acquire(); original.release(retain=True)
    extra = deepcopy(storage.engine.records[lease.container_ids[0]])
    extra['Id'] = 'f' * 64
    extra['Config']['Labels'].update({'com.docker.compose.service': 'jaeger', 'api-agent.profile': 'otel'})
    storage.engine.records[extra['Id']] = extra
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='one verified running LocalStack'):
        r.enroll_reporting(original.plan, base_root=base, pack_root=pack, ownership_directory=original.directory,
            registry_directory=storage.registry, identity=lambda *args, **kwargs: 'engine', manager_factory=storage.factory)
    assert mutation_commands(storage.engine) == before and not list(storage.registry.glob('*.json'))


def test_reused_acquisition_failure_retains_wrapper_for_original_journal_retry(storage, monkeypatch):
    import asyncio
    from api_agent.providers.control import CleanupPendingError
    first = storage.create(); first.acquire(); first.release(retain=True)
    original = p._write_journal
    def fail(*args): raise p.PrerequisiteError('synthetic journal fault')
    monkeypatch.setattr(p, '_write_journal', fail)
    second = storage.create()
    with pytest.raises(p.PrerequisiteError) as caught: second.acquire()
    assert caught.value.cleanup_pending.owners == (second,)
    manager = second.manager
    with pytest.raises(CleanupPendingError): second.acquire()
    assert second.manager is manager
    monkeypatch.setattr(p, '_write_journal', original)
    asyncio.run(caught.value.cleanup_pending.retry())
    assert second.pending_cleanup is None and manager._rollback is None


def test_reuse_manager_receives_remaining_parent_budget(storage, monkeypatch):
    from api_agent.providers import progress
    first = storage.create(); first.acquire(); first.release(retain=True)
    owner = storage.create()
    clock = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    def identity(*args, **kwargs):
        clock[0] += 2
        return 'engine'
    owner.identity = identity
    with progress.report_timeout_sync('Synthetic parent', 5):
        owner.acquire()
        assert owner.manager.timeout == 3
    owner.release(retain=True)


@pytest.mark.parametrize('kind', ['container-id', 'writable-bind', 'asset-content', 'hook-name', 'volume-labels', 'outside-snapshot'])
def test_storage_verification_rejects_one_invalid_ownership_fact(storage, tmp_path, kind, monkeypatch):
    import time
    owner = storage.create(); lease = owner.acquire(); owner.release(retain=True)
    manager = owner.manager
    identifier = lease.container_ids[0]
    record = storage.engine.records[identifier]
    hook, = [mount for mount in record['Mounts'] if mount['Type'] == 'bind']
    if kind == 'container-id': record['Id'] = 'f' * 64
    if kind == 'writable-bind': hook['RW'] = True
    if kind == 'asset-content':
        path = Path(hook['Source'])
        content = path.read_bytes()
        path.write_bytes(b'X' + content[1:])
    if kind == 'hook-name': hook['Source'] = str(Path(hook['Source']).with_name('other.sh'))
    if kind == 'outside-snapshot':
        outside = tmp_path / 'outside'; outside.mkdir()
        for name, content in manager.plan.assets.items(): (outside / name).write_bytes(content)
        hook['Source'] = str(outside / 'localstack-ready.sh')
    if kind == 'volume-labels':
        original = manager.runner
        def runner(command, **kwargs):
            output = original(command, **kwargs)
            if command[1:3] == ('volume', 'inspect'):
                values = json.loads(output); values[0]['Labels']['api-agent.scope'] = 'f' * 16
                return json.dumps(values)
            return output
        monkeypatch.setattr(manager, 'runner', runner)
    before = mutation_commands(storage.engine)
    with pytest.raises(p.PrerequisiteError, match='storage ownership'):
        manager.verify_reporting_storage(identifier, manager.directory, time.monotonic() + 5)
    assert mutation_commands(storage.engine) == before



def test_rotating_compatibility_id_enrollment_and_lookup_use_same_binding(storage, identity_engine, tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'; base.mkdir(); pack.mkdir()
    scope = StackScope.from_roots(base, pack)
    original = storage.factory(plan_compose(CONFIG, scope), scope, tmp_path / 'original')
    lease = original.acquire(); original.release(retain=True)
    identity_engine.cli = {'host': {'hostname': 'host'}, 'store': {'graphRoot': '/store'}}
    identity_engine.cli_ids = list(storage.engine.records)
    identity_engine.direct_ids = [{'Id': value} for value in identity_engine.cli_ids]
    calls = []
    def rotating(*args, **kwargs):
        calls.append(True)
        identity_engine.direct['ID'] = f'compatibility-request-{len(calls)}'
        return identify_engine(identity_engine)
    key = r.enroll_reporting(original.plan, base_root=base, pack_root=pack,
        ownership_directory=original.directory, registry_directory=storage.registry,
        identity=rotating, manager_factory=storage.factory)
    joined = r.SharedReportingManager(original.plan, registry_directory=storage.registry,
        identity=rotating, manager_factory=storage.factory)
    with joined._binding() as (path, budget):
        assert path.stem == key and metadata._read(path) is not None
    assert joined.acquire().reused_ids == lease.container_ids and joined.key == key
    joined.release(retain=True)
    assert len(calls) == 4 and joined.lease is None


def test_ipv6_only_listener_prevents_unregistered_managed_provisioning(storage):
    import socket
    if not socket.has_ipv6:
        pytest.skip('IPv6 is unavailable.')
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind(('::1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert r._occupied(port)
        owner = storage.create({'ALLURE_S3_USE_LOCALSTACK': True,
            'ALLURE_LOCALSTACK_PORT': port, 'ALLURE_S3_ENDPOINT': f'http://localhost:{port}'})
        owner.occupied = r._occupied
        with pytest.raises(p.PrerequisiteError, match='occupied and unregistered'):
            owner.acquire()
        assert storage.engine.commands == []


@pytest.mark.parametrize('error,ignored', [(r.errno.EAFNOSUPPORT, True),
    (r.errno.EPROTONOSUPPORT, True), (r.errno.EACCES, False)])
def test_occupancy_skips_only_unavailable_address_families(monkeypatch, error, ignored):
    from contextlib import nullcontext
    original = r.socket.socket
    def socket(family, kind):
        if family == r.socket.AF_INET6:
            raise OSError(error, 'synthetic socket error')
        return original(family, kind)
    monkeypatch.setattr(r.socket, 'socket', socket)
    with original() as temporary:
        temporary.bind(('127.0.0.1', 0))
        port = temporary.getsockname()[1]
        expected = nullcontext() if ignored else pytest.raises(OSError)
        with expected:
            assert not r._occupied(port)
