"""Bounded authenticated local handoff of per-call credential authorities."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
import tempfile

from .contracts import ContractError

_LIMIT = 32768


def _encode(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode() + b'\n'
    if len(raw) > _LIMIT:
        raise ContractError('Authority handoff exceeds limit')
    return raw


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


async def _read(reader):
    try:
        raw = await reader.readline()
        if len(raw) > _LIMIT or not raw.endswith(b'\n'):
            raise ValueError()
        result = json.loads(raw, object_pairs_hook=_pairs)
        if type(result) is not dict:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, OSError):
        raise ContractError('Invalid authority handoff') from None


def _mac(key, role, *values):
    return hmac.new(key, _encode([role, *values]), hashlib.sha256).hexdigest()


def _nonce(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _credentials(values):
    if (type(values) is not list or len(values) > 64
            or any(type(value) is not str or not value for value in values)
            or sum(len(value.encode('utf-8')) for value in values) > 16384):
        raise ContractError('Invalid authority snapshot')
    return values


def _check_peer(writer):
    peer = writer.get_extra_info('socket')
    if peer is None:
        raise ContractError('Authority peer identity unavailable')
    try:
        if hasattr(socket, 'SO_PEERCRED'):
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        elif sys.platform == 'darwin':
            # getpeereid returns the kernel-authenticated effective Unix UID.
            import ctypes
            uid_value, gid_value = ctypes.c_uint(), ctypes.c_uint()
            getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
            getpeereid.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))
            getpeereid.restype = ctypes.c_int
            if getpeereid(peer.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
                raise OSError('peer identity unavailable')
            uid = uid_value.value
        else:
            raise OSError('peer identity unavailable')
    except (OSError, AttributeError, TypeError, ValueError, struct.error):
        raise ContractError('Authority peer identity unavailable') from None
    if uid != os.geteuid():
        raise ContractError('Authority peer rejected')


class AuthorityChannel:
    """Operation-owned endpoint; admission binds every transfer to one call."""
    def __init__(self, key, admission, *, snapshot_source=None):
        if type(key) is not bytes or len(key) < 32 or not callable(admission):
            raise ContractError('Invalid authority channel configuration')
        self._key, self._admission = key, admission
        if snapshot_source is not None and not callable(snapshot_source):
            raise ContractError('Invalid authority snapshot source')
        self._snapshot_source = snapshot_source
        self._directory = self._server = None
        self._tasks = set()

    async def start(self):
        self._directory = tempfile.TemporaryDirectory(prefix='api-agent-authority-')
        os.chmod(self._directory.name, 0o700)
        path = str(Path(self._directory.name) / 'channel')
        self._server = await asyncio.start_unix_server(self._accept, path, limit=_LIMIT)
        os.chmod(path, 0o600)
        return path

    def _accept(self, reader, writer):
        if len(self._tasks) >= 128:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader, writer):
        try:
            async with asyncio.timeout(5) as deadline:
                _check_peer(writer)
                hello = await _read(reader)
                if hello.get('purpose') == 'graph-capture':
                    await self._serve_snapshot(reader, writer, hello, deadline)
                    return
                if (set(hello) != {'version', 'binding', 'ticket', 'sequence', 'nonce', 'proof'}
                        or type(hello['version']) is not int or hello['version'] != 1
                        or not _nonce(hello['binding']) or not _nonce(hello['ticket']) or not _nonce(hello['nonce'])
                        or type(hello['sequence']) is not int or not 1 <= hello['sequence'] <= 2**63 - 1
                        or type(hello['proof']) is not str
                        or not hmac.compare_digest(hello['proof'], _mac(self._key, 'client-hello',
                            hello['binding'], hello['ticket'], hello['sequence'], hello['nonce']))):
                    raise ContractError('Authority handoff rejected')
                # Reserve a one-shot, operation-owned call before the client
                # receives a server proof or sends credential material.
                receipt = self._admission(hello['binding'], hello['ticket'], hello['sequence'])
                if receipt is None:
                    raise ContractError('Authority call binding rejected')
                nonce = secrets.token_hex(32)
                identity = (hello['binding'], hello['ticket'], hello['sequence'], hello['nonce'], nonce)
                writer.write(_encode(dict(version=1, nonce=nonce, proof=_mac(self._key, 'server-challenge', *identity))))
                await writer.drain()
                transfer = await _read(reader)
                if (set(transfer) != {'version', 'secrets', 'proof'} or type(transfer['version']) is not int
                        or transfer['version'] != 1 or type(transfer['proof']) is not str):
                    raise ContractError('Invalid authority transfer')
                values = _credentials(transfer['secrets'])
                digest = hashlib.sha256(_encode(values)).hexdigest()
                if not hmac.compare_digest(transfer['proof'], _mac(self._key, 'client-transfer', *identity, digest)):
                    raise ContractError('Authority transfer rejected')
                writer.write(_encode(dict(version=1, proof=_mac(self._key, 'server-ack', *identity, digest))))
                await writer.drain()
                confirmation = await _read(reader)
                if (set(confirmation) != {'version', 'proof'} or type(confirmation['version']) is not int
                        or confirmation['version'] != 1 or type(confirmation['proof']) is not str
                        or not hmac.compare_digest(confirmation['proof'], _mac(self._key, 'client-confirm', *identity, digest))):
                    raise ContractError('Authority confirmation rejected')
                receipt(values)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _serve_snapshot(self, reader, writer, hello, deadline):
        """Credential-only enrollment; node content never enters this channel."""
        if (self._snapshot_source is None
                or set(hello) != {'version', 'purpose', 'binding', 'nonce', 'proof'}
                or type(hello['version']) is not int or hello['version'] != 1
                or type(hello['binding']) is not dict or not _nonce(hello['nonce'])
                or type(hello['proof']) is not str
                or not hmac.compare_digest(hello['proof'], _mac(self._key,
                    'graph-client-hello', hello['binding'], hello['nonce']))):
            raise ContractError('Graph authority admission rejected')
        binding, client_nonce = hello['binding'], hello['nonce']
        # Check ownership before the challenge and again after confirmation.
        self._snapshot_source(binding)
        nonce = secrets.token_hex(32)
        identity = (binding, client_nonce, nonce)
        writer.write(_encode(dict(version=1, nonce=nonce,
            proof=_mac(self._key, 'graph-server-challenge', *identity))))
        await writer.drain()
        confirmation = await _read(reader)
        if (set(confirmation) != {'version', 'proof'} or type(confirmation['version']) is not int
                or confirmation['version'] != 1 or type(confirmation['proof']) is not str
                or not hmac.compare_digest(confirmation['proof'],
                    _mac(self._key, 'graph-client-confirm', *identity))):
            raise ContractError('Graph authority confirmation rejected')
        boundary = self._snapshot_source(binding)
        lease = boundary.begin_evidence_lease()
        try:
            _, snapshot = boundary.snapshot_content()
            values = _credentials(sorted(snapshot._secrets))
            digest = hashlib.sha256(_encode(values)).hexdigest()
            # No await separates the final binding check and credential release.
            if self._snapshot_source(binding)._authority is not boundary._authority:
                raise ContractError('Graph credential authority changed')
            writer.write(_encode(dict(version=1, secrets=values,
                proof=_mac(self._key, 'graph-server-snapshot', *identity, digest))))
            await writer.drain()
            receipt = await _read(reader)
            if (set(receipt) != {'version', 'proof'} or type(receipt['version']) is not int
                    or receipt['version'] != 1 or type(receipt['proof']) is not str
                    or not hmac.compare_digest(receipt['proof'],
                        _mac(self._key, 'graph-client-receipt', *identity, digest))):
                raise ContractError('Graph authority receipt rejected')
            if self._snapshot_source(binding)._authority is not boundary._authority:
                raise ContractError('Graph credential authority changed')
            deadline.reschedule(None)
            writer.write(_encode(dict(version=1,
                proof=_mac(self._key, 'graph-server-ready', *identity, digest))))
            await writer.drain()
            # A lease cannot expire while its consumer may still be committing.
            # Channel shutdown/EOF ends ownership; concurrency remains bounded.
            release = await _read(reader)
            if (set(release) != {'version', 'proof'} or type(release['version']) is not int
                    or release['version'] != 1 or type(release['proof']) is not str
                    or not hmac.compare_digest(release['proof'],
                        _mac(self._key, 'graph-client-release', *identity, digest))):
                raise ContractError('Graph authority release rejected')
            lease.release()
            writer.write(_encode(dict(version=1,
                proof=_mac(self._key, 'graph-server-released', *identity, digest))))
            await writer.drain()
        finally:
            lease.release()

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._directory is not None:
            self._directory.cleanup()


async def transfer(path, key, binding, ticket, sequence, values):
    """Authenticate the source before sending the bounded caller snapshot."""
    _credentials(values)
    reader, writer = await asyncio.open_unix_connection(path, limit=_LIMIT)
    try:
        _check_peer(writer)
        client_nonce = secrets.token_hex(32)
        base = (binding, ticket, sequence, client_nonce)
        writer.write(_encode(dict(version=1, binding=binding, ticket=ticket, sequence=sequence,
                                  nonce=client_nonce, proof=_mac(key, 'client-hello', *base))))
        await writer.drain()
        challenge = await _read(reader)
        if (set(challenge) != {'version', 'nonce', 'proof'} or type(challenge['version']) is not int
                or challenge['version'] != 1 or not _nonce(challenge['nonce']) or type(challenge['proof']) is not str
                or not hmac.compare_digest(challenge['proof'], _mac(key, 'server-challenge', *base, challenge['nonce']))):
            raise ContractError('Authority server authentication failed')
        identity = (*base, challenge['nonce'])
        digest = hashlib.sha256(_encode(values)).hexdigest()
        writer.write(_encode(dict(version=1, secrets=values, proof=_mac(key, 'client-transfer', *identity, digest))))
        await writer.drain()
        response = await _read(reader)
        if (set(response) != {'version', 'proof'} or type(response['version']) is not int
                or response['version'] != 1 or type(response['proof']) is not str
                or not hmac.compare_digest(response['proof'], _mac(key, 'server-ack', *identity, digest))):
            raise ContractError('Authority acknowledgement rejected')
        writer.write(_encode(dict(version=1, proof=_mac(key, 'client-confirm', *identity, digest))))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def leased_snapshot(path, key, binding):
    """Authenticate an enrolled worker before borrowing operation credentials."""
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    reader, writer = await asyncio.open_unix_connection(path, limit=_LIMIT)
    identity = digest = None
    enrolled = False
    try:
        async with asyncio.timeout(5):
            _check_peer(writer)
            client_nonce = secrets.token_hex(32)
            writer.write(_encode(dict(version=1, purpose='graph-capture', binding=binding,
                nonce=client_nonce, proof=_mac(key, 'graph-client-hello', binding, client_nonce))))
            await writer.drain()
            challenge = await _read(reader)
            if (set(challenge) != {'version', 'nonce', 'proof'}
                    or type(challenge['version']) is not int or challenge['version'] != 1
                    or not _nonce(challenge['nonce']) or type(challenge['proof']) is not str):
                raise ContractError('Invalid graph authority challenge')
            identity = (binding, client_nonce, challenge['nonce'])
            if not hmac.compare_digest(challenge['proof'],
                    _mac(key, 'graph-server-challenge', *identity)):
                raise ContractError('Graph authority authentication failed')
            writer.write(_encode(dict(version=1,
                proof=_mac(key, 'graph-client-confirm', *identity))))
            await writer.drain()
            response = await _read(reader)
            if (set(response) != {'version', 'secrets', 'proof'}
                    or type(response['version']) is not int or response['version'] != 1
                    or type(response['proof']) is not str):
                raise ContractError('Invalid graph authority snapshot')
            values = _credentials(response['secrets'])
            digest = hashlib.sha256(_encode(values)).hexdigest()
            if not hmac.compare_digest(response['proof'],
                    _mac(key, 'graph-server-snapshot', *identity, digest)):
                raise ContractError('Graph authority snapshot rejected')
            snapshot = EmissionBoundary(ContentPolicy(True), secrets=values, preserve_json_text=True)
            writer.write(_encode(dict(version=1,
                proof=_mac(key, 'graph-client-receipt', *identity, digest))))
            await writer.drain()
            ready = await _read(reader)
            if (set(ready) != {'version', 'proof'} or type(ready['version']) is not int
                    or ready['version'] != 1 or type(ready['proof']) is not str
                    or not hmac.compare_digest(ready['proof'],
                        _mac(key, 'graph-server-ready', *identity, digest))):
                raise ContractError('Graph authority enrollment rejected')
            enrolled = True
        yield snapshot
    finally:
        try:
            if enrolled:
                async with asyncio.timeout(5):
                    writer.write(_encode(dict(version=1,
                        proof=_mac(key, 'graph-client-release', *identity, digest))))
                    await writer.drain()
                    released = await _read(reader)
                    if (set(released) != {'version', 'proof'}
                            or type(released['version']) is not int or released['version'] != 1
                            or type(released['proof']) is not str
                            or not hmac.compare_digest(released['proof'],
                                _mac(key, 'graph-server-released', *identity, digest))):
                        raise ContractError('Graph authority release unconfirmed')
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
