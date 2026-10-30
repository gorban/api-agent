import io
import os
import threading

from api_agent.auth.interaction import Reply
from api_agent.auth.interaction_transport import InteractionClient, InteractionServer, OwnedInteraction, validate_descriptor
from api_agent.auth.terminal_interaction import TerminalInteraction


class Channel:
    def __init__(self):
        self.output = []
        self.closed = False
    def write(self, text):
        self.output.append(text)
    def read(self, timeout):
        return Reply('cancel' if self.closed else 'input', 'secret')
    def close(self):
        self.closed = True


class Interaction:
    def __init__(self):
        self.channel = Channel()
        self.lock = threading.RLock()
    def open(self, label):
        return self.channel


def test_bridge_keeps_transcript_in_dedicated_boundary(capsys):
    interaction = Interaction()
    server = InteractionServer(interaction)
    try:
        client = InteractionClient(server.descriptor)
        channel = client.open('Keeper')
        channel.write('live login transcript')
        reply = channel.read(.01)
        assert reply.value == 'secret'
        assert 'secret' not in repr(reply)
        assert interaction.channel.output == ['live login transcript']
        assert capsys.readouterr() == ('', '')
        channel.close()
        assert channel.read(.01).action == 'cancel'
    finally:
        server.close()
    assert client.open('Keeper').read(.01).action == 'cancel'


def test_operation_cancel_disposes_channels_and_prevents_new_login():
    interaction = Interaction()
    owned = OwnedInteraction(interaction)
    channel = owned.open('Keeper')
    owned.cancel()
    assert not interaction.channel.closed
    assert channel.read(0).action == 'cancel'
    channel.close()
    assert interaction.channel.closed
    assert owned.open('Keeper').read(.01).action == 'cancel'


def test_terminal_pipe_needs_no_tty_and_flushes_without_secret_echo():
    read_fd, write_fd = os.pipe()
    output = io.StringIO()
    with os.fdopen(read_fd) as stream:
        channel = TerminalInteraction(stream, output).open('Keeper')
        assert channel.read(0) is None
        channel.write('')
        channel.write('Password: ')
        assert output.getvalue() == 'Authentication: Keeper\nPassword: '
        assert channel.read(0) is None
        os.write(write_fd, b'private\nsecond\n')
        assert channel.read(.1).value == 'private'
        assert channel.read(0) is None
        channel.write('Password: ')
        assert channel.read(.1).value == 'second'
        os.close(write_fd)
        channel.write('Password: ')
        assert channel.read(.1).action == 'eof'
        channel.close()
        assert channel.read(.1).action == 'cancel'
        assert output.getvalue() == 'Authentication: Keeper\n' + 'Password: ' * 3


def test_endpoint_validation():
    import pytest
    for descriptor in ({}, {'port': True, 'token': '0' * 64}, {'port': 1, 'token': 'x' * 64}):
        with pytest.raises(ValueError):
            validate_descriptor(descriptor)


def test_protected_snapshot_roundtrip_connects_worker(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from api_agent.testing.provider_setup import write_launch_snapshot, _read_snapshot, _worker_interaction
    interaction = Interaction()
    server = InteractionServer(interaction)
    registry = SimpleNamespace(definitions=())
    context = SimpleNamespace(owner_root=tmp_path, external_pack=False)
    try:
        snapshot = write_launch_snapshot(tmp_path, context, {'DEBUG': True},
            password_manager_registry=registry, interaction_descriptor=server.descriptor)
        assert snapshot.stat().st_mode & 0o777 == 0o600
        monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(snapshot))
        worker = _worker_interaction(_read_snapshot())
        worker.open('Keeper').write('login only')
        assert interaction.channel.output == ['login only']
        assert 'login only' not in snapshot.read_text()
    finally:
        server.close()


