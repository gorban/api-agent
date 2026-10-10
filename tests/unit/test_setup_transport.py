"""Public synthetic readiness IPC; no credentials, provider or browser operations."""
import asyncio
from dataclasses import asdict
import json
import multiprocessing
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import (ContractError, InteractionRequiredError,
    SessionClosedError, SetupPrerequisiteError, UnattendedCredentialError)
from api_agent.providers.progress import ComponentProgress, component_timeout, progress_sink
from api_agent.providers.setup_transport import (SetupClient, SetupServer, validate_descriptor,
    MAX_FRAME, _binding, _capability, _decode, _encode, _error, _identifier, _raise_error,
    _timeout, _Peer)

CAPABILITY = 'public-synthetic-capability-value-000000'


class Registry:
    def __init__(self):
        self.bindings = {}
        self.revoked = []
        self.calls = 0
        self.failure = None
        self.release = None
        self.entered = asyncio.Event()
    def register(self, worker_id, attempt=1):
        if worker_id in self.bindings:
            raise ContractError('duplicate')
        binding = dict(version=1, operation_id='public-operation', selected_identity='frozen-selection',
                       worker_id=worker_id, attempt=attempt)
        self.bindings[worker_id] = binding
        return binding.copy()
    async def ensure_ready(self, binding):
        assert binding == self.bindings[binding['worker_id']]
        self.calls += 1
        self.entered.set()
        async with component_timeout('Public component', 5):
            if self.release is not None:
                await self.release.wait()
            if self.failure is not None:
                raise self.failure
    async def revoke(self, binding):
        if binding not in self.revoked:
            self.revoked.append(binding)


def run(coroutine):
    return asyncio.run(coroutine)


def descriptor(**updates):
    return dict(version=1, host='127.0.0.1', port=43123, capability=CAPABILITY, **updates)


def _process_client(endpoint, worker, result):
    client = SetupClient(endpoint, worker_id=worker, timeout=3)
    try:
        client.ensure_ready()
        client.ensure_ready()
        result.put('ready')
    finally:
        client.close()


async def raw_request(endpoint, worker='raw-worker'):
    reader, writer = await asyncio.open_connection(endpoint['host'], endpoint['port'])
    frame = dict(version=1, capability=CAPABILITY, action='register', timeout=3, worker_id=worker, attempt=1)
    writer.write(_encode(frame))
    await writer.drain()
    response = _decode(await reader.readline())
    return reader, writer, response['binding']


def test_descriptor_and_initialization_are_inert_and_private():
    value = descriptor()
    client = SetupClient(value)
    assert client._loop is None and client._writer is None
    assert CAPABILITY not in repr(client)
    value['port'] = 1
    assert client._descriptor['port'] == 43123
    client.close()
    client.close()
    with pytest.raises(SessionClosedError):
        client.ensure_ready()
    for field, invalid in [('version', True), ('version', 2), ('host', 'localhost'),
                           ('host', 'remote.example'), ('port', True), ('port', 0), ('port', 65536),
                           ('capability', 'short'), ('capability', 42)]:
        data = descriptor()
        data[field] = invalid
        with pytest.raises(ContractError):
            validate_descriptor(data)
    for invalid in [None, {}, {**descriptor(), 'extra': 1}]:
        with pytest.raises(ContractError):
            validate_descriptor(invalid)
    for timeout in [True, 0, -1, float('inf'), float('nan'), '3', 3601]:
        with pytest.raises(ContractError):
            SetupClient(descriptor(), timeout=timeout)
        with pytest.raises(ContractError):
            SetupServer(Registry(), CAPABILITY, timeout=timeout)
    for identifier in ['', 3, 'x'*513, '\n']:
        with pytest.raises(ContractError):
            SetupClient(descriptor(), worker_id=identifier)
    with pytest.raises(ContractError):
        SetupServer(Registry(), 'invalid')


