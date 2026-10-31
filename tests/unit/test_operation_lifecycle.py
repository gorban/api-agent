from api_agent.providers.control import CleanupPendingError
import asyncio
from dataclasses import dataclass
import sys
from types import SimpleNamespace
import time

import pytest

from api_agent.runtime.operation import OperationRunner, callback_endpoint, local_operation
from api_agent.runtime.listener import ListenerError, UvicornListener


@dataclass
class Config:
    values: dict
    errors: tuple = ()
    missing: tuple = ()


@pytest.mark.parametrize('argv,environ,dotenv,expected,source', [
    ((), {}, {}, 'https://localhost:8443', 'shared_default'),
    ((), {}, {'AGENT_DJANGO_URL': 'https://saved.example:9443'}, 'https://saved.example:9443', 'dotenv'),
    ((), {'AGENT_DJANGO_URL': 'https://env.example:9444'},
     {'AGENT_DJANGO_URL': 'https://saved.example:9443'}, 'https://env.example:9444', 'environment'),
    (('--agent-django-url=https://cli.example:9445',), {'AGENT_DJANGO_URL': 'https://env.example:9444'},
     {'AGENT_DJANGO_URL': 'https://saved.example:9443'}, 'https://cli.example:9445', 'cli'),
])
def test_declared_backend_origin_drives_listener_with_explicit_precedence(tmp_path, argv, environ, dotenv, expected, source):
    from api_agent.config import load_shared_schema, resolve_config
    schema = load_shared_schema()
    assert 'API_LISTENER_URL' not in schema.fields
    config = resolve_config(schema, argv=('--tui-mode=unit', *argv), environ=environ,
                            dotenv=dotenv, anchors={'owner_root': tmp_path})
    config.raise_for_errors()
    assert config.values['AGENT_DJANGO_URL'] == expected
    assert config.fields['AGENT_DJANGO_URL'].source == source
    endpoint = callback_endpoint(config)
    assert endpoint.origin == expected
    assert endpoint.port == int(expected.rsplit(':', 1)[1])


@pytest.fixture
def rig():
    events = []
    config = Config({'AGENT_DJANGO_URL': 'https://agent.example:4200/custom/callback?tenant=one'})
    tls = type('TLS', (), {'material': object()})()

    class Manager:
        def acquire(self):
            events.append('acquire')
            return 'lease'

        def release(self, *, retain):
            events.append(f'release:{retain}')

    class Runtime:
        async def astart(self):
            events.append('runtime-start')

        async def aclose(self):
            events.append('runtime-close')

    class Listener:
        async def start(self, application, material, endpoint):
            assert application == 'application'
            assert material is tls.material
            assert endpoint.port == 4200
            events.append('listener-start')

        async def wait_ready(self, timeout):
            assert timeout == 5
            events.append('ready')

        async def stop(self):
            events.append('listener-stop')

    def resolved():
        events.append('resolve')
        return config

    def dependencies(value):
        assert value is config
        events.append('dependencies')

    def https(value, endpoint):
        assert endpoint.origin == 'https://agent.example:4200'
        events.append('tls')
        return tls

    def plan(value):
        events.append('plan')
        return 'plan'

    def manager(value, cancelled):
        assert value == 'plan'
        events.append('manager')
        return Manager()

    def compose(value, origin, material):
        assert origin == 'https://agent.example:4200'
        assert material is tls.material
        events.append('compose')
        return Runtime()

    def application(runtime, host):
        assert host == 'agent.example'
        events.append('application')
        return 'application'

    runner = OperationRunner(resolve=resolved, prepare_dependencies=dependencies,
        prepare_https=https, plan=plan, manager=manager,
        compose_runtime=compose, create_application=application,
        listener=Listener, timeout=5)
    return runner, events, config


def test_order_and_test_retention(rig):
    async def scenario():
        runner, events, _ = rig
        async with runner.open(retain=True) as session:
            assert session.lease == 'lease'
            assert session.callback.port == 4200
            events.append('work')
        assert events == ['resolve', 'dependencies', 'tls', 'plan', 'manager',
            'acquire', 'compose', 'runtime-start', 'application', 'listener-start',
            'ready', 'work', 'listener-stop', 'runtime-close', 'release:True']

    asyncio.run(scenario())


