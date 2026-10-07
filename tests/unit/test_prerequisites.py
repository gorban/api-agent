import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.runtime.compose import plan_compose
from api_agent.runtime.ownership import StackScope
import api_agent.runtime.prerequisites as p


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def sleep(self, duration): self.value += duration


class Engine:
    def __init__(self):
        self.records = {}
        self.commands = []
        self.fail_up = False
        self.cancel = False
        self.owner = None
        self.counter = 0
        self.health = None
        self.fail_stop = False
        self.fail_hook = False

    def add(self, name, *, fingerprint=None, image=None, running=True, foreign=False, profile=None):
        self.counter += 1
        identifier = f'{self.counter:064x}'
        owner = self.owner
        labels = {**owner.scope.labels(profile or owner.specs[name]['profiles'][0]),
            'com.docker.compose.project': owner.scope.project,
            'com.docker.compose.service': name,
            p.CONFIG_LABEL: fingerprint or owner.fingerprints[name]}
        if foreign: labels['api-agent.scope'] = 'f' * 16
        record = {'Id': identifier, 'Config': {'Image': image or owner.specs[name]['image'], 'Labels': labels},
                  'State': {'Running': running}}
        if self.health: record['State']['Health'] = {'Status': self.health}
        self.records[identifier] = record
        return identifier

    def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[1] == 'ps': return '\n'.join(self.records)
        if command[1] == 'inspect': return json.dumps([self.records[item] for item in command[2:]])
        if command[1] == 'stop':
            if self.fail_stop: raise p.PrerequisiteError('stop failed')
            for identifier in command[4:]: self.records[identifier]['State']['Running'] = False
            return ''
        if command[1] == 'exec':
            if self.fail_hook: raise p.PrerequisiteError('bucket hook failed')
            return ''
        if 'up' in command:
            names = command[command.index('--no-recreate') + 1:]
            for name in names:
                present = next((record for record in self.records.values()
                    if record['Config']['Labels']['com.docker.compose.service'] == name), None)
                if present:
                    present['State']['Running'] = True
                else:
                    self.add(name)
                if self.fail_up: raise p.PrerequisiteError('up failed')
            return ''
        raise AssertionError(command)


@pytest.fixture
def setup(tmp_path):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    clock, engine = Clock(), Engine()
    def create(config=None, **kwargs):
        manager = p.PrerequisiteManager(plan_compose(config or {'TRACE_MODE': 'OTEL'}, scope), scope,
            tmp_path / 'artifacts' / 'prerequisites', command_runner=engine.run,
            cancelled=lambda: engine.cancel, readiness_probe=lambda *args: True,
            now=clock.now, sleep=clock.sleep, identity=lambda pid: 'boot:start',
            **kwargs)
        engine.owner = manager
        return manager
    return create, engine, clock, scope, tmp_path


def test_start_lease_and_local_cleanup(setup):
    create, engine, _, scope, tmp_path = setup
    manager = create(engine_environment={'PATH': '/usr/local/bin:/usr/bin', 'DOCKER_HOST': 'unix:///test.sock'})
    lease = manager.acquire()
    assert lease.owned_ids and not lease.reused_ids
    assert manager.acquire() is lease
    journal = json.loads((manager.directory / f'{scope.id}.json').read_text())
    record = journal[lease.invocation_id]
    assert record['owned_ids'] == list(lease.owned_ids)
    assert record['reused_ids'] == []
    assert record['fingerprints'] == manager.fingerprints
    assert p.active_leased_ids(manager.directory, scope, identity=lambda pid: 'boot:start') == set(lease.container_ids)
    up = next(command for command, _ in engine.commands if 'up' in command)
    assert up[-4:] == ('up', '-d', '--no-recreate', 'jaeger')
    assert '--no-deps' not in up
    assert 'down' not in up
    assert manager.release() == lease.owned_ids
    assert manager.release() == ()
    assert not json.loads((manager.directory / f'{scope.id}.json').read_text())
    stop = next(command for command, _ in engine.commands if command[1] == 'stop')
    assert stop[2:] == ('--time', '5', *lease.owned_ids)
    assert all(kwargs['environment']['PATH'] == '/usr/local/bin:/usr/bin' for _, kwargs in engine.commands)


def test_reuse_exact_existing_and_retained_tests(setup):
    create, engine, _, _, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    lease = manager.acquire()
    assert lease.reused_ids == (identifier,) and lease.owned_ids == ()
    assert not any('up' in command for command, _ in engine.commands)
    assert manager.release(retain=True) == ()
    assert engine.records[identifier]['State']['Running']


def test_reused_services_never_stopped_on_local_release(setup):
    create, engine, _, _, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    manager.acquire()
    assert manager.release() == ()
    assert engine.records[identifier]['State']['Running']


def test_second_active_lease_protects_first_invocation_new_service(setup):
    create, engine, _, scope, _ = setup
    first = create()
    lease = first.acquire()
    second = create()
    second.acquire()
    assert first.release() == ()
    assert p.active_leased_ids(first.directory, scope, identity=lambda pid: 'boot:start') == set(lease.container_ids)
    assert second.release() == ()
    assert engine.records[lease.owned_ids[0]]['State']['Running']


@pytest.mark.parametrize('change', ['fingerprint', 'image', 'duplicate', 'profile'])
def test_incompatible_existing_stack_is_never_replaced(setup, change):
    create, engine, _, _, _ = setup
    manager = create()
    values = {'fingerprint': {'fingerprint': 'wrong'}, 'image': {'image': 'foreign:1'},
              'stopped': {'running': False}, 'profile': {'profile': 'langfuse'}}
    engine.add('jaeger', **values.get(change, {}))
    if change == 'duplicate': engine.add('jaeger')
    with pytest.raises(p.PrerequisiteError, match='incompatible'): manager.acquire()
    assert not any('up' in command or command[1] == 'stop' for command, _ in engine.commands)


