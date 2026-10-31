import asyncio
import hashlib

import pytest

from api_agent.providers.authority_channel import AuthorityChannel, _encode, _mac, _read, transfer
from api_agent.providers.contracts import ContractError

KEY = b'k' * 32
BINDING, TICKET, NONCE = 'a' * 64, 'b' * 64, 'c' * 64


@pytest.fixture
def short_fake_socket():
    import tempfile
    from pathlib import Path
    from api_agent.providers.authority_channel import _short_control_root
    with tempfile.TemporaryDirectory(prefix='af-', dir=_short_control_root()) as directory:
        yield str(Path(directory) / 'fake')


def test_call_binding_replay_and_incomplete_confirmations():
    async def run():
        admitted = set()
        received = []
        def admit(binding, ticket, sequence):
            identity = binding, ticket, sequence
            if identity != (BINDING, TICKET, 1) or identity in admitted:
                return None
            admitted.add(identity)
            return received.append
        channel = AuthorityChannel(KEY, admit)
        path = await channel.start()
        try:
            for binding, ticket, sequence in [(NONCE, TICKET, 1), (BINDING, NONCE, 1), (BINDING, TICKET, 2)]:
                with pytest.raises(ContractError):
                    await transfer(path, KEY, binding, ticket, sequence, ['synthetic-secret'])
            await transfer(path, KEY, BINDING, TICKET, 1, ['synthetic-secret'])
            await asyncio.sleep(0.01)
            assert received == [['synthetic-secret']]
            with pytest.raises(ContractError):
                await transfer(path, KEY, BINDING, TICKET, 1, ['synthetic-secret'])
        finally:
            await channel.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['hello', 'transfer', 'confirm'])
def test_disconnect_or_truncation_never_commits(stage):
    async def run():
        received = []
        channel = AuthorityChannel(KEY, lambda *_: received.append)
        path = await channel.start()
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            base = (BINDING, TICKET, 1, NONCE)
            hello = dict(version=1, binding=BINDING, ticket=TICKET, sequence=1, nonce=NONCE,
                         proof=_mac(KEY, 'client-hello', *base))
            if stage == 'hello':
                writer.write(_encode(hello)[:-1])
            else:
                writer.write(_encode(hello))
                await writer.drain()
                challenge = await _read(reader)
                identity = (*base, challenge['nonce'])
                values = ['synthetic-secret']
                digest = hashlib.sha256(_encode(values)).hexdigest()
                msg = dict(version=1, secrets=values, proof=_mac(KEY, 'client-transfer', *identity, digest))
                writer.write(_encode(msg) if stage == 'confirm' else _encode(msg)[:-1])
                if stage == 'confirm':
                    await writer.drain()
                    await _read(reader)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.01)
            assert received == []
        finally:
            await channel.close()
    asyncio.run(run())


@pytest.mark.parametrize('role', ['client-hello', 'client-transfer', 'server-ack', 'server-challenge'])
def test_forged_or_reflected_server_challenge_never_receives_secret(short_fake_socket, role):
    async def run():
        observed = []
        path = short_fake_socket
        async def fake(reader, writer):
            hello = await _read(reader)
            base = (hello['binding'], hello['ticket'], hello['sequence'], hello['nonce'])
            # Even a key-holder cannot reflect another protocol role as a challenge.
            proof = _mac(KEY, role, *base, NONCE) if role != 'server-challenge' else '0' * 64
            writer.write(_encode(dict(version=1, nonce=NONCE, proof=proof)))
            await writer.drain()
            observed.append(await reader.read())
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(fake, path)
        try:
            with pytest.raises(ContractError):
                await transfer(path, KEY, BINDING, TICKET, 1, ['synthetic-secret'])
            await asyncio.sleep(.01)
            assert observed == [b'']
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['hello', 'transfer', 'confirm'])
def test_reflected_client_roles_never_commit(stage):
    async def run():
        received = []
        channel = AuthorityChannel(KEY, lambda *_: received.append)
        path = await channel.start()
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            base = (BINDING, TICKET, 1, NONCE)
            proof = _mac(KEY, 'server-challenge' if stage == 'hello' else 'client-hello', *base)
            writer.write(_encode(dict(version=1, binding=BINDING, ticket=TICKET, sequence=1, nonce=NONCE, proof=proof)))
            await writer.drain()
            if stage != 'hello':
                challenge = await _read(reader)
                identity = (*base, challenge['nonce'])
                values = ['synthetic-secret']
                digest = hashlib.sha256(_encode(values)).hexdigest()
                role = 'server-ack' if stage == 'transfer' else 'client-transfer'
                writer.write(_encode(dict(version=1, secrets=values, proof=_mac(KEY, role, *identity, digest))))
                await writer.drain()
                if stage == 'confirm':
                    await _read(reader)
                    writer.write(_encode(dict(version=1, proof=_mac(KEY, 'server-ack', *identity, digest))))
                    await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
            assert received == []
        finally:
            await channel.close()
    asyncio.run(run())