def test_operation_reports_fixed_startup_stages(rig):
    runner, _, _ = rig
    stages = []
    runner.on_stage = stages.append

    async def scenario():
        async with runner.open():
            pass

    asyncio.run(scenario())
    assert stages == ['configuration', 'dependencies', 'local HTTPS', 'operation plan',
                      'prerequisites', 'runtime', 'listener', 'listener ready']


def test_infrastructure_binding_runs_after_acquire_and_before_runtime(rig):
    from dataclasses import replace
    from api_agent.runtime.infrastructure_session import InfrastructureSession
    runner, events, original = rig
    bound = replace(original, values={**original.values, 'API_ENDPOINT': 'http://127.0.0.1:58092/graphql'})
    compose = runner.compose_runtime
    def bind(value, infrastructure):
        assert value is original
        assert isinstance(infrastructure, InfrastructureSession)
        assert events[-1] == 'acquire'
        events.append('bind')
        return bound
    def runtime(value, origin, material):
        assert value is bound
        assert events[-1] == 'bind'
        return compose(value, origin, material)
    runner.bind_infrastructure = bind
    runner.compose_runtime = runtime

    async def scenario():
        async with runner.open() as session:
            assert session.config is bound
            assert 'API_ENDPOINT' not in original.values
    asyncio.run(scenario())
    assert events.index('acquire') < events.index('bind') < events.index('compose')
    assert events[-1] == 'release:False'


@pytest.mark.parametrize('failure', ['errors', 'missing', 'listener', 'callback'])
def test_invalid_infrastructure_binding_releases_acquired_lease(rig, failure):
    from dataclasses import replace
    runner, events, config = rig
    def bind(value, infrastructure):
        if failure == 'callback':
            raise ValueError('binding_failed')
        if failure == 'listener':
            return replace(value, values={'AGENT_DJANGO_URL': 'https://other.example:8443'})
        return replace(value, **{failure: ('not ready',)})
    runner.bind_infrastructure = bind

    async def scenario():
        with pytest.raises(ValueError, match='binding_failed|invalid_bound_runtime_configuration'):
            async with runner.open():
                pytest.fail('invalid binding must not start the operation')
    asyncio.run(scenario())
    assert 'compose' not in events
    assert events[-1] == 'release:False'

def test_workload_failure_still_stops_run_local(rig):
    async def scenario():
        runner, events, _ = rig
        with pytest.raises(ZeroDivisionError):
            async with runner.open():
                1 / 0
        assert events[-3:] == ['listener-stop', 'runtime-close', 'release:False']

    asyncio.run(scenario())

def test_partial_listener_start_rolls_back(rig):
    async def scenario():
        runner, events, _ = rig
        class Broken:
            async def start(self, *args):
                raise RuntimeError('bind failed')
            async def stop(self):
                events.append('listener-stop')
        runner.listener = Broken
        with pytest.raises(RuntimeError, match='bind failed'):
            async with runner.open():
                pass
        assert events[-3:] == ['listener-stop', 'runtime-close', 'release:False']

    asyncio.run(scenario())

@pytest.mark.parametrize('uri', ['http://agent.example:4200/cb',
    'https://user@agent.example:4200/cb', 'https://agent.example:bad/cb',
    'https://agent.example:4200/cb#fragment'])
def test_callback_rejects_invalid_listener_authority(uri):
    with pytest.raises(ValueError, match='invalid_callback_uri'):
        callback_endpoint(Config({'AGENT_DJANGO_URL': uri}))


def test_invalid_config_prevents_preparation(rig):
    async def scenario():
        runner, events, config = rig
        config.errors = ('bad',)
        with pytest.raises(ValueError, match='invalid_runtime_configuration'):
            async with runner.open():
                pass
        assert events == ['resolve']

    asyncio.run(scenario())

def test_listener_contract_without_socket(monkeypatch):
    async def scenario():
        listener = UvicornListener()
        with pytest.raises(ListenerError, match='listener_not_started'):
            await listener.wait_ready(.01)
        await listener.stop()
        listener.task = asyncio.create_task(asyncio.sleep(0))
        listener.server = type('Server', (), {'started': False, 'should_exit': False})()
        with pytest.raises(ListenerError, match='listener_stopped_before_ready'):
            await listener.wait_ready(.1)
        await listener.stop()

    asyncio.run(scenario())