def test_foreign_owned_project_is_refused(setup):
    create, engine, _, _, _ = setup
    manager = create()
    engine.add('jaeger', foreign=True)
    with pytest.raises(p.PrerequisiteError, match='foreign ownership'): manager.acquire()
    assert not any('up' in command for command, _ in engine.commands)


def test_partial_up_failure_rolls_back_only_new_ids(setup):
    create, engine, _, _, _ = setup
    manager = create({'TRACE_MODE': 'OTEL', 'ALLURE_S3_ENDPOINT': 'http://localhost:4566'})
    reused = engine.add('jaeger')
    engine.fail_up = True
    with pytest.raises(p.PrerequisiteError, match='up failed'): manager.acquire()
    stopped = next(command for command, _ in engine.commands if command[1] == 'stop')
    assert reused not in stopped
    assert len(stopped[4:]) == 1
    assert engine.records[reused]['State']['Running']
    assert not engine.records[stopped[4]]['State']['Running']


def test_cancellation_after_up_rolls_back_even_with_cancelled_signal(setup):
    create, engine, _, _, _ = setup
    manager = create()
    original = engine.run
    def run(command, **kwargs):
        output = original(command, **kwargs)
        if 'up' in command: engine.cancel = True
        return output
    manager.runner = run
    with pytest.raises(p.PrerequisiteCancelled): manager.acquire()
    assert any(command[1] == 'stop' for command, _ in engine.commands)
    assert not any(record['State']['Running'] for record in engine.records.values())
    assert all(not kwargs['cancelled']() for command, kwargs in engine.commands if command[1] == 'stop')


def test_readiness_deadline_rolls_back_new_services(setup):
    create, engine, clock, _, _ = setup
    manager = create(deadline_seconds=.2)
    manager.probe = lambda *args: False
    with pytest.raises(p.PrerequisiteError, match='deadline'): manager.acquire()
    assert clock.value >= .2
    assert any(command[1] == 'stop' for command, _ in engine.commands)


def test_health_wait_then_ready(setup):
    create, engine, clock, _, _ = setup
    manager = create()
    engine.health = 'starting'
    def sleep(duration):
        clock.sleep(duration)
        for record in engine.records.values(): record['State']['Health']['Status'] = 'healthy'
    manager.sleep = sleep
    assert manager.acquire().owned_ids
    assert clock.value > 0


def test_no_profile_does_not_query_engine(setup):
    create, engine, _, _, _ = setup
    manager = create({'TRACE_MODE': ''})
    assert not manager.acquire().container_ids
    manager.release()
    assert not engine.commands