def test_strict_codec_and_binding():
    valid = dict(version=1, operation_id='op', selected_identity='identity', worker_id='worker', attempt=1)
    assert _binding(valid) is valid
    for value in [None, {}, {**valid, 'extra': 1}, {**valid, 'version': True},
                  {**valid, 'version': 2}, {**valid, 'attempt': True}, {**valid, 'attempt': 0},
                  {**valid, 'operation_id': ''}]:
        with pytest.raises(ContractError):
            _binding(value)
    assert _decode(_encode({'safe': 'value'})) == {'safe': 'value'}
    for raw in [b'', b'{}', b'[]\n', b'not-json\n', b'\xff\n', b'{"a":1,"a":2}\n',
                b'{"a":NaN}\n', b'x'*(MAX_FRAME+1)+b'\n']:
        with pytest.raises(ContractError):
            _decode(raw)
    for value in [{'value': object()}, {'value': float('nan')}, {'value': 'x'*MAX_FRAME}]:
        with pytest.raises(ContractError):
            _encode(value)


def test_persistent_client_and_real_worker_processes():
    async def scenario():
        registry = Registry()
        server = SetupServer(registry, CAPABILITY, timeout=3)
        endpoint = await server.start()
        assert await server.start() == endpoint
        endpoint['port'] = 1
        endpoint = await server.start()
        client = SetupClient(endpoint, worker_id='thread-worker', timeout=3)
        events = []
        with progress_sink(events.append):
            await asyncio.to_thread(client.ensure_ready)
            await asyncio.to_thread(client.ensure_ready)
        assert len(registry.bindings) == 1 and registry.calls == 2
        assert any(event.name == 'Shared provider setup' for event in events)
        await asyncio.to_thread(client.close)
        assert len(registry.revoked) == 1
        context = multiprocessing.get_context('spawn')
        result = context.Queue()
        workers = [context.Process(target=_process_client, args=(endpoint, f'process-{index}', result)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            await asyncio.to_thread(worker.join, 8)
            assert worker.exitcode == 0
        assert [result.get(timeout=1) for _ in workers] == ['ready', 'ready']
        result.close()
        result.join_thread()
        await server.aclose()
        await server.aclose()
        assert len(registry.revoked) == 3
        with pytest.raises(SessionClosedError):
            await server.start()
    run(scenario())


@pytest.mark.parametrize('failure,expected', [
    (SetupPrerequisiteError('Selected provider has no completion subscription.'), SetupPrerequisiteError),
    (InteractionRequiredError('Complete attended browser setup.'), InteractionRequiredError),
    (UnattendedCredentialError('Credential access needs unlock.', index_source='dotenv'), UnattendedCredentialError),
    (RuntimeError('synthetic-secret-marker'), ContractError),
])
def test_safe_failure_types_and_no_raw_messages(failure, expected):
    async def scenario():
        registry = Registry()
        registry.failure = failure
        server = SetupServer(registry, CAPABILITY, timeout=3)
        endpoint = await server.start()
        client = SetupClient(endpoint, timeout=3)
        with pytest.raises(expected) as caught:
            await asyncio.to_thread(client.ensure_ready)
        assert 'synthetic-secret-marker' not in str(caught.value)
        if type(failure) is not RuntimeError:
            assert str(caught.value) == str(failure)
        assert CAPABILITY not in str(caught.value)
        await asyncio.to_thread(client.close)
        await server.aclose()
    run(scenario())


def test_disconnect_does_not_stop_sibling_readiness_and_progress_is_broadcast():
    async def scenario():
        registry = Registry()
        registry.release = asyncio.Event()
        server = SetupServer(registry, CAPABILITY, timeout=3)
        endpoint = await server.start()
        one, writer_one, binding_one = await raw_request(endpoint, 'one')
        two, writer_two, binding_two = await raw_request(endpoint, 'two')
        for writer, binding in [(writer_one, binding_one), (writer_two, binding_two)]:
            writer.write(_encode(dict(version=1, capability=CAPABILITY, action='ready', timeout=3, binding=binding)))
            await writer.drain()
        await registry.entered.wait()
        await asyncio.sleep(.01)
        server._progress(ComponentProgress('public-progress', 'synthetic-secret-marker', 3, .1, True))
        received = _decode(await two.readline())
        assert received['status'] == 'progress' and 'synthetic-secret-marker' not in str(received)
        writer_one.close()
        await writer_one.wait_closed()
        registry.release.set()
        while True:
            response = _decode(await two.readline())
            if response['status'] != 'progress':
                break
        assert response == {'version': 1, 'status': 'ready'}
        writer_two.close()
        await writer_two.wait_closed()
        await server.aclose()
        assert binding_one in registry.revoked and binding_two in registry.revoked
    run(scenario())


def test_request_guards_are_independent_and_precede_registry_side_effects():
    async def scenario():
        registry = Registry()
        server = SetupServer(registry, CAPABILITY)
        valid = dict(version=1, capability=CAPABILITY, action='register', timeout=3,
                     worker_id='public-worker', attempt=1)
        for updates in [{'version': True}, {'version': 2}, {'action': 'unknown'}, {'extra': 1},
                        {'capability': 'other-public-synthetic-capability-000000'},
                        {'timeout': float('nan')}, {'attempt': True}, {'attempt': 0}]:
            with pytest.raises(ContractError):
                server._request({**valid, **updates})
        assert registry.bindings == {}
        reader = SimpleNamespace(read=AsyncMock(return_value=b''))
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        peer = _Peer(reader, writer)
        with pytest.raises(ContractError):
            await server._dispatch(peer, {'binding': {}}, 'ready')
        response = await server._dispatch(peer, valid, 'register')
        assert response['status'] == 'registered'
        with pytest.raises(ContractError):
            await server._dispatch(peer, valid, 'register')
        forged = {**peer.binding, 'selected_identity': 'another-selection'}
        request = dict(version=1, capability=CAPABILITY, action='ready', timeout=3, binding=forged)
        assert server._request(request) == 'ready'
        with pytest.raises(ContractError):
            await server._dispatch(peer, request, 'ready')
        assert registry.calls == 0
        with pytest.raises(ContractError):
            await server._dispatch(peer, {'binding': peer.binding}, 'ready')
        await server.aclose()
    run(scenario())


def test_error_frames_strict_safe_detail_and_guidance():
    error = UnattendedCredentialError('Public credential access failed.', index_source='/public/.env')
    frame = _error(error)
    assert frame['message'] == error.detail and frame['index_source'] == 'dotenv'
    with pytest.raises(UnattendedCredentialError) as mapped:
        _raise_error(frame)
    assert str(mapped.value).count('Failed to get credentials') == 1
    override = _error(UnattendedCredentialError('Public credential access failed.', index_source='cli'))
    assert override['index_source'] == 'override'
    assert CAPABILITY not in _error(InteractionRequiredError(CAPABILITY), CAPABILITY)['message']
    assert len(_error(SetupPrerequisiteError('x'*4097))['message']) < 4096
    assert _error(SetupPrerequisiteError(''))['message']
    good = _error(None)
    for updates in [{'message': 3}, {'message': ''}, {'message': 'x'*4097}, {'category': []},
                    {'category': 'unknown'}, {'index_source': 'invalid'}, {'extra': 1},
                    {'index_source': 'dotenv'}]:
        with pytest.raises(ContractError):
            _raise_error({**good, **updates})


def test_progress_broadcast_is_bounded_and_invalid_events_are_ignored():
    from unittest.mock import Mock
    server = SetupServer(Registry(), CAPABILITY)
    writer = SimpleNamespace(write=Mock(), is_closing=lambda: False)
    peer = _Peer(None, writer, waiting=True)
    server._peers.add(peer)
    server._progress(ComponentProgress('child', 'Public child', 5, 1, True, 'parent'))
    event = _decode(writer.write.call_args.args[0])['component']
    assert event['parent_id'] is not None and event['name'] == 'Shared provider setup'
    peer.count = 1024
    server._progress(ComponentProgress('child', 'Public child', 5, 2, False, 'parent'))
    assert writer.write.call_count == 2
    server._ids = {str(index): str(index) for index in range(1024)}
    server._progress(ComponentProgress('extra', 'Public child', 5, 0, True))
    assert writer.write.call_count == 2
    server._progress(object())
    peer.count = 0
    server._ids = {}
    writer.write.side_effect = RuntimeError('private')
    server._progress(ComponentProgress('event', 'Public child', 5, 0, True))
    server._closed = True
    server._progress(ComponentProgress('event', 'Public child', 5, 0, True))


def test_server_startup_shutdown_races_and_failed_revocation_retain_retry(monkeypatch):
    from unittest.mock import Mock
    from api_agent.providers.control import CleanupPendingError
    async def scenario():
        registry = Registry()
        server = SetupServer(registry, CAPABILITY, timeout=.1)
        endpoint = await server.start()
        reader, writer, binding = await raw_request(endpoint)
        peer = next(iter(server._peers))
        registry.revoke = AsyncMock(side_effect=RuntimeError('private'))
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            await asyncio.sleep(0)
            if peer.dropping is not None and peer.dropping.done():
                break
        assert peer in server._peers
        with pytest.raises(CleanupPendingError) as error:
            await server.aclose()
        assert error.value.owners == (server,) and peer in server._peers
        registry.revoke.side_effect = None
        await server.aclose()
        assert not server._peers
        await SetupServer(Registry(), CAPABILITY).aclose()
        entered, release = asyncio.Event(), asyncio.Event()
        listener = SimpleNamespace(close=Mock(), wait_closed=AsyncMock(), sockets=[])
        async def start(*args, **kwargs):
            entered.set()
            await release.wait()
            return listener
        monkeypatch.setattr(asyncio, 'start_server', start)
        server = SetupServer(Registry(), CAPABILITY)
        starting = asyncio.create_task(server.start())
        await entered.wait()
        closing = asyncio.create_task(server.aclose())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(SessionClosedError):
            await starting
        await closing
        listener.close.assert_called()
        monkeypatch.setattr(asyncio, 'start_server', AsyncMock(side_effect=OSError('private')))
        server = SetupServer(Registry(), CAPABILITY)
        with pytest.raises(OSError):
            await server.start()
        await server.aclose()
    run(scenario())


def test_client_protocol_validation_progress_and_execution_context(monkeypatch):
    from unittest.mock import Mock
    async def response_case(response, action='ready', worker='public-worker'):
        client = SetupClient(descriptor(), worker_id=worker)
        reader = asyncio.StreamReader()
        reader.feed_data(_encode(response))
        reader.feed_eof()
        client._reader = reader
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        client._binding = dict(version=1, operation_id='op', selected_identity='identity', worker_id=worker, attempt=1)
        await client._exchange(action)
    async def scenario():
        for response in [{'version': True, 'status': 'ready'}, {'version': 2, 'status': 'ready'},
                         {'version': 1, 'status': 'unexpected'}, {'version': 1, 'status': 'ready', 'extra': 1}]:
            with pytest.raises(ContractError):
                await response_case(response)
        binding = dict(version=1, operation_id='op', selected_identity='identity', worker_id='another-worker', attempt=1)
        with pytest.raises(ContractError):
            await response_case(dict(version=1, status='registered', binding=binding), 'register')
        binding['worker_id'] = 'public-worker'
        binding['attempt'] = 2
        with pytest.raises(ContractError):
            await response_case(dict(version=1, status='registered', binding=binding), 'register')
        client = SetupClient(descriptor())
        with pytest.raises(ContractError):
            client.ensure_ready()
        with pytest.raises(ContractError):
            client.close()
    run(scenario())
    client = SetupClient(descriptor())
    event = asdict(ComponentProgress('public-event', 'Shared provider setup', 3, 1, True))
    frame = dict(version=1, status='progress', component=event)
    client._progress(frame)
    with progress_sink(lambda event: (_ for _ in ()).throw(RuntimeError('presentation'))):
        client._progress(frame)
    for invalid in [{**frame, 'extra': 1}, {**frame, 'component': None},
                    {**frame, 'component': {**event, 'name': 'private'}},
                    {**frame, 'component': {**event, 'elapsed': -1}}]:
        with pytest.raises(ContractError):
            client._progress(invalid)
    client._pid = -1
    with pytest.raises(ContractError):
        client.ensure_ready()
    with pytest.raises(ContractError):
        client.close()
    monkeypatch.setattr(asyncio, 'open_connection', AsyncMock(side_effect=OSError('private')))
    client = SetupClient(descriptor())
    with pytest.raises(ContractError, match='connection failed'):
        client.ensure_ready()
    assert client._closed and client._loop.is_closed()


def test_progress_identifier_union_is_bounded_and_existing_final_survives():
    from unittest.mock import Mock
    server = SetupServer(Registry(), CAPABILITY)
    writer = SimpleNamespace(write=Mock(), is_closing=lambda: False)
    peer = _Peer(None, writer, waiting=True)
    server._peers.add(peer)
    for index in range(1100):
        server._progress(ComponentProgress('same-child', 'Public child', 3, index, True, f'parent-{index}'))
    assert len(server._ids) == 1024 and len(server._parents) <= 1024
    peer.count = 1024
    before = writer.write.call_count
    server._progress(ComponentProgress('same-child', 'Public child', 3, 1101, False, 'unmapped-parent'))
    assert writer.write.call_count == before + 1
    closed = _decode(writer.write.call_args.args[0])['component']
    assert closed['active'] is False and closed['parent_id'] == server._parents['same-child']
    server._progress(ComponentProgress('same-child', 'Public child', 3, 1102, False, 'unmapped-parent'))
    assert writer.write.call_count == before + 1
    server._ids = {str(index): str(index) for index in range(1023)}
    server._progress(ComponentProgress('new-child', 'Public child', 3, 1, True, 'new-parent'))
    assert len(server._ids) == 1023
    server._progress(ComponentProgress('new-child', 'Public child', 3, 1, True, '0'))
    assert len(server._ids) == 1024 and server._parents['new-child'] == '0'


def test_real_malformed_registration_closes_socket_before_private_loop(monkeypatch):
    async def scenario():
        observed = []
        async def bad_server(reader, writer):
            await reader.readline()
            writer.write(b'{"version":true,"status":"registered"}\n')
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        listener = await asyncio.start_server(bad_server, '127.0.0.1', 0)
        endpoint = dict(version=1, host='127.0.0.1', port=listener.sockets[0].getsockname()[1], capability=CAPABILITY)
        original = asyncio.open_connection
        async def capture(*args, **kwargs):
            reader, writer = await original(*args, **kwargs)
            observed.append(writer.get_extra_info('socket').fileno())
            return reader, writer
        monkeypatch.setattr(asyncio, 'open_connection', capture)
        client = SetupClient(endpoint, timeout=1)
        with pytest.raises(ContractError):
            await asyncio.to_thread(client.ensure_ready)
        assert client._disposed and client._loop.is_closed()
        with pytest.raises(OSError):
            os.fstat(observed[0])
        await asyncio.to_thread(client.close)
        listener.close()
        await listener.wait_closed()
    run(scenario())


def test_client_failed_operation_and_pending_cleanup_keep_actionable_retry(monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"version":true,"status":"registered"}\n')
        gate = asyncio.Event()
        writer = SimpleNamespace(write=lambda value: None, drain=AsyncMock(),
                                 close=lambda: None, wait_closed=lambda: gate.wait())
        monkeypatch.setattr(asyncio, 'open_connection', AsyncMock(return_value=(reader, writer)))
        client = SetupClient(descriptor(), timeout=.03)
        with pytest.raises(CleanupPendingError) as error:
            await asyncio.to_thread(client.ensure_ready)
        assert error.value.owners == (client,)
        assert str(error.value.operation_error) == 'Shared setup connection failed'
        assert not client._disposed and not client._loop.is_closed()
        # Simulated wait_closed finishes on the client's creating loop on retry.
        writer.wait_closed = AsyncMock()
        await error.value.retry()
        assert client._disposed and client._loop.is_closed()
    run(scenario())


def test_partial_peers_and_pending_existing_drop_are_confirmed():
    from unittest.mock import Mock
    async def scenario():
        registry = Registry()
        server = SetupServer(registry, CAPABILITY)
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        server._accept(None, writer)
        peer = next(iter(server._peers))
        peer.task.cancel()
        await server.aclose()
        assert not server._peers
        writer.wait_closed.assert_awaited_once()
        server = SetupServer(registry, CAPABILITY)
        server._closed = True
        peer = _Peer(None, writer)
        server._peers.add(peer)
        await server._serve(peer)
        assert not server._peers
        server = SetupServer(registry, CAPABILITY)
        entered, release = asyncio.Event(), asyncio.Event()
        async def revoke(binding):
            entered.set()
            await release.wait()
        registry.revoke = revoke
        binding = registry.register('pending-drop')
        peer = _Peer(None, writer, binding=binding)
        server._peers.add(peer)
        peer.task = asyncio.create_task(asyncio.sleep(0))
        await peer.task
        peer.dropping = asyncio.create_task(server._drop(peer))
        await entered.wait()
        closing = asyncio.create_task(server.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert not server._peers
    run(scenario())


def test_client_remote_revoke_failure_still_confirms_socket_disposal():
    from unittest.mock import Mock
    client = SetupClient(descriptor())
    client._loop = asyncio.new_event_loop()
    client._reader = asyncio.StreamReader(loop=client._loop)
    client._reader.feed_data(_encode(_error(None)))
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(),
                                    wait_closed=AsyncMock(side_effect=OSError('disconnected')))
    client._binding = dict(version=1, operation_id='op', selected_identity='identity', worker_id=client._worker, attempt=1)
    client.close()
    assert client._disposed and client._loop.is_closed()
    client._writer.close.assert_called_once()


def test_client_repeated_pending_close_does_not_abandon_its_cleanup_task():
    import threading
    from api_agent.providers.control import CleanupPendingError
    from unittest.mock import Mock
    client = SetupClient(descriptor(), timeout=.03)
    client._loop = asyncio.new_event_loop()
    client._failed = True
    release = threading.Event()
    async def wait_closed():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(.001)
    client._writer = SimpleNamespace(close=Mock(), wait_closed=wait_closed)
    try:
        with pytest.raises(CleanupPendingError):
            client.close()
        task = client._closing_task
        with pytest.raises(CleanupPendingError):
            client.close()
        assert client._closing_task is task and not task.done()
        assert not client._disposed and not client._loop.is_closed()
    finally:
        release.set()
        client.close()
    assert client._disposed and client._loop.is_closed()


def test_endpoint_subclasses_and_cleanup_budget_cannot_weaken_boundaries():
    class Host(str):
        def __ne__(self, other):
            return False
    value = descriptor()
    value['host'] = Host('remote.invalid')
    with pytest.raises(ContractError):
        SetupClient(value)
    client = SetupClient(descriptor(), timeout=300)
    assert client._cleanup_timeout == 5
    assert SetupServer(Registry(), CAPABILITY, timeout=300)._timeout == 5
    for timeout in [False, 0, float('inf')]:
        with pytest.raises(ContractError):
            SetupClient(descriptor(), cleanup_timeout=timeout)
        with pytest.raises(ContractError):
            SetupServer(Registry(), CAPABILITY, cleanup_timeout=timeout)


def test_real_registry_coalesces_clients_and_survives_one_deadline():
    from api_agent.providers.setup_registry import SetupRegistry
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        class Owner:
            async def ensure_ready(self):
                calls.append('ready')
                entered.set()
                async with component_timeout('Public backend preparation', 3):
                    await release.wait()
            async def aclose(self):
                calls.append('closed')
        def factory():
            calls.append('factory')
            return Owner()
        registry = SetupRegistry('public-operation', 'frozen-selection', factory)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        endpoint = await server.start()
        reader, writer, binding = await raw_request(endpoint, 'surviving-worker')
        writer.write(_encode(dict(version=1, capability=CAPABILITY, action='ready', timeout=3, binding=binding)))
        await writer.drain()
        await entered.wait()
        deadline_client = SetupClient(endpoint, worker_id='deadline-worker', timeout=.04)
        with pytest.raises(ContractError):
            await asyncio.to_thread(deadline_client.ensure_ready)
        assert calls == ['factory', 'ready']
        assert not registry._ready.done()
        release.set()
        while True:
            response = _decode(await reader.readline())
            if response['status'] != 'progress':
                break
        assert response['status'] == 'ready'
        writer.close()
        await writer.wait_closed()
        await server.aclose()
        assert calls == ['factory', 'ready']
        await registry.aclose()
        assert calls == ['factory', 'ready', 'closed']
    run(scenario())


@pytest.mark.parametrize('pending', [False, True])
def test_remote_timeout_disposes_client_before_fixture_teardown(monkeypatch, pending):
    import threading
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    monkeypatch.setattr(RemoteIntegrationLLM, 'timeout', .2)
    async def scenario():
        entered, blocked = asyncio.Event(), asyncio.Event()
        release_cleanup = threading.Event()
        calls, events = [], []
        class Owner:
            async def ensure_ready(self):
                calls.append('ready')
                entered.set()
                await blocked.wait()
            async def aclose(self):
                calls.append('closed')
        registry = SetupRegistry('public-operation', 'frozen-selection', Owner)
        server = SetupServer(registry, CAPABILITY, timeout=2)
        endpoint = await server.start()
        remote = RemoteIntegrationLLM(endpoint)
        original_connect = asyncio.open_connection
        async def connect(*args, **kwargs):
            reader, writer = await original_connect(*args, **kwargs)
            if pending:
                original_wait = writer.wait_closed
                async def delayed_acknowledgement():
                    while not release_cleanup.is_set():
                        await asyncio.sleep(.001)
                    await original_wait()
                writer.wait_closed = delayed_acknowledgement
            return reader, writer
        monkeypatch.setattr(asyncio, 'open_connection', connect)
        try:
            expected = CleanupPendingError if pending else ContractError
            with progress_sink(events.append), pytest.raises(expected) as caught:
                await asyncio.to_thread(remote.ensure_ready)
            assert type(caught.value) is expected
            assert entered.is_set() and calls == ['ready']
            assert remote.client._writer.is_closing()
            # The server observes this real disconnect before session teardown.
            async with asyncio.timeout(2):
                while not registry._leases[remote.client._worker]['revoked']:
                    await asyncio.sleep(.001)
            assert not registry._ready.done()
            cleanup = [event for event in events if event.name in (
                'Worker setup disconnect', 'Worker transport cleanup')]
            assert {event.name for event in cleanup} == {
                'Worker setup disconnect', 'Worker transport cleanup'}
            assert all(event.timeout > 0 and event.parent_id is None for event in cleanup)
            assert all(any(event.name == name and not event.active for event in cleanup)
                       for name in ('Worker setup disconnect', 'Worker transport cleanup'))
            if pending:
                assert caught.value.owners == (remote.client,)
                assert str(caught.value.operation_error) == 'Shared setup connection failed'
                assert not remote.client._disposed and not remote.client._loop.is_closed()
                release_cleanup.set()
                await caught.value.retry()
            else:
                assert str(caught.value) == 'Shared setup connection failed'
            assert remote.client._disposed and remote.client._loop.is_closed()
            assert remote.client._writer.get_extra_info('socket').fileno() == -1
            assert not remote.loop.is_closed()
        finally:
            release_cleanup.set()
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
        assert remote.loop.is_closed() and calls == ['ready', 'closed']
    run(scenario())


@pytest.mark.parametrize('pending', [False, True])
def test_interrupt_preserves_request_and_socket_cleanup_ownership(monkeypatch, pending):
    client = SetupClient(descriptor(), timeout=.03)
    client._loop = asyncio.new_event_loop()
    reader = asyncio.StreamReader(loop=client._loop)
    gate = asyncio.Event()
    writer = SimpleNamespace(write=lambda value: None, drain=AsyncMock(), close=lambda: None,
                             wait_closed=(lambda: gate.wait()) if pending else AsyncMock())
    monkeypatch.setattr(asyncio, 'open_connection', AsyncMock(return_value=(reader, writer)))
    native = client._loop.run_until_complete
    interrupted = False
    def run_until_complete(awaitable):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            native(asyncio.sleep(.001))
            raise KeyboardInterrupt
        return native(awaitable)
    monkeypatch.setattr(client._loop, 'run_until_complete', run_until_complete)
    with pytest.raises(KeyboardInterrupt) as error:
        client.ensure_ready()
    assert client._request_task.cancelled()
    if pending:
        assert error.value.retained_owners == (client,)
        assert not client._disposed and not client._loop.is_closed()
        writer.wait_closed = AsyncMock()
        client.close()
    assert client._disposed and client._loop.is_closed()