def test_listener_ready_timeout_and_start(monkeypatch):
    class Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(.001)
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=lambda *a, **kw: kw, Server=Server))
    async def scenario():
        listener = UvicornListener()
        endpoint = callback_endpoint(Config({'AGENT_DJANGO_URL': 'https://agent.example:4200/cb'}))
        material = SimpleNamespace(cert_file='cert', key_file='key')
        await listener.start('app', material, endpoint)
        assert listener.server.config['port'] == 4200
        assert listener.server.config['workers'] == 1
        with pytest.raises(ListenerError, match='listener_already_started'):
            await listener.start('app', material, endpoint)
        with pytest.raises(ListenerError, match='listener_readiness_timeout'):
            await listener.wait_ready(.001)
        listener.server.started = True
        await listener.wait_ready(.001)
        await listener.stop()
        assert listener.task is None
    asyncio.run(scenario())


@pytest.mark.parametrize(('number', 'expected'), [
    (__import__('errno').EADDRINUSE, 'listener_bind_in_use'),
    (__import__('errno').EADDRNOTAVAIL, 'listener_bind_unavailable'),
    (__import__('errno').EACCES, 'listener_bind_denied'),
    (__import__('errno').EINVAL, 'listener_start_failed'),
])
def test_listener_classifies_bind_context_without_os_text(monkeypatch, number, expected):
    class Server:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
        async def serve(self):
            try:
                raise OSError(number, 'synthetic sensitive path')
            except OSError:
                raise SystemExit(1)
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=lambda *a, **kw: kw, Server=Server))
    async def scenario():
        listener = UvicornListener()
        await listener.start('app', SimpleNamespace(cert_file='cert', key_file='key'), SimpleNamespace(port=1234))
        with pytest.raises(ListenerError, match=expected) as caught:
            await listener.wait_ready(2)
        assert 'synthetic sensitive path' not in str(caught.value)
        with pytest.raises(ListenerError):
            await listener.stop()
        assert listener.task is None
    asyncio.run(scenario())


def test_listener_context_walk_is_bounded_and_cycle_safe():
    import errno
    from api_agent.runtime.listener import _start_failure

    error = SystemExit(1)
    middle = RuntimeError('synthetic private text')
    middle.__cause__ = OSError(errno.EADDRINUSE, 'synthetic private text')
    error.__context__ = middle
    assert _start_failure(error).startswith('listener_bind_in_use')
    middle.__cause__ = None
    middle.__context__ = middle
    assert _start_failure(error) == 'listener_start_failed'
    head = error
    for _ in range(9):
        child = RuntimeError('synthetic')
        head.__context__ = child
        head = child
    head.__context__ = OSError(errno.EADDRINUSE, 'synthetic')
    assert _start_failure(error) == 'listener_start_failed'


def test_real_listener_reports_occupied_ephemeral_port(tmp_path):
    import datetime
    import ipaddress
    import socket
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                await send({'type': message['type'] + '.complete'})
                if message['type'] == 'lifespan.shutdown':
                    return
    async def scenario(port):
        listener = UvicornListener()
        await listener.start(app, SimpleNamespace(cert_file=cert_file, key_file=key_file), SimpleNamespace(port=port))
        with pytest.raises(ListenerError, match='listener_bind_in_use'):
            await listener.wait_ready(5)
        with pytest.raises(ListenerError):
            await listener.stop()
        assert listener.task is None
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        asyncio.run(scenario(occupied.getsockname()[1]))
        assert occupied.fileno() >= 0


def test_callback_type_and_runner_timeout(rig):
    runner, events, config = rig
    config.values['AGENT_DJANGO_URL'] = None
    with pytest.raises(ValueError, match='invalid_callback_uri'):
        callback_endpoint(config)
    with pytest.raises(ValueError, match='timeout must be positive'):
        OperationRunner(resolve=None, prepare_dependencies=None, prepare_https=None,
            plan=None, manager=None, compose_runtime=None, create_application=None,
            listener=None, timeout=0)
    assert not events
    default_port = callback_endpoint(Config({'AGENT_DJANGO_URL': 'https://agent.example/cb?x=1'}))
    assert default_port.port == 443
    assert default_port.origin == 'https://agent.example'


def test_acquire_failure_does_not_release(rig):
    runner, events, _ = rig
    class Broken:
        def acquire(self):
            raise RuntimeError('acquire failed')
    runner.manager = lambda plan, cancelled: Broken()
    async def scenario():
        with pytest.raises(RuntimeError, match='acquire failed'):
            async with runner.open():
                pass
    asyncio.run(scenario())
    assert 'release:False' not in events