def test_stop_failure_preserves_active_lease(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    lease = manager.acquire()
    engine.fail_stop = True
    with pytest.raises(p.PrerequisiteError, match='stop failed'): manager.release()
    assert p.active_leased_ids(manager.directory, scope, identity=lambda pid: 'boot:start') == set(lease.container_ids)


def test_disappeared_new_container_is_not_stopped(setup):
    create, engine, _, _, _ = setup
    manager = create()
    manager.acquire()
    engine.records.clear()
    assert manager.release() == ()


def test_stale_lease_pid_reuse_does_not_protect_container(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    lease = manager.acquire()
    path = manager.directory / f'{scope.id}.json'
    journal = json.loads(path.read_text())
    journal['stale'] = {'pid': 98765, 'identity': 'boot:old-start', 'ids': list(lease.container_ids)}
    path.write_text(json.dumps(journal))
    assert manager.release() == lease.owned_ids


@pytest.mark.parametrize('value', ['not json', '[]', '{"a": {"pid": "bad"}}',
    '{"a":{"pid":1,"identity":"start","ids":["bad"]}}'])
def test_invalid_journal_fails_closed(setup, value):
    create, engine, _, scope, _ = setup
    manager = create()
    manager.directory.mkdir(parents=True)
    (manager.directory / f'{scope.id}.json').write_text(value)
    with pytest.raises(p.PrerequisiteError): manager.acquire()
    assert not engine.commands


@pytest.mark.parametrize('kind', ['lock', 'json', 'override'])
def test_symlink_artifacts_refused(setup, kind):
    create, engine, _, scope, tmp_path = setup
    manager = create()
    manager.directory.mkdir(parents=True)
    victim = tmp_path / 'victim'
    victim.write_text('do not overwrite')
    name = f'{scope.id}-labels.json' if kind == 'override' else f'{scope.id}.{kind}'
    (manager.directory / name).symlink_to(victim)
    with pytest.raises(p.PrerequisiteError, match='symlink'): manager.acquire()
    assert victim.read_text() == 'do not overwrite'


@pytest.mark.parametrize('failure', ['invalid_id', 'invalid_json', 'count', 'duplicate', 'missing'])
def test_invalid_inspection_refused(setup, failure):
    create, engine, _, _, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    original = engine.run
    def run(command, **kwargs):
        if failure == 'invalid_id' and command[1] == 'ps': return '-unsafe'
        if command[1] == 'inspect':
            if failure == 'invalid_json': return 'invalid'
            if failure == 'count': return '{}'
            record = deepcopy(engine.records[identifier])
            if failure == 'missing': del record['Config']
            if failure == 'duplicate': record['Id'] = 'f' * 64
            return json.dumps([record])
        return original(command, **kwargs)
    manager.runner = run
    with pytest.raises(p.PrerequisiteError): manager.acquire()


def test_unselected_owned_service_is_untouched(setup):
    create, engine, _, _, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    record = engine.records[identifier]
    record['Config']['Labels']['com.docker.compose.service'] = 'localstack'
    manager.acquire()
    manager.release()
    assert record['State']['Running']


def test_initialization_identity_and_deadline_checks(setup):
    create, _, _, scope, tmp_path = setup
    with pytest.raises(p.PrerequisiteError, match='deadline'): create(deadline_seconds=0)
    with pytest.raises(p.PrerequisiteError, match='ownership'):
        p.PrerequisiteManager(plan_compose({}, scope), StackScope('f' * 16), tmp_path)


def test_missing_invocation_identity_fails_before_engine_query(setup):
    create, engine, _, _, _ = setup
    manager = create()
    manager.identity = lambda pid: None
    with pytest.raises(p.PrerequisiteError, match='process identity'): manager.acquire()
    assert not engine.commands


def test_bounded_cli_success_and_error_redaction(tmp_path):
    args = dict(environment={'PATH': '/usr/bin', 'PRIVATE_VALUE': 'synthetic-sensitive-marker'},
        directory=tmp_path, deadline=p.time.monotonic() + 5, cancelled=lambda: False)
    assert p.run_command((sys.executable, '-c', 'print("safe inventory")'), **args).strip() == 'safe inventory'
    with pytest.raises(p.PrerequisiteError, match='exit 3') as error:
        p.run_command((sys.executable, '-c', 'import os,sys;print(os.environ["PRIVATE_VALUE"],file=sys.stderr);sys.exit(3)'), **args)
    assert 'synthetic-sensitive-marker' not in str(error.value)
    with pytest.raises(p.PrerequisiteError, match='safe bound'):
        p.run_command((sys.executable, '-c', 'print("x" * (8 * 1024 * 1024 + 1))'), **args)


def test_bounded_cli_timeout_kills_group_and_descendant(tmp_path):
    pidfile = tmp_path / 'child.pid'
    code = ('import subprocess,sys,time;'
        'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);'
        'open(sys.argv[1],"w").write(str(child.pid));time.sleep(30)')
    with pytest.raises(p.PrerequisiteError, match='deadline'):
        p.run_command((sys.executable, '-c', code, str(pidfile)), environment={}, directory=tmp_path,
                      deadline=p.time.monotonic() + .25, cancelled=lambda: False)
    child = int(pidfile.read_text())
    stat = Path(f'/proc/{child}/stat')
    assert not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'


def test_bounded_cli_cancellation(tmp_path):
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks > 2
    with pytest.raises(p.PrerequisiteCancelled):
        p.run_command((sys.executable, '-c', 'import time;time.sleep(30)'), environment={},
            directory=tmp_path, deadline=p.time.monotonic() + 5, cancelled=cancelled)


def test_cli_unavailable_and_cleanup_failure(tmp_path, monkeypatch):
    args = dict(environment={}, directory=tmp_path, deadline=p.time.monotonic() + 5, cancelled=lambda: False)
    with pytest.raises(p.PrerequisiteError, match='unavailable'):
        p.run_command(('/does-not-exist-cli',), **args)
    def failed_cleanup(process, **kwargs):
        process.wait(timeout=5)
        raise RuntimeError('sensitive')
    monkeypatch.setattr(p, 'finalize', failed_cleanup)
    with pytest.raises(p.PrerequisiteError, match='cleanup failed'):
        p.run_command((sys.executable, '-c', 'pass'), **args)


def test_process_identity_linux_dead_and_malformed(monkeypatch):
    assert p.process_identity(os.getpid())
    assert p.process_identity(999999999) is None
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).endswith('/stat'): return '123 (name) ' + ' '.join(['Z'] + ['1'] * 19)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert p.process_identity(123) is None
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: 'malformed')
    with pytest.raises(p.PrerequisiteError): p.process_identity(123)


@pytest.mark.parametrize('case', ['alive', 'missing', 'zombie', 'boot_error', 'oserror', 'timeout', 'unsupported', 'ps_permission', 'ps_empty'])
def test_process_identity_mac_and_unknown_fail_closed(monkeypatch, case):
    monkeypatch.setattr(Path, 'is_file', lambda self: False)
    monkeypatch.setattr(p.sys, 'platform', 'unknown' if case == 'unsupported' else 'darwin')
    def run(command, **kwargs):
        assert kwargs['timeout'] == 1
        if case == 'oserror': raise OSError('private')
        if case == 'timeout': raise subprocess.TimeoutExpired(command, 1)
        if command[0].endswith('sysctl'):
            return SimpleNamespace(returncode=1 if case == 'boot_error' else 0, stdout='{ sec = 123, usec = 456 }')
        return SimpleNamespace(returncode=1 if case in {'missing', 'ps_permission'} else 0, stderr='permission denied' if case == 'ps_permission' else '',
            stdout='' if case in {'missing', 'ps_permission', 'ps_empty'} else 'Mon Oct 5 12:00:00 2026 ' + ('Z' if case == 'zombie' else 'S'))
    monkeypatch.setattr(p.subprocess, 'run', run)
    if case in {'boot_error', 'oserror', 'timeout', 'unsupported', 'ps_permission', 'ps_empty'}:
        with pytest.raises(p.PrerequisiteError): p.process_identity(123)
    elif case in {'missing', 'zombie'}:
        assert p.process_identity(123) is None
    else:
        assert p.process_identity(123) == '{ sec = 123, usec = 456 }:Mon Oct 5 12:00:00 2026'


def test_lock_contention_waits_boundedly(setup, monkeypatch):
    create, _, clock, _, _ = setup
    manager = create({'TRACE_MODE': ''})
    calls = []
    def flock(stream, operation):
        calls.append(operation)
        if len(calls) == 1: raise BlockingIOError()
    monkeypatch.setattr(p.fcntl, 'flock', flock)
    manager.acquire()
    assert clock.value == .05
    assert calls == [p.fcntl.LOCK_EX | p.fcntl.LOCK_NB,
                     p.fcntl.LOCK_EX | p.fcntl.LOCK_NB, p.fcntl.LOCK_UN]


