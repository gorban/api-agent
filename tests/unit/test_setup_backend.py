"""Shared setup host lifetimes use synthetic owners and operation-local IPC."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

import pytest

from api_agent.providers.contracts import ContractError, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError
from api_agent.testing import provider_setup, setup_backend


@pytest.fixture
def fake_host(monkeypatch, tmp_path):
    from api_agent.providers import setup_transport
    events = []
    state = SimpleNamespace(events=events, provider_failure=False, server_failure=False,
                            start_failure=False, wait_start=False)
    class Provider:
        def __init__(self, config, root, **kwargs):
            assert kwargs['loop'] is asyncio.get_running_loop()
            self.loop = kwargs['loop']
            self.closed = False
            self.config = deepcopy(config)
            events.append(('provider', threading.get_ident(), kwargs['unattended_index_source']))
        async def _ensure(self):
            events.append('ready')
            await asyncio.sleep(.01)
        async def _close(self):
            events.append('provider-close')
            if state.provider_failure:
                raise RuntimeError('synthetic private cleanup')
    class Server:
        def __init__(self, registry, capability, *, timeout):
            self.registry = registry
            self.capability = capability
            self.timeout = timeout
            events.append('server')
        async def start(self):
            events.append('start')
            if state.wait_start:
                await asyncio.sleep(.03)
            if state.start_failure:
                raise ContractError('synthetic startup failed')
            return {'version': 1, 'host': '127.0.0.1', 'port': 32123, 'capability': self.capability}
        async def aclose(self):
            events.append('server-close')
            if state.server_failure:
                raise RuntimeError('synthetic private server')
    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    monkeypatch.setattr(setup_transport, 'SetupServer', Server)
    state.backend = setup_backend.SetupBackend({'LLM_PROVIDER': 'synthetic', 'UNATTENDED_AUTH': {'roles': []}},
                                              tmp_path, unattended_index_source='cli')
    yield state
    state.server_failure = state.provider_failure = False
    state.backend.close()
    if state.backend.loop is not None:
        assert state.backend.loop.is_closed()
    if state.backend.thread is not None:
        assert not state.backend.thread.is_alive()


def checkpoint(backend, count=1):
    async def ready():
        bindings = [backend.registry.register('worker-' + str(index)) for index in range(count)]
        await asyncio.gather(*(backend.registry.ensure_ready(binding) for binding in bindings))
    return asyncio.run_coroutine_threadsafe(ready(), backend.loop).result()


def test_inert_host_starts_ipc_once_and_coalesces_setup_in_creating_loop(fake_host):
    state = fake_host
    backend = state.backend
    assert not state.events and backend.loop is backend.thread is None
    descriptor = backend.start()
    descriptor['port'] = 1
    assert backend.start()['port'] == 32123
    assert state.events == ['server', 'start']
    assert backend.selected_identity != 'synthetic' and len(backend.selected_identity) == 64
    checkpoint(backend, 2)
    assert state.events.count('ready') == 1
    providers = [row for row in state.events if isinstance(row, tuple)]
    assert providers == [('provider', backend.thread.ident, 'cli')]
    backend.close()
    assert backend.closed and not backend.pending
    assert state.events[-2:] == ['server-close', 'provider-close']
    backend.close()
    assert state.events.count('provider-close') == 1
    with pytest.raises(ContractError):
        backend.start()


def test_unstarted_host_has_no_thread_or_disposal_side_effects(fake_host):
    backend = fake_host.backend
    backend.close()
    assert backend.closed and backend.thread is None and not fake_host.events
    assert backend.end_inspection()
    backend.wait_closed()
    backend.abort()


def test_debug_host_is_rejected_before_allocation(tmp_path):
    with pytest.raises(ContractError, match='worker-local'):
        setup_backend.SetupBackend({'DEBUG': True}, tmp_path)


@pytest.mark.parametrize('failure', ['provider', 'server'])
def test_pending_host_disposal_keeps_thread_and_registry_for_retry(fake_host, failure):
    state = fake_host
    backend = state.backend
    backend.start()
    checkpoint(backend)
    setattr(state, failure + '_failure', True)
    with pytest.raises(CleanupPendingError) as caught:
        backend.close()
    assert caught.value.inspections == (backend,) and caught.value.owners == (backend,)
    assert backend.pending and not backend.closed and backend.thread.is_alive()
    assert not backend.loop.is_closed()
    assert not backend.end_inspection()
    setattr(state, failure + '_failure', False)
    asyncio.run(caught.value.retry())
    assert backend.closed and not backend.pending and backend.loop.is_closed()


def test_start_failure_disposes_allocated_server_before_return(fake_host):
    state = fake_host
    state.start_failure = True
    with pytest.raises(ContractError, match='startup failed'):
        state.backend.start()
    assert state.backend.closed and state.events == ['server', 'start', 'server-close']


def test_start_failure_retains_failed_cleanup_receipt(fake_host):
    state = fake_host
    state.start_failure = state.server_failure = True
    with pytest.raises(ContractError) as caught:
        state.backend.start()
    assert caught.value.inspections == (state.backend,)
    assert caught.value.cleanup_pending.owners == (state.backend,)
    with pytest.raises(ContractError, match='pending'):
        state.backend.start()
    state.server_failure = False
    state.backend.wait_closed()
    assert state.backend.closed


def test_close_waits_for_owned_startup_before_disposal(fake_host):
    state = fake_host
    state.wait_start = True
    backend = state.backend
    backend._start_thread()
    backend._start_future = asyncio.run_coroutine_threadsafe(backend._initialize(), backend.loop)
    backend.close()
    assert state.events == ['server', 'start', 'server-close']
    assert backend.closed and backend.loop.is_closed()


def test_loop_creation_failure_has_no_orphan_thread(fake_host, monkeypatch):
    monkeypatch.setattr(setup_backend.asyncio, 'new_event_loop', lambda: (_ for _ in ()).throw(RuntimeError('private loop')))
    with pytest.raises(ContractError, match='unavailable'):
        fake_host.backend.start()
    fake_host.backend.thread.join(timeout=1)
    assert fake_host.backend.closed and not fake_host.backend.thread.is_alive()


def test_interrupted_thread_join_retains_disposal_ack_until_retry(fake_host, monkeypatch):
    backend = fake_host.backend
    backend.start()
    original = backend.thread.join
    interrupted = []
    def join(*args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return original(*args, **kwargs)
    monkeypatch.setattr(backend.thread, 'join', join)
    with pytest.raises(KeyboardInterrupt) as caught:
        backend.close()
    assert caught.value.inspections == (backend,) and backend.pending and not backend.closed
    backend.abort()
    assert backend.closed and not backend.pending
    assert fake_host.events.count('server-close') == 1


def test_protected_snapshot_validates_backend_before_acceptance(tmp_path, monkeypatch):
    descriptor = {'version': 1, 'host': '127.0.0.1', 'port': 32123, 'capability': 'a' * 32}
    path = provider_setup.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {},
        unattended_index_source='cli', setup_backend=descriptor)
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    assert provider_setup._read_snapshot()['setup_backend'] == descriptor
    payload = json.loads(path.read_text())
    payload['setup_backend']['host'] = 'example.invalid'
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match='start through launch.py'):
        provider_setup._read_snapshot()


def test_remote_fixture_is_inert_and_closes_lease_on_same_owned_thread(tmp_path, monkeypatch):
    from api_agent.providers import setup_transport
    events = []
    class Client:
        def __init__(self, descriptor, *, timeout):
            self.descriptor = descriptor
            self.timeout = timeout
        def ensure_ready(self):
            events.append(('ready', threading.get_ident()))
        def close(self):
            events.append(('close', threading.get_ident()))
    monkeypatch.setattr(setup_transport, 'SetupClient', Client)
    monkeypatch.setattr(provider_setup, '_read_snapshot', lambda: {'setup_backend': {'fixture': True}})
    values = provider_setup.llm_provider.__wrapped__()
    remote = next(values)
    assert events == []
    remote.ensure_ready()
    remote.ensure_ready()
    with pytest.raises(StopIteration):
        next(values)
    assert [name for name, _ in events] == ['ready', 'ready', 'close']
    assert len({identity for _, identity in events}) == 1
    assert events[0][1] != threading.get_ident() and remote.loop.is_closed()
    remote.close()
    assert len(events) == 3


def test_remote_prerequisite_skip_keeps_lease_owned_until_fixture_close(monkeypatch):
    from api_agent.providers import setup_transport
    class Client:
        def __init__(self, *args, **kwargs): pass
        def ensure_ready(self): raise SetupPrerequisiteError('Select synthetic provider')
        def close(self): pass
    monkeypatch.setattr(setup_transport, 'SetupClient', Client)
    remote = provider_setup.RemoteIntegrationLLM({})
    try:
        with pytest.raises(pytest.skip.Exception, match='Select synthetic provider'):
            remote.ensure_ready()
        assert not remote.loop.is_closed()
    finally:
        remote.close()


def test_slow_loop_handoff_retains_one_thread_until_late_ack(fake_host, monkeypatch):
    backend = fake_host.backend
    backend.cleanup_timeout = .01
    gate = threading.Event()
    original = setup_backend.asyncio.new_event_loop
    def delayed():
        gate.wait(timeout=2)
        return original()
    monkeypatch.setattr(setup_backend.asyncio, 'new_event_loop', delayed)
    try:
        with pytest.raises(CleanupPendingError) as caught:
            backend.start()
        thread = backend.thread
        assert backend.pending and thread.is_alive() and backend.loop is None
        assert caught.value.inspections == (backend,)
        gate.set()
        assert backend._ready.wait(timeout=1)
        backend.close()
        assert backend.thread is thread and backend.closed and backend.loop.is_closed()
        assert not fake_host.events
    finally:
        gate.set()


def test_late_startup_and_cleanup_keep_same_resources_for_ack_retry(fake_host):
    from concurrent.futures import TimeoutError
    state = fake_host
    state.wait_start = True
    backend = state.backend
    backend.cleanup_timeout = .005
    with pytest.raises(TimeoutError) as caught:
        backend.start()
    assert caught.value.inspections == (backend,)
    assert backend.pending and backend.thread.is_alive()
    backend._close_future.result(timeout=1)
    backend.close()
    assert backend.closed and backend.loop.is_closed()
    assert state.events == ['server', 'start', 'server-close']


def test_failed_late_startup_is_drained_before_server_cleanup(fake_host):
    state = fake_host
    state.wait_start = state.start_failure = True
    backend = state.backend
    backend.cleanup_timeout = .005
    with pytest.raises(TimeoutError):
        backend.start()
    backend._close_future.result(timeout=1)
    backend.close()
    assert backend.closed and state.events == ['server', 'start', 'server-close']


def test_join_timeout_preserves_disposed_loop_ack_without_duplicate_cleanup(fake_host, monkeypatch):
    backend = fake_host.backend
    backend.start()
    thread = backend.thread
    original_join, original_alive = thread.join, thread.is_alive
    joined = []
    def join(*args, **kwargs):
        original_join(*args, **kwargs)
        joined.append(True)
    monkeypatch.setattr(thread, 'join', join)
    monkeypatch.setattr(thread, 'is_alive', lambda: True)
    try:
        with pytest.raises(CleanupPendingError):
            backend.close()
        assert backend._disposed and not backend.closed and backend.pending
        assert backend.loop.is_closed()
    finally:
        monkeypatch.setattr(thread, 'is_alive', original_alive)
    backend.close()
    assert backend.closed and not backend.pending and len(joined) == 2
    assert fake_host.events.count('server-close') == 1


def test_close_retries_cancelled_control_future_without_recreating_host(fake_host):
    from concurrent.futures import Future
    backend = fake_host.backend
    backend.start()
    future = Future()
    future.cancel()
    backend._close_future = future
    thread = backend.thread
    backend.close()
    assert backend.closed and backend.thread is thread


def test_externally_supplied_loop_handoff_is_not_replaced(fake_host):
    backend = fake_host.backend
    backend.loop = asyncio.new_event_loop()
    creating_loop = backend.loop
    backend._start_thread()
    backend.close()
    assert backend.loop is creating_loop and creating_loop.is_closed()


def synthetic_provider(tmp_path):
    factory = tmp_path / 'provider.py'
    events = tmp_path / 'events.jsonl'
    factory.write_text('''import asyncio, json
from pathlib import Path
from api_agent.providers.progress import report_timeout

def record(path, event):
    with Path(path).open('a') as stream:
        stream.write(json.dumps(event) + '\\n')

class Auth:
    def __init__(self, path): self.path = path
    async def __call__(self):
        record(self.path, 'token')
        return object()
    async def aclose(self): record(self.path, 'auth-close')

class Adapter:
    version = 1
    def __init__(self, path, gate): self.path, self.gate = path, gate
    async def asetup(self, *, interactive, user_index, credentials):
        record(self.path, 'setup')
        async with report_timeout('Synthetic setup stage', 2):
            if self.gate:
                while not Path(self.gate).exists():
                    await asyncio.sleep(.005)
            else:
                await asyncio.sleep(.15)
        return Auth(self.path)
    async def aclose(self): record(self.path, 'provider-close')

def factory(config, services):
    record(config['EVENT_PATH'], 'factory')
    return Adapter(config['EVENT_PATH'], config.get('SETUP_GATE'))
''')
    return {'LLM_PROVIDER': str(factory) + ':factory', 'EVENT_PATH': str(events)}, events


def test_real_processes_coalesce_setup_and_disconnected_waiter_leaves_sibling(tmp_path):
    import subprocess
    import sys
    import time
    from api_agent.testing.processes import finalize
    config, events = synthetic_provider(tmp_path)
    gate = tmp_path / 'release-setup'
    config['SETUP_GATE'] = str(gate)
    backend = setup_backend.SetupBackend(config, tmp_path)
    script = '''import json, sys
from api_agent.providers.setup_transport import SetupClient
client = SetupClient(json.loads(sys.argv[1]), timeout=5)
assert client.ensure_ready() is None
print('ready', flush=True)
client.close()
'''
    processes = []
    try:
        descriptor = backend.start()
        first = subprocess.Popen([sys.executable, '-c', script, json.dumps(descriptor)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(first)
        deadline = time.monotonic() + 5
        while (not events.exists() or 'setup' not in events.read_text()) and time.monotonic() < deadline:
            time.sleep(.005)
        assert events.exists() and 'setup' in events.read_text()
        second = subprocess.Popen([sys.executable, '-c', script, json.dumps(descriptor)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(second)
        async def leases():
            return len(backend.registry._leases)
        while (asyncio.run_coroutine_threadsafe(leases(), backend.loop).result() < 2
                and time.monotonic() < deadline):
            time.sleep(.005)
        assert asyncio.run_coroutine_threadsafe(leases(), backend.loop).result() == 2
        first.terminate()
        first.communicate(timeout=5)
        gate.touch()
        output, errors = second.communicate(timeout=5)
        assert second.returncode == 0, errors
        assert output == 'ready\n' and descriptor['capability'] not in output + errors
        backend.close()
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        assert rows.count('factory') == rows.count('setup') == rows.count('token') == 1
        assert rows[-2:] == ['auth-close', 'provider-close']
        assert backend.loop.is_closed() and not backend.thread.is_alive()
    finally:
        gate.touch()
        for process in processes:
            finalize(process, communicate=True)
        backend.close()


def test_full_integration_operation_reuses_setup_across_repository_processes(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    config, events = synthetic_provider(tmp_path)
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    for root, name in ((base, 'base'), (pack, 'pack')):
        suite = root / 'tests/integration'
        suite.mkdir(parents=True)
        (suite / ('test_' + name + '.py')).write_text(
            'def test_setup(llm_provider):\n    llm_provider.ensure_ready()\n')
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/tests.toml').write_text('version=1\n[tests]\nintegration=["provider_tests"]\n')
    provider_suite = tmp_path / 'provider_tests'
    provider_suite.mkdir()
    (provider_suite / 'test_provider.py').write_text(
        'def test_setup(llm_provider):\n    llm_provider.ensure_ready()\n')
    context = SimpleNamespace(base_root=base, owner_root=pack,
        suite_paths=lambda mode: (base / 'tests/integration', pack / 'tests/integration'))
    selected = collect_tests(context, 'integration', config=config)
    published, reports = [], []
    result = run_integration_tests(context, selected, config=config, accepted=True,
        on_event=reports.append,
        after_run=lambda row: published.append((row.exit_code, events.read_text())))
    assert result.exit_code == 0 and len(result.tests) == 3, result.output
    assert all(test.status == 'passed' for test in result.tests)
    rows = [json.loads(line) for line in published[0][1].splitlines()]
    assert rows.count('factory') == rows.count('setup') == 1
    assert rows.count('token') == 3 and rows[-2:] == ['auth-close', 'provider-close']
    assert not result.inspections
    progress = [event for event in reports if event.kind == 'component_progress']
    assert {event.identity.repo for event in progress} == {item.repo for item in selected}
    assert all(event.component.name and event.component.elapsed >= 0 for event in progress)


@pytest.mark.parametrize('primary_boundary', ['start', 'publication'])
@pytest.mark.parametrize('cleanup_interrupted', [False, True])
def test_original_interrupt_keeps_pending_backend_after_secondary_disposal_failure(
        tmp_path, monkeypatch, primary_boundary, cleanup_interrupted):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    from io import StringIO
    identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok', root=tmp_path)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    primary = KeyboardInterrupt('original publication or startup interrupt')
    secondary = KeyboardInterrupt('cleanup interrupt') if cleanup_interrupted else RuntimeError('private cleanup')
    backend = SimpleNamespace(closed=False, pending=True, lost=False,
        start=lambda: {'version': 1, 'host': '127.0.0.1', 'port': 32123, 'capability': 'a' * 32},
        close=lambda: (_ for _ in ()).throw(RuntimeError('private close')),
        abort=lambda: (_ for _ in ()).throw(secondary))
    monkeypatch.setattr(setup_backend, 'SetupBackend', lambda *a, **k: backend)
    monkeypatch.setattr(integration, 'collect_tests', lambda *a, **k: (identity,))
    finalized = []
    def popen(args, **kwargs):
        if primary_boundary == 'start':
            raise primary
        rows = [dict(kind='report', node=identity.node_id, phase=phase, status='passed',
                     duration=0, traceback='') for phase in ('setup', 'call', 'teardown')]
        Path(args[3]).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return SimpleNamespace(stdout=StringIO(), returncode=0, poll=lambda: 0)
    monkeypatch.setattr(integration.subprocess, 'Popen', popen)
    monkeypatch.setattr(integration, 'finalize', finalized.append)
    def publish(result):
        assert result.inspections == (backend,)
        if primary_boundary == 'publication':
            raise primary
    with pytest.raises(KeyboardInterrupt) as caught:
        integration.run_integration_tests(context, (identity,), config={}, accepted=True, after_run=publish)
    assert caught.value is primary and caught.value.inspections == (backend,)
    assert not backend.closed and backend.pending
    assert len(finalized) == int(primary_boundary == 'publication')


def test_failed_preflight_never_allocates_backend_or_worker(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok', root=tmp_path)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    allocations = []
    monkeypatch.setattr(integration, 'collect_tests', lambda *a, **k: (identity,))
    monkeypatch.setattr(setup_backend, 'SetupBackend', lambda *a, **k: allocations.append('backend'))
    result = integration.run_integration_tests(context, (identity,), config={}, accepted=True,
        before_run=lambda: (_ for _ in ()).throw(RuntimeError('prerequisite rejected')))
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert allocations == []


def test_backend_identity_and_lazy_owner_share_immutable_snapshot(fake_host):
    backend = fake_host.backend
    original_identity = backend.selected_identity
    public = backend.config
    public['LLM_PROVIDER'] = 'changed'
    public['UNATTENDED_AUTH']['roles'].append('changed account')
    for attribute in ('config', 'operation_id', 'selected_identity', 'owner_root', 'unattended_index_source'):
        with pytest.raises(AttributeError):
            setattr(backend, attribute, 'changed')
    backend.start()
    checkpoint(backend)
    assert backend.selected_identity == original_identity
    assert backend.registry.selected_identity == original_identity
    assert backend.registry._owner.provider.config == {
        'LLM_PROVIDER': 'synthetic', 'UNATTENDED_AUTH': {'roles': []}}


def test_caller_nested_config_changes_cannot_change_backend_selection(fake_host, tmp_path):
    source = {'LLM_PROVIDER': 'original', 'UNATTENDED_AUTH': {'roles': ['original account']}}
    backend = setup_backend.SetupBackend(source, tmp_path)
    source['LLM_PROVIDER'] = 'changed'
    source['UNATTENDED_AUTH']['roles'][0] = 'changed account'
    try:
        backend.start()
        checkpoint(backend)
        assert backend.registry._owner.provider.config == {
            'LLM_PROVIDER': 'original', 'UNATTENDED_AUTH': {'roles': ['original account']}}
    finally:
        backend.close()


def test_owner_cleanup_interrupt_preserves_live_loop_and_disposes_sibling(fake_host):
    backend = fake_host.backend
    backend.start()
    checkpoint(backend)
    primary = KeyboardInterrupt('owned server cleanup interruption')
    original = backend.server.aclose
    async def interrupted():
        raise primary
    backend.server.aclose = interrupted
    with pytest.raises(KeyboardInterrupt) as caught:
        backend.close()
    assert caught.value is primary and caught.value.inspections == (backend,)
    assert backend.pending and not backend.closed and backend.thread.is_alive()
    assert 'provider-close' in fake_host.events
    backend.server.aclose = original
    backend.close()
    assert backend.closed and backend.loop.is_closed()