def test_cleanup_attempts_all_owners(rig):
    runner, events, _ = rig
    class BrokenListener:
        async def start(self, *args):
            pass
        async def wait_ready(self, timeout):
            pass
        async def stop(self):
            events.append('listener-stop')
            raise RuntimeError('stop failed')
    runner.listener = BrokenListener
    async def scenario():
        with pytest.raises(CleanupPendingError):
            async with runner.open():
                pass
    asyncio.run(scenario())
    assert events[-1:] == ['listener-stop']
    assert runner.pending_cleanup is not None


def test_runtime_and_lease_cleanup_failures(rig):
    runner, events, _ = rig
    class BrokenRuntime:
        async def astart(self):
            pass
        async def aclose(self):
            events.append('runtime-close')
            raise RuntimeError('runtime close failed')
    class BrokenManager:
        def acquire(self):
            return 'lease'
        def release(self, *, retain):
            events.append('release')
            raise RuntimeError('release failed')
    runner.compose_runtime = lambda *args: BrokenRuntime()
    runner.manager = lambda plan, cancelled: BrokenManager()
    async def scenario():
        with pytest.raises(CleanupPendingError):
            async with runner.open():
                pass
    asyncio.run(scenario())
    assert events[-1:] == ['runtime-close']
    assert runner.pending_cleanup is not None


def test_listener_shutdown_timeout(monkeypatch):
    async def timeout(task, seconds):
        raise TimeoutError()
    monkeypatch.setattr(asyncio, 'wait_for', timeout)
    async def scenario():
        listener = UvicornListener()
        listener.server = SimpleNamespace(should_exit=False)
        listener.task = asyncio.create_task(asyncio.sleep(100))
        with pytest.raises(ListenerError, match='listener_shutdown_timeout'):
            await listener.stop()
        assert listener.server is None
    asyncio.run(scenario())


def test_cancel_during_acquire_waits_for_manager_rollback(rig):
    runner, events, _ = rig
    class CancellingManager:
        lease = None
        def __init__(self, cancelled):
            self.cancelled = cancelled
        def acquire(self):
            events.append('acquire-enter')
            while not self.cancelled():
                time.sleep(.001)
            events.append('acquire-rollback')
            raise RuntimeError('cancelled by manager')
    runner.manager = lambda plan, cancelled: CancellingManager(cancelled)
    async def scenario():
        task = asyncio.create_task(_enter(runner))
        while 'acquire-enter' not in events:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async def _enter(runner):
        async with runner.open():
            pass
    asyncio.run(scenario())
    assert events[-2:] == ['acquire-enter', 'acquire-rollback']


def test_local_factory_wires_helpers_without_io_at_initialization(monkeypatch, tmp_path):
    import api_agent.runtime.dependencies as dependencies
    import api_agent.runtime.https as https
    import api_agent.runtime.compose as compose
    import api_agent.runtime.prerequisites as prerequisites
    import api_agent.api.composition as composition
    import api_agent.api.application as application
    import api_agent.runtime.listener as listener_module
    import api_agent.auth.extensions as extensions
    events = []
    config = Config({'AGENT_DJANGO_URL': 'https://agent.example/cb'})
    context = SimpleNamespace(base_root=tmp_path)
    def preflight(selected, values):
        assert selected is context and values is config.values
        events.append('preflight')
    monkeypatch.setattr(extensions, 'validate_operation', preflight)
    monkeypatch.setattr('api_agent.runtime.operation.sys.prefix', str(tmp_path / '.venv'))
    material = SimpleNamespace(cert_file='cert', key_file='key', ca_file='ca')
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *a, **kw: events.append('dependencies'))
    monkeypatch.setattr(https, 'prepare_local_https', lambda *a, **kw: (events.append('tls'), SimpleNamespace(material=material))[1])
    monkeypatch.setattr(compose, 'plan_compose', lambda *a, **kw: (events.append('plan'), 'plan')[1])
    class Manager:
        lease = None
        def __init__(self, *a, **kw):
            assert kw['engine_environment'] == {'DOCKER_HOST': 'synthetic'}
            assert callable(kw['cancelled'])
        def acquire(self):
            events.append('acquire')
            self.lease = 'lease'
            return self.lease
        def release(self, *, retain):
            events.append(f'release:{retain}')
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', Manager)
    class Runtime:
        async def astart(self):
            events.append('runtime-start')
        async def aclose(self):
            events.append('runtime-close')
    def runtime(*args, **kw):
        assert kw['callback_origin'] == 'https://agent.example'
        assert kw['tls_material'] is material
        events.append('compose')
        return Runtime()
    monkeypatch.setattr(composition, 'compose_runtime', runtime)
    monkeypatch.setattr(application, 'create_application', lambda *a, **kw: (events.append('application'), 'app')[1])
    class Listener:
        async def start(self, *a):
            events.append('listener-start')
        async def wait_ready(self, timeout):
            events.append('ready')
        async def stop(self):
            events.append('listener-stop')
    monkeypatch.setattr(listener_module, 'UvicornListener', Listener)
    runner = local_operation(context, resolve=lambda: config, argv=(), scope='scope',
        ownership_directory=tmp_path, engine_environment={'DOCKER_HOST': 'synthetic'})
    assert events == []
    async def scenario():
        async with runner.open(retain=True):
            events.append('work')
    asyncio.run(scenario())
    assert events == ['preflight', 'dependencies', 'tls', 'plan', 'acquire', 'compose',
        'runtime-start', 'application', 'listener-start', 'ready', 'work',
        'listener-stop', 'runtime-close', 'release:True']