def test_backend_provider_receives_operation_interaction(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from api_agent.testing.setup_backend import SetupBackend, _SetupOwner
    from api_agent.testing import provider_setup
    captured = {}
    class Provider:
        def __init__(self, config, root, **options):
            captured.update(options)
    monkeypatch.setattr(provider_setup, 'IntegrationLLM', Provider)
    interaction = Interaction()
    backend = SetupBackend({}, tmp_path, interaction=interaction,
                           password_manager_registry=SimpleNamespace(definitions=()))
    async def create():
        _SetupOwner(backend)
    asyncio.run(create())
    assert captured['interaction'] is interaction
    backend.close()


def test_integration_recovery_budget_and_safe_error(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from api_agent.auth import credentials
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.providers.progress import report_timeout
    config = {'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': 'keeper'},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}}}
    interaction = Interaction()
    provider = IntegrationLLM(config, tmp_path, interaction=interaction)
    called = {}
    def helper(source, **options):
        called.update(options)
        raise credentials.CredentialRecoveryError('Password manager recovery input ended; retry.')
    monkeypatch.setattr(credentials, 'get_password', helper)
    async def run():
        async with report_timeout('Enclosing setup', 30):
            from api_agent.providers.contracts import UnattendedCredentialError
            with pytest.raises(UnattendedCredentialError, match='input ended'):
                await provider._credentials(0)(0, 'password')
    try:
        provider.loop.run_until_complete(run())
        assert called['timeout'] == credentials.DEFAULT_TIMEOUT
        assert 15 < called['recovery_timeout'] <= 30
        assert called['interaction'].interaction is interaction
    finally:
        provider.close()


def test_debug_bridge_real_synthetic_keeper_recovery(tmp_path, capsys):
    import sys
    from api_agent.testing.provider_setup import IntegrationLLM
    cli = tmp_path / 'synthetic-keeper'
    cli.write_text('#!' + sys.executable + '\n'
        'import os,sys\n'
        'if not os.isatty(0):\n'
        ' sys.stderr.write("Keeper login required\\n"); sys.exit(1)\n'
        'sys.stderr.write("Login transcript Password: "); sys.stderr.flush()\n'
        'value=sys.stdin.readline().strip()\n'
        'if value != "secret": sys.exit(2)\n'
        'sys.stdout.write("retrieved-only\\n")\n')
    cli.chmod(0o700)
    interaction = Interaction()
    server = InteractionServer(interaction)
    provider = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': str(cli)},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}}},
        tmp_path, interaction=InteractionClient(server.descriptor))
    try:
        assert provider.loop.run_until_complete(provider._credentials(0)(0, 'password')) == 'retrieved-only'
        assert 'Login transcript' in ''.join(interaction.channel.output)
        assert 'retrieved-only' not in ''.join(interaction.channel.output)
        assert capsys.readouterr() == ('', '')
    finally:
        provider.close()
        server.close()


def test_terminal_masks_real_tty_restores_echo_and_bounds_partial_input(monkeypatch):
    import termios
    from api_agent.auth import terminal_interaction as module
    master, slave = os.openpty()
    with os.fdopen(slave) as stream:
        original = termios.tcgetattr(stream.fileno())
        channel = TerminalInteraction(stream, io.StringIO()).open('Keeper')
        assert not termios.tcgetattr(stream.fileno())[3] & termios.ECHO
        channel.close()
        channel.write('discarded')
        assert termios.tcgetattr(stream.fileno()) == original
    os.close(master)
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            channel = TerminalInteraction(stream, io.StringIO()).open('Keeper')
            channel.write('Password: ')
            os.write(write_fd, b'a')
            assert channel.read(.01) is None
            monkeypatch.setattr(module.os, 'read', lambda *args: b'x' * 4096)
            os.write(write_fd, b'b')
            assert channel.read(.01).action == 'cancel'
            channel.close()
    finally:
        os.close(write_fd)


def test_bridge_rejects_capability_and_bounds_frames():
    import socket
    import struct
    import pytest
    from api_agent.auth.interaction_transport import _receive, _send
    server = InteractionServer(Interaction())
    try:
        for row in ([], {'token': '0' * 64}, {'token': server.descriptor['token'], 'action': 'open', 'label': 4}):
            with socket.create_connection(('127.0.0.1', server.descriptor['port']), timeout=1) as stream:
                _send(stream, row)
                with pytest.raises(EOFError):
                    _receive(stream)
        left, right = socket.socketpair()
        try:
            with pytest.raises(ValueError):
                _send(left, 'x' * 65536)
            left.sendall(struct.pack('!I', 65537))
            with pytest.raises(ValueError):
                _receive(right)
        finally:
            left.close()
            right.close()
    finally:
        server.close()
    assert server._dispatch({'action': 'open'})['action'] == 'cancel'


