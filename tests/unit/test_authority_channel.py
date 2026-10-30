import asyncio
import hashlib

import pytest

from api_agent.providers.authority_channel import AuthorityChannel, _encode, _mac, _read, transfer
from api_agent.providers.contracts import ContractError

KEY = b'k' * 32
BINDING, TICKET, NONCE = 'a' * 64, 'b' * 64, 'c' * 64


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
def test_forged_or_reflected_server_challenge_never_receives_secret(tmp_path, role):
    async def run():
        observed = []
        path = str(tmp_path / 'fake')
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


def test_reflected_ack_rejected_before_confirmation(tmp_path):
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
        path = str(tmp_path / 'fake-ack')
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