def test_local_factory_pack_preflight_checks_fresh_valid_config_before_side_effects(monkeypatch, tmp_path):
    import api_agent.auth.extensions as extensions
    import api_agent.runtime.dependencies as dependencies
    import api_agent.runtime.https as https
    import api_agent.api.composition as composition

    calls = []
    configs = iter((Config({'AGENT_DJANGO_URL': 'https://agent.example/cb'}, errors=('bad',)),
                    Config({'AGENT_DJANGO_URL': 'https://agent.example/cb', 'API_ENDPOINT': ''})))
    context = SimpleNamespace(base_root=tmp_path)
    def preflight(selected, values):
        calls.append(('preflight', selected, values))
        raise PermissionError('pack blocked operation')
    monkeypatch.setattr(extensions, 'validate_operation', preflight)
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *a, **kw: pytest.fail('dependencies ran'))
    monkeypatch.setattr(https, 'prepare_local_https', lambda *a, **kw: pytest.fail('TLS ran'))
    monkeypatch.setattr(composition, 'compose_runtime', lambda *a, **kw: pytest.fail('compose ran'))
    runner = local_operation(context, resolve=lambda: next(configs), argv=(), scope='scope',
                             ownership_directory=tmp_path)

    async def scenario():
        with pytest.raises(ValueError, match='invalid_runtime_configuration'):
            async with runner.open():
                pytest.fail('invalid config entered operation')
        assert calls == []
        with pytest.raises(PermissionError, match='pack blocked operation'):
            async with runner.open():
                pytest.fail('blocked config entered operation')
    asyncio.run(scenario())
    assert len(calls) == 1 and calls[0][1] is context
    assert calls[0][2]['API_ENDPOINT'] == ''


def test_factory_handoff_status_is_explicit(monkeypatch, tmp_path):
    import api_agent.runtime.dependencies as dependencies
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *a, **kw: 0)
    monkeypatch.setattr('api_agent.runtime.operation.sys.prefix', str(tmp_path / '.venv'))
    runner = local_operation(SimpleNamespace(base_root=tmp_path),
        resolve=lambda: Config({'AGENT_DJANGO_URL': 'https://agent.example/cb'}),
        argv=(), scope='scope', ownership_directory=tmp_path)
    async def scenario():
        with pytest.raises(RuntimeError, match='interpreter_handoff_required'):
            async with runner.open():
                pass
    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_owned_task():
    from api_agent.runtime.operation import _settle, _offload
    async def scenario():
        target = asyncio.create_task(asyncio.sleep(.03, result='finished'))
        waiter = asyncio.create_task(_settle(target))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        assert await waiter == 'finished'
        entered = asyncio.Event()
        def work():
            time.sleep(.02)
            return 'done'
        task = asyncio.create_task(_offload(work))
        await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_settle_completed_task_after_cancel_race(monkeypatch):
    from api_agent.runtime.operation import _settle
    async def interrupted(task):
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, 'shield', interrupted)
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0, result='ready'))
        await task
        assert await _settle(task) == 'ready'
    asyncio.run(scenario())