def test_journal_read_and_write_io_errors(setup, monkeypatch):
    create, _, _, scope, _ = setup
    manager = create()
    manager.directory.mkdir(parents=True)
    def fail(*args, **kwargs): raise PermissionError('private')
    monkeypatch.setattr(Path, 'read_text', fail)
    with pytest.raises(p.PrerequisiteError, match='read ownership'):
        p._read_journal(manager.directory, scope, lambda pid: 'boot')
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(p.PrerequisiteError, match='persist ownership'):
        p._write_journal(manager.directory, scope, {})


@pytest.mark.parametrize('profile,status,body,expected', [
    ('langfuse', 200, {'data': [{'id': 'api-agent-testscope'}]}, True),
    ('langfuse', 200, {'data': [{'id': 'other-project'}]}, False),
    ('langfuse', 401, {}, False),
    ('otel', 200, {}, True), ('otel', 503, {}, False),
    ('localstack', 200, {'services': {'s3': 'available'}}, True),
    ('localstack', 200, {'services': {'s3': 'running'}}, True),
    ('localstack', 200, {'services': {'s3': 'starting'}}, False)])
def test_readiness_authenticates_headless_project_and_actual_endpoints(monkeypatch, profile, status, body, expected):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 65536
            return json.dumps(body).encode()
    response = Response()
    response.status = status
    def open_url(request, **kwargs):
        calls.append((request, kwargs))
        if request.full_url.endswith('/init/ready'):
            class Ready(Response):
                status = 200
                def read(self, limit):
                    assert limit == 65536
                    return json.dumps({'completed': True, 'scripts': [
                        {'name': '10-api-agent-s3.sh', 'state': 'SUCCESSFUL'}]}).encode()
            return Ready()
        return response
    monkeypatch.setattr(p, 'urlopen', open_url)
    environment = {'SA_SCOPE': 'testscope', 'SA_LANGFUSE_PUBLIC_KEY': 'pk-local', 'SA_LANGFUSE_SECRET_KEY': 'sk-local'}
    assert p._readiness(profile, .5, environment) is expected
    request, kwargs = calls[0]
    assert kwargs == {'timeout': .5}
    if profile == 'langfuse':
        assert request.full_url == 'http://127.0.0.1:3000/api/public/projects'
        assert base64.b64decode(request.headers['Authorization'].split()[1]) == b'pk-local:sk-local'
    elif profile == 'otel':
        assert request.full_url == 'http://127.0.0.1:4318/v1/traces'
        assert json.loads(request.data) == {'resourceSpans': []}
        assert request.get_method() == 'POST'
    else:
        assert request.full_url == 'http://127.0.0.1:4566/_localstack/health'


def test_readiness_connection_and_invalid_json_errors(monkeypatch):
    def fail(*args, **kwargs): raise p.URLError('private')
    monkeypatch.setattr(p, 'urlopen', fail)
    assert not p._readiness('localstack', .1, {})


def test_interpolation_leaves_nonstring_values_and_literals_unchanged():
    assert p._interpolate({'list': [True, 7, 'literal']}, {}) == {'list': [True, 7, 'literal']}


def test_reused_identity_replacement_during_readiness_is_refused(setup):
    create, engine, _, _, _ = setup
    manager = create()
    old = engine.add('jaeger')
    def probe(*args):
        engine.records.pop(old)
        engine.add('jaeger')
        return False
    manager.probe = probe
    with pytest.raises(p.PrerequisiteError, match='changed during acquisition'):
        manager.acquire()
    assert not any(command[1] == 'stop' for command, _ in engine.commands)
    assert all(record['State']['Running'] for record in engine.records.values())


def test_guarded_cleanup_holds_lock_during_stop_check(setup):
    create, _, _, scope, _ = setup
    manager = create()
    lease = manager.acquire()
    with p.guarded_cleanup(manager.directory, scope, identity=lambda pid: 'boot:start') as active:
        assert active == set(lease.container_ids)
        with (manager.directory / f'{scope.id}.lock').open('a') as concurrent:
            with pytest.raises(BlockingIOError):
                p.fcntl.flock(concurrent, p.fcntl.LOCK_EX | p.fcntl.LOCK_NB)
    with (manager.directory / f'{scope.id}.lock').open('a') as concurrent:
        p.fcntl.flock(concurrent, p.fcntl.LOCK_EX | p.fcntl.LOCK_NB)
        p.fcntl.flock(concurrent, p.fcntl.LOCK_UN)


def test_cli_closed_pipes_still_obeys_deadline(tmp_path):
    with pytest.raises(p.PrerequisiteError, match='deadline'):
        p.run_command((sys.executable, '-c', 'import os,time;os.close(1);os.close(2);time.sleep(30)'),
            environment={}, directory=tmp_path, deadline=p.time.monotonic() + .25, cancelled=lambda: False)


def test_cli_stderr_is_bounded_while_writer_is_still_running(tmp_path):
    with pytest.raises(p.PrerequisiteError, match='safe bound'):
        p.run_command((sys.executable, '-c', 'import os,time;os.write(2,b"x" * (9 * 1024 * 1024));time.sleep(30)'),
            environment={}, directory=tmp_path, deadline=p.time.monotonic() + 5, cancelled=lambda: False)