def test_bridge_dispatch_validates_reply_and_channel_actions(monkeypatch):
    import pytest
    interaction = Interaction()
    server = InteractionServer(interaction)
    client = InteractionClient(server.descriptor)
    channel = client.open('Keeper')
    try:
        for row in ({'action': 'write', 'text': 4}, {'action': 'read', 'timeout': 2}, {'action': 'other'}):
            with pytest.raises(ValueError):
                server._dispatch(dict(row, channel=channel.identifier))
        monkeypatch.setattr(interaction.channel, 'read', lambda timeout: None)
        assert channel.read(0) is None
        monkeypatch.setattr(interaction.channel, 'read', lambda timeout: Reply('invalid'))
        with pytest.raises(ValueError):
            server._dispatch({'action': 'read', 'timeout': 0, 'channel': channel.identifier})
        monkeypatch.setattr(client, '_call', lambda **kwargs: {'action': 'invalid', 'value': ''})
        assert channel.read(0).action == 'cancel'
    finally:
        server.close()


def test_owned_channel_closed_operations_are_inert():
    owned = OwnedInteraction(Interaction())
    channel = owned.open('Keeper')
    channel.write('visible')
    assert channel.read(0).action == 'input'
    channel.close()
    channel.close()
    channel.write('hidden')
    assert channel.read(0).action == 'cancel'
    owned.cancel()
    cancelled = owned.open('Keeper')
    cancelled.write('hidden')
    cancelled.close()


def test_integration_cancellation_drains_helper_before_return(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from api_agent.auth import credentials
    from api_agent.testing.provider_setup import IntegrationLLM
    started = threading.Event()
    ended = threading.Event()
    interaction = Interaction()
    provider = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': 'keeper'},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}}}, tmp_path, interaction=interaction)
    def helper(source, **options):
        channel = options['interaction'].open('Keeper')
        started.set()
        while channel.read(0).action != 'cancel':
            threading.Event().wait(.001)
        channel.close()
        ended.set()
        raise credentials.CredentialRecoveryError('Password manager recovery cancelled.')
    monkeypatch.setattr(credentials, 'get_password', helper)
    async def run():
        task = asyncio.create_task(provider._credentials(0)(0, 'password'))
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set()
        assert not provider.interaction.cancel_event.is_set()
    try:
        provider.loop.run_until_complete(run())
    finally:
        provider.close()


def test_terminal_preserves_operation_input_between_channels():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'first\nsecond\n')
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        interaction = TerminalInteraction(stream, io.StringIO())
        first = interaction.open('First')
        first.write('Password: ')
        assert first.read(.1).value == 'first'
        first.close()
        second = interaction.open('Second')
        second.write('Password: ')
        assert second.read(.1).value == 'second'
        second.close()


def test_cancel_request_keeps_sibling_active_and_defers_ui_disposal():
    interaction = Interaction()
    operation = OwnedInteraction(interaction)
    first, second = operation.fork(), operation.fork()
    channel = first.open('Keeper')
    second.cancel()
    assert channel.read(0).action == 'input'
    assert not interaction.channel.closed
    second.close()
    operation.cancel()
    assert channel.read(0).action == 'cancel'
    assert not interaction.channel.closed
    operation.close()
    assert interaction.channel.closed
    late = operation.fork()
    assert late.cancel_event.is_set()
    late.close()