def test_bind_exit_is_owned_failure_and_drains_every_owner(rig, monkeypatch):
    class Server:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
        async def serve(self):
            raise SystemExit(3)
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=lambda *a, **kw: kw, Server=Server))
    runner, events, _ = rig
    runner.listener = UvicornListener
    runner.prepare_https = lambda *a: SimpleNamespace(material=SimpleNamespace(cert_file='cert', key_file='key'))
    runner.compose_runtime = lambda *a: SimpleNamespace(
        astart=lambda: asyncio.sleep(0), aclose=lambda: close())
    async def close():
        events.append('runtime-close')
    async def scenario():
        with pytest.raises(ListenerError, match='listener_start_failed'):
            async with runner.open():
                pytest.fail('A failed bind must not run the workload')
        assert runner.pending_cleanup is not None
        await runner.pending_cleanup.retry()
        assert events[-2:] == ['runtime-close', 'release:False']
    asyncio.run(scenario())


def test_operation_failure_callback_cannot_replace_primary_or_cleanup(rig):
    runner, events, _ = rig
    primary = ListenerError('listener_bind_in_use')
    class Listener:
        async def start(self, *args): pass
        async def wait_ready(self, timeout): raise primary
        async def stop(self): events.append('listener-stop')
    runner.listener = Listener
    def failing_callback(error):
        assert error is primary
        events.append('failure-callback')
        raise asyncio.CancelledError('synthetic observer fault')
    runner.on_failure = failing_callback
    async def scenario():
        with pytest.raises(ListenerError) as caught:
            async with runner.open():
                pytest.fail('startup cannot succeed')
        assert caught.value is primary
        assert events.index('failure-callback') < events.index('listener-stop')
        assert events[-3:] == ['listener-stop', 'runtime-close', 'release:False']
    asyncio.run(scenario())


def test_operation_workload_failure_does_not_report_as_startup(rig):
    runner, _, _ = rig
    failures = []
    runner.on_failure = failures.append
    async def scenario():
        with pytest.raises(ValueError, match='workload'):
            async with runner.open():
                raise ValueError('workload')
        assert failures == []
    asyncio.run(scenario())


def test_first_and_repeated_cancellation_during_cleanup_propagates_after_drain(rig):
    runner, events, _ = rig
    async def scenario():
        stopping, finish = asyncio.Event(), asyncio.Event()
        class Listener:
            async def start(self, *args):
                pass
            async def wait_ready(self, timeout):
                pass
            async def stop(self):
                stopping.set()
                await finish.wait()
                events.append('listener-stop')
        runner.listener = Listener
        async def operation():
            async with runner.open():
                events.append('work')
            events.append('continued-after-cancel')
        task = asyncio.create_task(operation())
        await stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events[-3:] == ['listener-stop', 'runtime-close', 'release:False']
        assert 'continued-after-cancel' not in events
    asyncio.run(scenario())


def test_port_zero_is_not_silently_replaced_with_443():
    with pytest.raises(ValueError, match='invalid_callback_uri'):
        callback_endpoint(Config({'AGENT_DJANGO_URL': 'https://agent.example:0/cb'}))


def test_factory_rejects_wrong_interpreter_before_nested_handoff(monkeypatch, tmp_path):
    import api_agent.runtime.dependencies as dependencies
    calls = []
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *a, **kw: calls.append('nested-launch'))
    monkeypatch.setattr('api_agent.runtime.operation.sys.prefix', str(tmp_path / 'wrong'))
    runner = local_operation(SimpleNamespace(base_root=tmp_path),
        resolve=lambda: Config({'AGENT_DJANGO_URL': 'https://agent.example/cb'}),
        argv=(), scope='scope', ownership_directory=tmp_path)
    async def scenario():
        with pytest.raises(RuntimeError, match='interpreter_handoff_required'):
            async with runner.open():
                pass
    asyncio.run(scenario())
    assert calls == []


def test_operation_specific_validation_precedes_all_side_effects(rig):
    runner, events, config = rig
    def reject(value):
        assert value is config
        raise PermissionError('fresh config cannot execute')
    async def scenario():
        with pytest.raises(PermissionError, match='fresh config cannot execute'):
            async with runner.open(validate=reject):
                pass
    asyncio.run(scenario())
    assert events == ['resolve']