def test_reflected_ack_rejected_before_confirmation(short_fake_socket):
    async def run():
        observed = []
        async def fake(reader, writer):
            hello = await _read(reader)
            base = (hello['binding'], hello['ticket'], hello['sequence'], hello['nonce'])
            identity = (*base, NONCE)
            writer.write(_encode(dict(version=1, nonce=NONCE, proof=_mac(KEY, 'server-challenge', *identity))))
            await writer.drain()
            body = await _read(reader)
            digest = hashlib.sha256(_encode(body['secrets'])).hexdigest()
            writer.write(_encode(dict(version=1, proof=_mac(KEY, 'client-transfer', *identity, digest))))
            await writer.drain()
            observed.append(await reader.read())
            writer.close()
            await writer.wait_closed()
        path = short_fake_socket
        server = await asyncio.start_unix_server(fake, path)
        try:
            with pytest.raises(ContractError):
                await transfer(path, KEY, BINDING, TICKET, 1, ['synthetic-secret'])
            await asyncio.sleep(.01)
            assert observed == [b'']
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_setup_server_actual_pending_admission_reserves_once():
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    async def run():
        registry = SetupRegistry('op', 'selected', object)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', authority=EmissionBoundary(ContentPolicy(True)))
        future = asyncio.get_running_loop().create_future()
        server._authority_pending[TICKET] = dict(binding=BINDING, sequence=1, future=future, reserved=False)
        assert server._admit_authority(NONCE, TICKET, 1) is None
        assert server._admit_authority(BINDING, NONCE, 1) is None
        assert server._admit_authority(BINDING, TICKET, 2) is None
        accept = server._admit_authority(BINDING, TICKET, 1)
        assert accept is not None
        assert server._admit_authority(BINDING, TICKET, 1) is None
        accept(['synthetic-secret'])
        accept(['other'])
        assert future.result() == ['synthetic-secret']
        assert server._admit_authority(BINDING, TICKET, 1) is None
        del server._authority_pending[TICKET]
        assert server._admit_authority(BINDING, TICKET, 1) is None
    asyncio.run(run())


def test_peer_uid_and_snapshot_limits(monkeypatch):
    import os
    import struct
    from api_agent.providers.authority_channel import _check_peer, _credentials
    class Socket:
        def getsockopt(self, *_):
            return struct.pack('3i', 1, os.geteuid() + 1, 1)
    class Writer:
        def get_extra_info(self, _):
            return Socket()
    with pytest.raises(ContractError, match='peer rejected'):
        _check_peer(Writer())
    assert _credentials(['x'] * 64) == ['x'] * 64
    assert _credentials(['x' * 16384]) == ['x' * 16384]
    for values in (['x'] * 65, ['x' * 16385], [''], [1]):
        with pytest.raises(ContractError):
            _credentials(values)


@pytest.mark.parametrize('uid_delta,result,accepted', [(0, 0, True), (1, 0, False), (0, -1, False)])
def test_darwin_getpeereid_authenticates_effective_uid(monkeypatch, uid_delta, result, accepted):
    import ctypes
    import os
    import socket
    import api_agent.providers.authority_channel as channel
    class Function:
        def __call__(self, fd, uid, gid):
            assert fd == 17
            ctypes.cast(uid, ctypes.POINTER(ctypes.c_uint))[0] = os.geteuid() + uid_delta
            ctypes.cast(gid, ctypes.POINTER(ctypes.c_uint))[0] = os.getgid()
            return result
    class Library:
        getpeereid = Function()
    class Peer:
        def fileno(self):
            return 17
    class Writer:
        def get_extra_info(self, _):
            return Peer()
    monkeypatch.delattr(socket, 'SO_PEERCRED', raising=False)
    monkeypatch.setattr(channel.sys, 'platform', 'darwin')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *_args, **_kwargs: Library())
    if accepted:
        channel._check_peer(Writer())
    else:
        with pytest.raises(ContractError):
            channel._check_peer(Writer())