@pytest.mark.parametrize('child_exits_without_release', [False, True])
def test_cross_process_leases_with_real_controlled_fake_cli(tmp_path, child_exits_without_release):
    """Exercise the real adapter, journal lock, birth identity, and a second PID."""
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    state_path, metadata_path = tmp_path / 'fake-engine.json', tmp_path / 'fake-metadata.json'
    state_path.write_text('{}')
    executable = tmp_path / 'fake-engine'
    script = '''import json,sys
from pathlib import Path
state_path=Path(STATE_PATH)
metadata_path=Path(METADATA_PATH)
records=json.loads(state_path.read_text())
args=sys.argv[1:]
if args[0]=='ps':
 print('\\n'.join(records))
elif args[0]=='inspect':
 print(json.dumps([records[item] for item in args[1:]]))
elif args[0]=='stop':
 for identifier in args[3:]: records[identifier]['State']['Running']=False
 state_path.write_text(json.dumps(records))
elif args[0]=='compose' and 'up' in args:
 metadata=json.loads(metadata_path.read_text())
 for name in args[args.index('--no-recreate')+1:]:
  identifier=f'{len(records)+1:064x}'
  records[identifier]={'Id':identifier,'Config':metadata[name],'State':{'Running':True}}
 state_path.write_text(json.dumps(records))
else:
 sys.exit(9)
'''.replace('STATE_PATH', repr(str(state_path))).replace('METADATA_PATH', repr(str(metadata_path)))
    executable.write_text('#!' + sys.executable + '\n' + script)
    executable.chmod(0o755)
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, scope, executable=str(executable))
    ownership = tmp_path / 'artifacts' / 'prerequisites'
    manager = p.PrerequisiteManager(plan, scope, ownership, readiness_probe=lambda *args: True)
    metadata_path.write_text(json.dumps({name: {'Image': spec['image'], 'Labels': {
        **scope.labels(spec['profiles'][0]), 'com.docker.compose.project': scope.project,
        'com.docker.compose.service': name, p.CONFIG_LABEL: manager.fingerprints[name]}}
        for name, spec in manager.specs.items()}))
    lease = manager.acquire()
    ready, release = tmp_path / 'child.ready', tmp_path / 'child.release'
    child_code = '''import os,sys,time
from pathlib import Path
from api_agent.runtime.compose import plan_compose
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.prerequisites import PrerequisiteManager
scope=StackScope(sys.argv[1])
manager=PrerequisiteManager(plan_compose({'TRACE_MODE':'OTEL'},scope,executable=sys.argv[2]),scope,
                           sys.argv[3],readiness_probe=lambda *args:True)
lease=manager.acquire()
Path(sys.argv[4]).write_text(str(os.getpid()))
if sys.argv[6]=='crash':os._exit(0)
deadline=time.monotonic()+10
while not Path(sys.argv[5]).exists():
 if time.monotonic()>deadline:raise RuntimeError('test release deadline')
 time.sleep(.02)
manager.release()
'''
    child = subprocess.Popen((sys.executable, '-c', child_code, scope.id, str(executable),
        str(ownership), str(ready), str(release), 'crash' if child_exits_without_release else 'release'),
        env={'PYTHONPATH': str(Path(p.__file__).resolve().parents[2]), 'PATH': '/usr/bin'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = p.time.monotonic() + 5
        while not ready.exists():
            assert child.poll() is None
            assert p.time.monotonic() < deadline
            p.time.sleep(.02)
        if child_exits_without_release:
            assert child.wait(timeout=5) == 0
            assert manager.release() == lease.owned_ids
            assert not json.loads(state_path.read_text())[lease.owned_ids[0]]['State']['Running']
        else:
            journal = json.loads((ownership / f'{scope.id}.json').read_text())
            assert len(journal) == 2
            assert {record['pid'] for record in journal.values()} == {os.getpid(), child.pid}
            assert manager.release() == ()
            assert json.loads(state_path.read_text())[lease.owned_ids[0]]['State']['Running']
            release.write_text('release')
            assert child.wait(timeout=5) == 0
        assert not p.active_leased_ids(ownership, scope)
    finally:
        p.finalize(child)


def test_repeated_local_run_restarts_exact_stopped_owned_id(setup):
    create, engine, _, _, _ = setup
    first = create()
    lease = first.acquire()
    assert first.release() == lease.owned_ids
    second = create()
    repeat = second.acquire()
    assert repeat.owned_ids == lease.owned_ids
    assert repeat.reused_ids == ()
    assert engine.records[repeat.owned_ids[0]]['State']['Running']
    assert second.release() == repeat.owned_ids


def test_failed_restart_rolls_back_restarted_id_and_preserves_reused(setup):
    create, engine, _, _, _ = setup
    manager = create({'TRACE_MODE': 'OTEL', 'ALLURE_S3_ENDPOINT': 'http://localhost:4566'})
    reused = engine.add('jaeger')
    restarted = engine.add('localstack', running=False)
    engine.fail_up = True
    with pytest.raises(p.PrerequisiteError, match='up failed'): manager.acquire()
    stop = next(command for command, _ in engine.commands if command[1] == 'stop')
    assert stop[4:] == (restarted,)
    assert engine.records[reused]['State']['Running']
    assert not engine.records[restarted]['State']['Running']


def test_stopped_but_actively_leased_container_cannot_restart(setup):
    create, engine, _, _, _ = setup
    first = create()
    lease = first.acquire()
    engine.records[lease.owned_ids[0]]['State']['Running'] = False
    second = create()
    with pytest.raises(p.PrerequisiteError, match='active invocation lease'): second.acquire()
    assert len([command for command, _ in engine.commands if 'up' in command]) == 1


@pytest.mark.parametrize('case', ['stat_permission', 'stat_io', 'stat_missing_live_pid', 'malformed_ticks', 'empty_fields', 'boot_permission', 'boot_empty'])
def test_linux_identity_uncertainty_refuses_stale_cleanup(monkeypatch, case):
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).endswith('/stat'):
            if case == 'stat_permission': raise PermissionError('private')
            if case == 'stat_io': raise OSError('private')
            if case == 'stat_missing_live_pid': raise FileNotFoundError('private')
            if case == 'empty_fields': return '123 (name) '
            return '123 (name) ' + ' '.join(['S'] + ['1'] * 18 + ['bad' if case == 'malformed_ticks' else '456'])
        if str(path).endswith('/boot_id'):
            if case == 'boot_permission': raise PermissionError('private')
            if case == 'boot_empty': return ''
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(p.PrerequisiteError): p.process_identity(os.getpid())


def test_uncertain_process_identity_does_not_allow_cleanup(setup):
    create, _, _, scope, _ = setup
    manager = create()
    manager.acquire()
    def uncertain(pid): raise p.PrerequisiteError('Cannot verify process.')
    with pytest.raises(p.PrerequisiteError):
        with p.guarded_cleanup(manager.directory, scope, identity=uncertain):
            pytest.fail('Uncertain identity must not authorize cleanup')


def test_hidden_missing_proc_stat_does_not_prove_process_dead(monkeypatch):
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).endswith('/stat'): raise FileNotFoundError('hidden process')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(p.os, 'kill', lambda *args: None)
    with pytest.raises(p.PrerequisiteError, match='identity'): p.process_identity(123)
    def denied(*args): raise PermissionError('private')
    monkeypatch.setattr(p.os, 'kill', denied)
    with pytest.raises(p.PrerequisiteError, match='existence'): p.process_identity(123)