def test_factory_requests_recording_and_tracing_dependencies(monkeypatch, tmp_path):
    import api_agent.runtime.dependencies as dependencies
    calls = []
    monkeypatch.setattr('api_agent.runtime.operation.sys.prefix', str(tmp_path / '.venv'))
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *a, **kw: calls.append(kw))
    runner = local_operation(SimpleNamespace(base_root=tmp_path), resolve=lambda: None,
        argv=(), scope='scope', ownership_directory=tmp_path, groups=('reports', 'browser'))
    runner.prepare_dependencies(Config({'TUI_MODE': 'integration', 'TRACE_MODE': 'OTEL'}))
    assert calls == [{'collect': False, 'groups': ('reports', 'browser', 'tracing')}]
    runner.prepare_dependencies(Config({'TUI_MODE': 'run-local'}))
    assert calls[-1]['groups'] == ('reports', 'browser')


def test_body_failure_keeps_pending_retry_handle_and_owned_lease(rig):
    runner, events, _ = rig
    async def scenario():
        fails=True
        class Listener:
            async def start(self,*args): pass
            async def wait_ready(self,*args): pass
            async def stop(self):
                events.append('listener-stop')
                if fails: raise ValueError('private stop failure')
        runner.listener=Listener
        primary=ValueError('body')
        with pytest.raises(ValueError) as error:
            async with runner.open():
                raise primary
        assert error.value is primary
        assert isinstance(primary.cleanup_pending,CleanupPendingError)
        assert primary.cleanup_pending is runner.pending_cleanup
        assert 'runtime-close' not in events and 'release:False' not in events
        with pytest.raises(CleanupPendingError):
            async with runner.open(): pytest.fail('pending owner cannot be overwritten')
        fails=False
        await asyncio.gather(primary.cleanup_pending.retry(),primary.cleanup_pending.retry())
        await primary.cleanup_pending.retry()
        assert events.count('runtime-close')==events.count('release:False')==1
        assert runner.pending_cleanup is None
    asyncio.run(scenario())


def test_start_failure_before_yield_keeps_pending_runtime_and_lease(rig):
    runner,events,_=rig
    async def scenario():
        fails=True
        class Runtime:
            async def astart(self): raise ValueError('start')
            async def aclose(self):
                events.append('runtime-close')
                if fails: raise ValueError('close')
        runner.compose_runtime=lambda *args: Runtime()
        with pytest.raises(ValueError,match='start') as error:
            async with runner.open(): pytest.fail('must not yield')
        assert isinstance(error.value.cleanup_pending,CleanupPendingError)
        assert 'release:False' not in events
        fails=False
        await error.value.cleanup_pending.retry()
        assert events.count('release:False')==1
    asyncio.run(scenario())