def test_peer_identity_uses_effective_not_real_uid(monkeypatch):
    import os
    import socket
    import struct
    from api_agent.providers.authority_channel import _check_peer
    if not hasattr(socket, 'SO_PEERCRED'):
        pytest.skip('Linux peer credential path required')
    class Peer:
        def getsockopt(self, *_):
            return struct.pack('3i', 1, 1234, 1)
    class Writer:
        def get_extra_info(self, _):
            return Peer()
    monkeypatch.setattr(os, 'getuid', lambda: 4321)
    monkeypatch.setattr(os, 'geteuid', lambda: 1234)
    _check_peer(Writer())


@pytest.mark.parametrize('long_path', [False, True])
def test_authority_socket_handles_temporary_path_budget_in_real_process(tmp_path, long_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory(prefix='at-', dir='/tmp') as short:
        bulk = (tmp_path / ('bulk-' + 'x' * 120)) if long_path else Path(short)
        bulk.mkdir(exist_ok=True)
        program = '''
import asyncio, json, os
from pathlib import Path
from api_agent.providers.authority_channel import AuthorityChannel, transfer
from api_agent.providers.setup_transport import validate_descriptor
async def main():
    key = b'k' * 32
    received = []
    owner = AuthorityChannel(key, lambda *_: received.append)
    path = await owner.start()
    root = Path(path).parent
    validate_descriptor(dict(version=1, host='127.0.0.1', port=1234, capability='a'*32, authority_path=path))
    await transfer(path, key, 'a'*64, 'b'*64, 1, ['synthetic-secret'])
    await asyncio.sleep(.01)
    data = dict(path=path, mode=root.stat().st_mode & 0o777, received=received,
                original_tmp=os.environ['TMPDIR'])
    await owner.close()
    data['removed'] = not root.exists()
    print(json.dumps(data))
asyncio.run(main())
'''
        env = dict(os.environ, TMPDIR=str(bulk), PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.run([sys.executable, '-c', program], env=env, check=True,
                                 text=True, capture_output=True, timeout=20)
        row = json.loads(process.stdout)
        assert row['original_tmp'] == str(bulk) and row['mode'] == 0o700
        assert len(row['path'].encode()) <= 100 and row['removed']
        assert row['received'] == [['synthetic-secret']]
        assert Path(row['path']).is_relative_to(bulk) is not long_path
        assert not list(bulk.glob('api-agent-authority-*'))


@pytest.mark.parametrize('failure', [RuntimeError('synthetic bind failure'), asyncio.CancelledError()])
def test_authority_start_failure_or_cancel_cleans_socket_directory(monkeypatch, failure):
    from types import ModuleType
    import api_agent.providers.authority_channel as protocol
    fake_asyncio = ModuleType('isolated_asyncio')
    fake_asyncio.__dict__.update(vars(asyncio))
    async def fail(*args, **kwargs):
        raise failure
    fake_asyncio.start_unix_server = fail
    monkeypatch.setattr(protocol, 'asyncio', fake_asyncio)
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(type(failure)):
            await owner.start()
        assert not __import__('pathlib').Path(owner._directory.name).exists()
    asyncio.run(run())


def test_short_authority_root_rejects_nonsticky_and_symlink_target(tmp_path, monkeypatch):
    import api_agent.providers.authority_channel as protocol
    plain = tmp_path / 'plain'
    plain.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(plain, target_is_directory=True)
    monkeypatch.setattr(protocol, '_CONTROL_ROOTS', (str(tmp_path / 'missing'), str(link)))
    with pytest.raises(ContractError, match='trusted short directory'):
        protocol._short_control_root()


def test_short_authority_root_rejects_foreign_sticky_directory(tmp_path, monkeypatch):
    import os
    from types import ModuleType
    import api_agent.providers.authority_channel as protocol
    root = tmp_path / 'sticky'
    root.mkdir()
    root.chmod(0o1777)
    fake_os = ModuleType('isolated_os')
    fake_os.__dict__.update(vars(os))
    fake_os.geteuid = lambda: os.geteuid() + 1
    monkeypatch.setattr(protocol, 'os', fake_os)
    monkeypatch.setattr(protocol, '_CONTROL_ROOTS', (str(root),))
    with pytest.raises(ContractError, match='trusted short directory'):
        protocol._short_control_root()


def test_authority_start_rejects_unowned_leaf_and_cleans(monkeypatch):
    import os
    from types import ModuleType, SimpleNamespace
    import api_agent.providers.authority_channel as protocol
    fake_os = ModuleType('isolated_os')
    fake_os.__dict__.update(vars(os))
    fake_os.fstat = lambda descriptor: SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=0o40700)
    monkeypatch.setattr(protocol, 'os', fake_os)
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(ContractError, match='Invalid authority directory'):
            await owner.start()
        assert not __import__('pathlib').Path(owner._directory.name).exists()
    asyncio.run(run())


def test_authority_fallback_in_process_preserves_authentication(tmp_path, monkeypatch):
    from types import ModuleType
    import tempfile
    import api_agent.providers.authority_channel as protocol
    long_base = tmp_path / ('long-' + 'x' * 120)
    long_base.mkdir()
    fake_tempfile = ModuleType('isolated_tempfile')
    fake_tempfile.__dict__.update(vars(tempfile))
    def temporary(*args, **kwargs):
        if 'dir' not in kwargs:
            kwargs['dir'] = long_base
        return tempfile.TemporaryDirectory(*args, **kwargs)
    fake_tempfile.TemporaryDirectory = temporary
    monkeypatch.setattr(protocol, 'tempfile', fake_tempfile)
    async def run():
        received = []
        owner = AuthorityChannel(KEY, lambda *_: received.append)
        path = await owner.start()
        assert len(path.encode()) <= 100
        assert not __import__('pathlib').Path(path).is_relative_to(long_base)
        await transfer(path, KEY, BINDING, TICKET, 1, ['synthetic-secret'])
        await asyncio.sleep(.01)
        assert received == [['synthetic-secret']]
        await owner.close()
        assert not __import__('pathlib').Path(path).parent.exists()
        assert not list(long_base.iterdir())
    asyncio.run(run())


def test_short_authority_root_rejects_writable_ancestor(monkeypatch):
    import tempfile
    from pathlib import Path
    import api_agent.providers.authority_channel as protocol
    with tempfile.TemporaryDirectory(prefix='ar-', dir='/var/tmp') as directory:
        parent = Path(directory) / 'unsafe'
        parent.mkdir()
        parent.chmod(0o777)
        root = parent / 'sticky'
        root.mkdir()
        root.chmod(0o1777)
        monkeypatch.setattr(protocol, '_CONTROL_ROOTS', (str(root),))
        with pytest.raises(ContractError, match='trusted short directory'):
            protocol._short_control_root()


def test_authority_unavailable_fallback_cleans_long_candidate(tmp_path, monkeypatch):
    from types import ModuleType
    import tempfile
    import api_agent.providers.authority_channel as protocol
    long_base = tmp_path / ('long-' + 'x' * 120)
    long_base.mkdir()
    fake_tempfile = ModuleType('isolated_tempfile')
    fake_tempfile.__dict__.update(vars(tempfile))
    fake_tempfile.TemporaryDirectory = lambda **kw: tempfile.TemporaryDirectory(dir=long_base, **kw)
    monkeypatch.setattr(protocol, 'tempfile', fake_tempfile)
    monkeypatch.setattr(protocol, '_CONTROL_ROOTS', ())
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(ContractError, match='trusted short directory'):
            await owner.start()
        assert owner._directory is None and not list(long_base.iterdir())
    asyncio.run(run())


def test_authority_rejects_fallback_over_portable_limit(monkeypatch):
    import api_agent.providers.authority_channel as protocol
    monkeypatch.setattr(protocol, '_SOCKET_LIMIT', 1)
    monkeypatch.setattr(protocol, '_short_control_root', lambda: '/var/tmp')
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(ContractError, match='portable path limit'):
            await owner.start()
        assert not __import__('pathlib').Path(owner._directory.name).exists()
    asyncio.run(run())


def test_authority_server_cleanup_preserves_start_error(monkeypatch):
    import os
    from types import ModuleType
    import api_agent.providers.authority_channel as protocol
    fake_os = ModuleType('isolated_os')
    fake_os.__dict__.update(vars(os))
    def fail_chmod(*args, **kwargs):
        raise OSError('synthetic socket chmod failure')
    fake_os.chmod = fail_chmod
    monkeypatch.setattr(protocol, 'os', fake_os)
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(OSError, match='synthetic socket chmod failure'):
            await owner.start()
        assert not owner._server.is_serving()
        assert not __import__('pathlib').Path(owner._directory.name).exists()
    asyncio.run(run())


def test_authority_cleanup_error_does_not_replace_start_error(monkeypatch):
    import tempfile
    from types import ModuleType
    import api_agent.providers.authority_channel as protocol
    fake_tempfile = ModuleType('isolated_tempfile')
    fake_tempfile.__dict__.update(vars(tempfile))
    class Directory:
        def __init__(self, **kwargs):
            kwargs.setdefault('dir', '/var/tmp')
            self.inner = tempfile.TemporaryDirectory(**kwargs)
            self.name = self.inner.name
        def cleanup(self):
            self.inner.cleanup()
            raise RuntimeError('synthetic cleanup failure')
    fake_tempfile.TemporaryDirectory = Directory
    fake_asyncio = ModuleType('isolated_asyncio')
    fake_asyncio.__dict__.update(vars(asyncio))
    async def fail(*args, **kwargs):
        raise ValueError('synthetic primary failure')
    fake_asyncio.start_unix_server = fail
    monkeypatch.setattr(protocol, 'tempfile', fake_tempfile)
    monkeypatch.setattr(protocol, 'asyncio', fake_asyncio)
    async def run():
        owner = AuthorityChannel(KEY, lambda *_: None)
        with pytest.raises(ValueError, match='synthetic primary failure') as error:
            await owner.start()
        assert any('RuntimeError' in note for note in error.value.__notes__)
        assert not __import__('pathlib').Path(owner._directory.name).exists()
    asyncio.run(run())


def test_authority_cancel_after_real_listener_registration_drains_owner(tmp_path, monkeypatch):
    from pathlib import Path
    import tempfile
    from types import ModuleType
    import api_agent.providers.authority_channel as protocol
    if not Path('/proc/self/fd').is_dir():
        pytest.skip('descriptor inventory unavailable')
    long_base = tmp_path / ('long-' + 'x' * 110)
    long_base.mkdir()
    fake_tempfile = ModuleType('isolated_tempfile')
    fake_tempfile.__dict__.update(vars(tempfile))
    def temporary(**kwargs):
        kwargs.setdefault('dir', long_base)
        return tempfile.TemporaryDirectory(**kwargs)
    fake_tempfile.TemporaryDirectory = temporary
    monkeypatch.setattr(protocol, 'tempfile', fake_tempfile)
    async def run():
        loop = asyncio.get_running_loop()
        original = loop._start_serving
        current = asyncio.current_task()
        servers = []
        before = len(tuple(Path('/proc/self/fd').iterdir()))
        def register(*args, **kwargs):
            original(*args, **kwargs)
            server = kwargs.get('server', args[3] if len(args) > 3 else None)
            assert server is not None
            servers.append(server)
            loop.call_soon(current.cancel)
        monkeypatch.setattr(loop, '_start_serving', register)
        owner = AuthorityChannel(KEY, lambda *_: None)
        try:
            with pytest.raises(asyncio.CancelledError):
                await owner.start()
            assert servers and all(not server.is_serving() for server in servers)
            assert not Path(owner._directory.name).exists()
            await owner.close()
            assert len(tuple(Path('/proc/self/fd').iterdir())) == before
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()
            await owner.close()
    asyncio.run(run())