def test_readiness_does_not_redirect_authentication_to_another_host():
    with pytest.raises(p.URLError, match='may not redirect'):
        p._NoReadinessRedirect().redirect_request(None, None, 302, 'redirect', {}, 'http://remote.example')


def test_owned_readiness_transport_ignores_proxy_and_refuses_local_redirect(tmp_path, monkeypatch):
    """A controlled loopback server proves credentials never reach the proxy/redirect."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    target_calls, forbidden_calls = [], []
    class Forbidden(BaseHTTPRequestHandler):
        def do_GET(self):
            forbidden_calls.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args): pass
    trap = ThreadingHTTPServer(('127.0.0.1', 0), Forbidden)
    trap_url = f'http://127.0.0.1:{trap.server_port}'
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            target_calls.append(self.headers.get('Authorization'))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', trap_url + '/must-not-receive-auth')
            else:
                self.send_response(200)
            self.end_headers()
        def log_message(self, *args): pass
    target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
    threads = [Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
               for server in (trap, target)]
    for thread in threads: thread.start()
    try:
        monkeypatch.setenv('http_proxy', trap_url)
        monkeypatch.setenv('HTTP_PROXY', trap_url)
        monkeypatch.setenv('no_proxy', '')
        monkeypatch.setenv('NO_PROXY', '')
        headers = {'Authorization': 'Basic synthetic-local-development'}
        url = f'http://127.0.0.1:{target.server_port}'
        with p.urlopen(p.Request(url + '/ready', headers=headers), timeout=1) as response:
            assert response.status == 200
        with pytest.raises(p.URLError, match='may not redirect'):
            p.urlopen(p.Request(url + '/redirect', headers=headers), timeout=1)
        assert target_calls == [headers['Authorization'], headers['Authorization']]
        assert forbidden_calls == []
    finally:
        for server in (trap, target):
            server.shutdown()
            server.server_close()
        for thread in threads: thread.join(timeout=1)


def test_localstack_selected_bucket_is_provisioned_on_new_and_reused_stack(setup):
    create, engine, _, _, _ = setup
    config = {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'selected-results', 'ALLURE_S3_REGION': 'eu-west-1'}
    manager = create(config)
    lease = manager.acquire()
    hook = next(command for command, _ in engine.commands if command[1] == 'exec')
    assert hook == ('docker', 'exec', lease.owned_ids[0], '/bin/sh', '/etc/localstack/init/ready.d/10-api-agent-s3.sh')
    assert manager.environment['SA_ALLURE_S3_BUCKET'] == 'selected-results'
    manager.release(retain=True)
    reused = create(config)
    assert reused.acquire().reused_ids == lease.owned_ids
    assert len([command for command, _ in engine.commands if command[1] == 'exec']) == 2


def test_bucket_provision_failure_rolls_back_only_new_services(setup):
    create, engine, _, _, _ = setup
    manager = create({'TRACE_MODE': 'OTEL', 'ALLURE_S3_USE_LOCALSTACK': True})
    existing = engine.add('jaeger')
    engine.fail_hook = True
    with pytest.raises(p.PrerequisiteError, match='bucket hook failed'): manager.acquire()
    stop = next(command for command, _ in engine.commands if command[1] == 'stop')
    assert existing not in stop
    assert engine.records[existing]['State']['Running']


def test_selected_bucket_and_hook_change_prevent_unsafe_reuse(setup, monkeypatch):
    create, engine, _, _, _ = setup
    first = create({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'first-results'})
    first.acquire()
    first.release(retain=True)
    changed = create({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'other-results'})
    with pytest.raises(p.PrerequisiteError, match='incompatible'): changed.acquire()
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) + (b'\n# changed hook contract\n' if path.name == 'localstack-ready.sh' else b''))
    changed_hook = create({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'first-results'})
    with pytest.raises(p.PrerequisiteError, match='incompatible'): changed_hook.acquire()


@pytest.mark.parametrize('exists,region', [(False, 'us-east-1'), (True, 'us-east-1'), (False, 'eu-west-1')])
def test_ready_hook_idempotently_creates_exact_bucket_with_controlled_awslocal(tmp_path, exists, region):
    _exercise_ready_hook(tmp_path, exists=exists, region=region)


def _exercise_ready_hook(tmp_path, *, exists=False, region='us-east-1', failure=None):
    state, log = tmp_path / 'bucket-state.json', tmp_path / 'awslocal-calls.json'
    state.write_text(json.dumps({'exists': exists}))
    log.write_text('[]')
    fake = tmp_path / 'awslocal'
    fake.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
state=Path(STATE)
log=Path(LOG)
failure=FAILURE
current=json.loads(state.read_text())
calls=json.loads(log.read_text())
args=sys.argv[1:]
calls.append({'args':args,'access':os.environ['AWS_ACCESS_KEY_ID'],'secret':os.environ['AWS_SECRET_ACCESS_KEY'],
              'token':os.environ['AWS_SESSION_TOKEN']})
log.write_text(json.dumps(calls))
if 'head-bucket' in args:
 sys.exit(0 if current['exists'] and failure!='head-after-create' else 1)
if 'create-bucket' in args:
 if failure=='create':
  current['exists']=True
  state.write_text(json.dumps(current))
  sys.exit(5)
 current['exists']=True
 state.write_text(json.dumps(current))
 sys.exit(0)
sys.exit(9)
'''.replace('STATE', repr(str(state))).replace('LOG', repr(str(log))).replace('FAILURE', repr(failure)))
    fake.chmod(0o755)
    hook = plan_compose({'ALLURE_S3_USE_LOCALSTACK': True}, StackScope('a' * 16)).directory / 'localstack-ready.sh'
    environment = {'PATH': str(tmp_path) + ':/usr/bin:/bin', 'SA_ALLURE_S3_BUCKET': 'chosen-results',
                   'SA_ALLURE_S3_REGION': region, 'AWS_ACCESS_KEY_ID': 'ambient', 'AWS_SECRET_ACCESS_KEY': 'ambient',
                   'AWS_SESSION_TOKEN': 'ambient'}
    result = subprocess.run(('/bin/sh', str(hook)), env=environment, capture_output=True, text=True, timeout=5)
    calls = json.loads(log.read_text())
    assert calls
    assert all(call['args'][:4] == ['--endpoint-url', 'http://127.0.0.1:4566', '--region', region] for call in calls)
    assert all(call['args'][call['args'].index('--bucket') + 1] == 'chosen-results' for call in calls)
    assert all((call['access'], call['secret'], call['token']) == ('test', 'test', '') for call in calls)
    creates = [call['args'] for call in calls if 'create-bucket' in call['args']]
    if failure:
        assert result.returncode != 0
        if failure == 'create': assert len(calls) == 2
        else: assert len(calls) == 3
        return
    assert result.returncode == 0
    assert len(creates) == (0 if exists else 1)
    if creates:
        assert ('--create-bucket-configuration' in creates[0]) == (region != 'us-east-1')
        if region != 'us-east-1': assert creates[0][-1] == 'LocationConstraint=' + region
    repeated = subprocess.run(('/bin/sh', str(hook)), env=environment, capture_output=True, text=True, timeout=5)
    assert repeated.returncode == 0
    assert len([call for call in json.loads(log.read_text()) if 'create-bucket' in call['args']]) == len(creates)


@pytest.mark.parametrize('failure', ['create', 'head-after-create'])
def test_ready_hook_propagates_create_and_final_readability_failures(tmp_path, failure):
    _exercise_ready_hook(tmp_path, failure=failure)


@pytest.mark.parametrize('status,stage,expected', [(200, {'completed': False}, False),
    (503, {}, False), (200, {'completed': True, 'scripts': []}, False),
    (200, {'completed': True, 'scripts': [{'name': '10-api-agent-s3.sh', 'state': 'ERROR'}]}, False),
    (200, {'completed': True, 'scripts': [{'name': 'other.sh', 'state': 'SUCCESSFUL'}]}, False),
    (200, {'completed': True, 'scripts': [{'name': '10-api-agent-s3.sh', 'state': 'SUCCESSFUL'}]}, True)])
def test_localstack_readiness_waits_for_automatic_selected_bucket_hook(monkeypatch, status, stage, expected):
    urls = []
    class Response:
        def __init__(self, status, body): self.status, self.body = status, body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == 65536
            return json.dumps(self.body).encode()
    def open_local(request, **kwargs):
        urls.append(request.full_url)
        if request.full_url.endswith('/health'):
            return Response(200, {'services': {'s3': 'available'}})
        return Response(status, stage)
    monkeypatch.setattr(p, 'urlopen', open_local)
    assert p._readiness('localstack', .1, {}) is expected
    assert urls == ['http://127.0.0.1:4566/_localstack/health', 'http://127.0.0.1:4566/_localstack/init/ready']


@pytest.mark.parametrize('restart', [False, True])
def test_failed_journal_write_rolls_back_only_new_invocation(setup, monkeypatch, restart):
    create, engine, _, _, _ = setup
    first = create({'TRACE_MODE': 'OTEL'})
    prior = first.acquire()
    manager = create({'TRACE_MODE': 'OTEL', 'ALLURE_S3_USE_LOCALSTACK': True})
    restarted = engine.add('localstack', running=False) if restart else None
    def fail(*args):
        raise p.PrerequisiteError('journal persistence failed')
    monkeypatch.setattr(p, '_write_journal', fail)
    with pytest.raises(p.PrerequisiteError, match='journal persistence failed'):
        manager.acquire()
    assert manager.lease is None
    assert all(engine.records[item]['State']['Running'] for item in prior.container_ids)
    changed = [item for item in engine.records if item not in prior.container_ids]
    assert changed and all(not engine.records[item]['State']['Running'] for item in changed)
    if restart:
        assert changed == [restarted]
    journal = json.loads((first.directory / f'{first.scope.id}.json').read_text())
    assert set(journal) == {prior.invocation_id}


@pytest.mark.parametrize('change_at', ['plan', 'manager'])
def test_execution_uses_reviewed_compose_and_hook_snapshot(setup, tmp_path, change_at):
    create, engine, clock, scope, _ = setup
    source = plan_compose({}, scope)
    assets = tmp_path / 'assets'; assets.mkdir()
    for name, content in source.assets.items():
        (assets / name).write_bytes(content)
    plan = plan_compose({'ALLURE_S3_USE_LOCALSTACK': True, 'TRACE_MODE': 'OTEL'}, scope,
                        compose_directory=assets)
    def mutate():
        document = json.loads((assets / 'compose.json').read_text())
        document['services']['jaeger']['ports'] = ['0.0.0.0:4318:4318']
        (assets / 'compose.json').write_text(json.dumps(document))
        (assets / 'localstack-ready.sh').write_text('changed unsafe hook')
        (assets / 'empty.env').write_text('SA_SCOPE=foreign')
    if change_at == 'plan': mutate()
    manager = p.PrerequisiteManager(plan, scope, tmp_path / 'ownership', command_runner=engine.run,
        readiness_probe=lambda *args: True, now=clock.now, sleep=clock.sleep,
        identity=lambda pid: 'boot:start')
    engine.owner = manager
    fingerprints = dict(manager.fingerprints)
    if change_at == 'manager': mutate()
    observed = []
    original = engine.run
    def run(command, **kwargs):
        if 'up' in command:
            path = Path(command[command.index('-f') + 1])
            observed.append(path.read_bytes())
            assert path.read_bytes() == plan.assets['compose.json']
            hook = Path(kwargs['environment']['SA_LOCALSTACK_READY_HOOK'])
            assert hook.read_bytes() == plan.assets['localstack-ready.sh']
            assert hook.stat().st_mode & 0o111 == 0o111
            assert Path(command[command.index('--env-file') + 1]).read_bytes() == plan.assets['empty.env']
        return original(command, **kwargs)
    manager.runner = run
    manager.acquire()
    assert observed and manager.fingerprints == fingerprints
    manager.release(retain=True)
    assert Path(manager.environment['SA_LOCALSTACK_READY_HOOK']).read_bytes() == plan.assets['localstack-ready.sh']
    # A second manager can reuse the same reviewed plan despite a distinct snapshot path.
    second = p.PrerequisiteManager(plan, scope, manager.directory, command_runner=engine.run,
        readiness_probe=lambda *args: True, now=clock.now, sleep=clock.sleep, identity=lambda pid: 'boot:start')
    engine.owner = second
    assert second.acquire().reused_ids
    second.release(retain=True)


def test_previous_scope_journal_requires_explicit_migration(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    manager.directory.mkdir(parents=True)
    journal = manager.directory / f'{scope.legacy_ids[0]}.json'
    journal.write_text('{"old lease": "preserved"}')
    with pytest.raises(p.PrerequisiteError, match='before migration'):
        manager.acquire()
    assert not engine.commands
    assert journal.read_text() == '{"old lease": "preserved"}'


def test_previous_scope_container_requires_explicit_migration(setup):
    create, engine, _, scope, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    labels = engine.records[identifier]['Config']['Labels']
    labels['api-agent.scope'] = scope.legacy_ids[0]
    labels['com.docker.compose.project'] = 'sa-' + scope.legacy_ids[0]
    with pytest.raises(p.PrerequisiteError, match='before migration'):
        manager.acquire()
    assert engine.records[identifier]['State']['Running']
    assert not any('up' in command or command[1] == 'stop' for command, _ in engine.commands)

@pytest.mark.parametrize('response', ['-invalid', '{}'])
def test_current_scope_invalid_inventory_independent_of_legacy(setup, response):
    create, engine, _, _, _ = setup
    manager = create()
    manager.scope = StackScope(manager.scope.id)
    engine.add('jaeger')
    original = engine.run
    def run(command, **kwargs):
        if response == '-invalid' and command[1] == 'ps': return response
        if response == '{}' and command[1] == 'inspect': return response
        return original(command, **kwargs)
    manager.runner = run
    with pytest.raises(p.PrerequisiteError):
        manager.acquire()


def test_failed_start_retains_snapshot_for_stopped_definition(setup):
    create, engine, _, _, _ = setup
    manager = create()
    engine.fail_up = True
    with pytest.raises(p.PrerequisiteError, match='up failed'):
        manager.acquire()
    assert manager.snapshot_directory.is_dir()
    assert list(manager.directory.glob('*-assets-*'))


def test_final_owned_release_retains_snapshot_for_stopped_definition(setup):
    create, _, _, _, _ = setup
    manager = create()
    manager.acquire()
    directory = manager.snapshot_directory
    assert directory.is_dir()
    manager.release()
    assert manager.snapshot_directory is None and directory.is_dir()


def test_failed_command_without_created_definition_removes_unused_snapshot(setup):
    create, engine, _, _, _ = setup
    manager = create()
    original = engine.run
    def run(command, **kwargs):
        if 'up' in command:
            raise p.PrerequisiteError('before creation')
        return original(command, **kwargs)
    manager.runner = run
    with pytest.raises(p.PrerequisiteError, match='before creation'):
        manager.acquire()
    assert manager.snapshot_directory is None
    assert not list(manager.directory.glob('*-assets-*'))


@pytest.mark.parametrize('failed_start', [False, True])
def test_stopped_localstack_definition_restarts_with_original_hook(setup, failed_start):
    create, engine, _, _, _ = setup
    mounts = {}
    original = engine.run
    def run(command, **kwargs):
        if 'up' not in command:
            return original(command, **kwargs)
        try:
            return original(command, **kwargs)
        finally:
            for identifier, record in engine.records.items():
                if record['Config']['Labels']['com.docker.compose.service'] == 'localstack':
                    # Compose --no-recreate keeps the definition's first mount.
                    mounts.setdefault(identifier, Path(kwargs['environment']['SA_LOCALSTACK_READY_HOOK']))
                    assert mounts[identifier].is_file()
                    assert mounts[identifier].stat().st_mode & 0o111 == 0o111
    engine.run = run
    manager = create({'ALLURE_S3_USE_LOCALSTACK': True})
    if failed_start:
        engine.fail_up = True
        with pytest.raises(p.PrerequisiteError, match='up failed'):
            manager.acquire()
        engine.fail_up = False
    else:
        lease = manager.acquire()
        manager.release()
        assert all(not engine.records[item]['State']['Running'] for item in lease.container_ids)
    assert mounts and all(path.is_file() for path in mounts.values())
    original_mounts = dict(mounts)
    retry = create({'ALLURE_S3_USE_LOCALSTACK': True})
    retry.acquire()
    assert mounts == original_mounts
    retry.release()
    assert all(path.is_file() for path in mounts.values())