@pytest.mark.parametrize('body_error,finish_cleanup',[(False,False),(True,False),(True,True)])
def test_cancellation_during_cleanup_preserves_body_and_retained_owner(rig,body_error,finish_cleanup):
    runner,events,_=rig
    async def scenario():
        completion_timeout = runner.timeout
        entered,finish=asyncio.Event(),asyncio.Event()
        runtime_entered,runtime_finish,released=asyncio.Event(),asyncio.Event(),asyncio.Event()
        loop = asyncio.get_running_loop()
        runtime_times = []
        delayed_finish = None
        class Listener:
            async def start(self,*args): pass
            async def wait_ready(self,*args): pass
            async def stop(self):
                entered.set()
                await finish.wait()
                events.append('listener-stop')
        class Runtime:
            async def astart(self): pass
            async def aclose(self):
                runtime_times.append(loop.time())
                runtime_entered.set()
                await runtime_finish.wait()
                runtime_times.append(loop.time())
                events.append('runtime-close')
        manager_factory = runner.manager
        def observed_manager(plan,cancelled):
            manager = manager_factory(plan,cancelled)
            class ObservedManager:
                def acquire(self): return manager.acquire()
                def release(self,*,retain):
                    manager.release(retain=retain)
                    loop.call_soon_threadsafe(released.set)
            return ObservedManager()
        listener,runtime=Listener(),Runtime()
        runner.listener=lambda: listener
        runner.compose_runtime=lambda *args: runtime
        runner.manager=observed_manager
        # Only deliberately blocked cleanup is expected to exhaust this budget.
        if not finish_cleanup: runner.timeout=.02
        primary=ValueError('body')
        async def operation():
            async with runner.open():
                if body_error: raise primary
        task=asyncio.create_task(operation())
        try:
            await asyncio.wait_for(entered.wait(),completion_timeout)
            task.cancel()
            assert not runtime_entered.is_set() and not released.is_set()
            if finish_cleanup:
                finish.set()
                await asyncio.wait_for(runtime_entered.wait(),completion_timeout)
                assert not released.is_set()
                # Deliberately exceed the former 20ms window after actual entry.
                delayed_finish=loop.call_later(.05,runtime_finish.set)
                await asyncio.wait_for(released.wait(),completion_timeout)
                assert runtime_times[1]-runtime_times[0] >= .02
            with pytest.raises(ValueError if body_error else asyncio.CancelledError) as error:
                await task
            if body_error: assert error.value is primary
            if not finish_cleanup:
                pending=error.value.cleanup_pending
                assert isinstance(pending,CleanupPendingError)
                assert runner.pending_cleanup is pending
                owner=pending.retry.__self__
                assert owner.listener is listener and owner.runtime is runtime and owner.lease=='lease'
                assert not runtime_entered.is_set() and not released.is_set()
                assert 'release:False' not in events
                finish.set()
                runtime_finish.set()
                # Retry acknowledges completion, rather than another intentional expiry.
                owner.timeout=completion_timeout
                await asyncio.wait_for(pending.retry(),completion_timeout)
            else:
                assert getattr(primary,'cleanup_pending',None) is None
            assert runner.pending_cleanup is None
            assert events.count('listener-stop')==events.count('runtime-close')==events.count('release:False')==1
        finally:
            if delayed_finish is not None: delayed_finish.cancel()
            finish.set()
            runtime_finish.set()
            if not task.done(): task.cancel()
            await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),completion_timeout)
            if runner.pending_cleanup is not None:
                pending=runner.pending_cleanup
                pending.retry.__self__.timeout=completion_timeout
                await asyncio.wait_for(pending.retry(),completion_timeout)
        assert not [owned for owned in asyncio.all_tasks() if owned is not asyncio.current_task() and not owned.done()]
    asyncio.run(scenario())


def test_overlapping_open_rejected_through_preparation_body_and_cleanup(rig):
    from threading import Event
    runner,events,_=rig
    async def scenario():
        preparing,prepared=Event(),Event()
        body,leave,closing,closed=asyncio.Event(),asyncio.Event(),asyncio.Event(),asyncio.Event()
        def dependencies(config):
            preparing.set()
            prepared.wait(2)
        runner.prepare_dependencies=dependencies
        class Listener:
            async def start(self,*args): pass
            async def wait_ready(self,*args): pass
            async def stop(self):
                closing.set()
                await closed.wait()
                raise ValueError('pending')
        runner.listener=Listener
        async def first():
            async with runner.open():
                body.set()
                await leave.wait()
        task=asyncio.create_task(first())
        await asyncio.to_thread(preparing.wait,2)
        async def overlap():
            with pytest.raises(RuntimeError,match='operation_already_active'):
                async with runner.open(): pytest.fail('overlap admitted')
        await overlap()
        prepared.set()
        await body.wait()
        await overlap()
        leave.set()
        await closing.wait()
        await overlap()
        closed.set()
        with pytest.raises(CleanupPendingError) as error: await task
        handle=error.value
        assert runner.pending_cleanup is handle and not runner._active
        with pytest.raises(CleanupPendingError) as blocked:
            async with runner.open(): pytest.fail('pending admitted')
        assert blocked.value is handle
        async def confirmed_stop(): pass
        handle.retry.__self__.listener.stop=confirmed_stop
        await handle.retry()
        assert runner.pending_cleanup is None and events.count('release:False')==1
    asyncio.run(scenario())


@pytest.mark.parametrize('stage',['configuration','dependencies'])
def test_preparation_failure_releases_single_operation_guard(rig,stage):
    runner,events,config=rig
    async def scenario():
        if stage=='configuration':
            resolve=runner.resolve
            runner.resolve=lambda: Config({},errors=('invalid',))
        else:
            dependencies=runner.prepare_dependencies
            def failed(config): raise ValueError('invalid')
            runner.prepare_dependencies=failed
        with pytest.raises(ValueError):
            async with runner.open(): pytest.fail('failed preparation yielded')
        assert not runner._active
        if stage=='configuration': runner.resolve=resolve
        else: runner.prepare_dependencies=dependencies
        async with runner.open(): pass
        assert events.count('release:False')==1
    asyncio.run(scenario())