def test_cancel_queued_real_helper_does_not_dispose_active_sibling(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from api_agent.auth import credentials
    from api_agent.testing.provider_setup import IntegrationLLM
    active, release = threading.Event(), threading.Event()
    interaction = Interaction()
    def retrieve(source, kind, timeout, **options):
        channel = options['interaction'].open('Keeper')
        active.set()
        try:
            assert release.wait(3)
            assert channel.read(0).action == 'input'
            return 'private'
        finally:
            channel.close()
    monkeypatch.setattr(credentials, '_retrieve', retrieve)
    provider = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': 'keeper'},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}}}, tmp_path, interaction=interaction)
    async def run():
        reader = provider._credentials(0)
        first = asyncio.create_task(reader(0, 'password'))
        while not active.is_set():
            await asyncio.sleep(.001)
        second = asyncio.create_task(reader(0, 'totp'))
        await asyncio.sleep(.02)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not interaction.channel.closed
        release.set()
        assert await first == 'private'
    try:
        provider.loop.run_until_complete(run())
    finally:
        release.set()
        provider.close()


def test_server_shutdown_interrupts_incomplete_unauthenticated_frame():
    import socket
    import struct
    import time
    server = InteractionServer(Interaction())
    peer = socket.create_connection(('127.0.0.1', server.descriptor['port']))
    try:
        peer.sendall(struct.pack('!I', 1000) + b' ')
        deadline = time.monotonic() + 1
        while not server.connections and time.monotonic() < deadline:
            time.sleep(.001)
        assert server.connections
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 1.5
        assert not server.connections
    finally:
        peer.close()
        if not server.closed:
            server.close()


def test_frame_absolute_deadline_and_closed_accept(monkeypatch):
    import pytest
    import socket
    from api_agent.auth import interaction_transport as module
    class Stream:
        def settimeout(self, timeout):
            pass
        def recv(self, size):
            return b'\0'
    values = iter((0, 1, 3))
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(values))
    with pytest.raises(TimeoutError):
        module._receive(Stream())
    monkeypatch.undo()
    server = InteractionServer(Interaction())
    try:
        with server.lock:
            server.closed = True
        with socket.create_connection(('127.0.0.1', server.descriptor['port']), timeout=1) as peer:
            assert peer.recv(1) == b''
        class Closed:
            def shutdown(self, how):
                raise OSError
        server.connections.add(Closed())
    finally:
        server.close()


def test_operation_shutdown_drains_helpers_despite_provider_disposal_error(tmp_path):
    import asyncio
    import pytest
    from api_agent.testing.provider_setup import IntegrationLLM
    interaction = Interaction()
    provider = IntegrationLLM({}, tmp_path, interaction=interaction)
    channel = provider.interaction.fork().open('Keeper')
    disposed = []
    class Provider:
        async def aclose(self):
            raise RuntimeError('disposal failed')
    provider.provider = Provider()
    async def helper():
        while channel.read(0).action != 'cancel':
            await asyncio.sleep(0)
        assert not interaction.channel.closed
        disposed.append('process reaped')
        channel.close()
    async def run():
        task = asyncio.create_task(helper())
        provider._credential_tasks.add(task)
        with pytest.raises(RuntimeError, match='disposal failed'):
            await provider._close()
        assert task.done() and disposed == ['process reaped']
        assert interaction.channel.closed
    try:
        provider.loop.run_until_complete(run())
    finally:
        provider.provider = None
        provider.close()


def test_server_disposes_listener_when_channel_close_fails(monkeypatch):
    import pytest
    interaction = Interaction()
    server = InteractionServer(interaction)
    InteractionClient(server.descriptor).open('Keeper')
    def fail():
        raise RuntimeError('closed UI unavailable')
    original = interaction.channel.close
    monkeypatch.setattr(interaction.channel, 'close', fail)
    with pytest.raises(RuntimeError, match='^Interaction cleanup failed$'):
        server.close()
    assert not server.thread.is_alive()
    assert server.channels
    monkeypatch.setattr(interaction.channel, 'close', original)
    server.close()
    assert not server.channels


def test_terminal_fragmented_prompts_preserve_input_after_progress():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'first\nsecond\nthird\nfourth\n')
        with os.fdopen(read_fd) as stream:
            channel = TerminalInteraction(stream, io.StringIO()).open('Keeper')
            channel.write('Pass')
            assert channel.read(0) is None
            channel.write('word: ')
            assert channel.read(.1).value == 'first'
            channel.write('Authentication ')
            assert channel.read(0) is None
            channel.write('succeeded')
            assert channel.read(0) is None
            channel.write('\n')
            assert channel.read(0) is None
            channel.write('Verification code? ')
            assert channel.read(.1).value == 'second'
            channel.write('Continue [y')
            assert channel.read(0) is None
            channel.write('/n] ')
            assert channel.read(.1).value == 'third'
            channel.write('Status: complete\nPassword: ')
            assert channel.read(.1).value == 'fourth'
            assert channel.read(0) is None
            channel.close()
    finally:
        os.close(write_fd)
