"""Read suite-configured password-manager entries in the launcher for its tests.

Password-manager CLIs such as 1Password approve each terminal session separately.
Test processes run in sessions of their own, so each read there would ask again.
The launcher reads on their behalf in its own approved session, holds nothing
between requests, and serves only the accounts configured in UNATTENDED_AUTH.
"""
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
import time

from api_agent.auth.credentials import (CredentialCapabilityUnsupportedError, CredentialError,
                                        CredentialLoginRequiredError, CredentialSource)
from api_agent.runtime.short_temp import trusted_short_root
from api_agent.testing.suite_prerequisite_broker import _encode, _peer, _read

ENVIRONMENT = 'API_AGENT_CREDENTIAL_RELAY'
_FIELDS = ('manager', 'cli_path', 'vault', 'user')
_KINDS = frozenset({'username', 'password', 'totp'})
# Bounds one queued read; reads are serialized so at most one approval is pending.
_QUEUE_SECONDS = 60
_WARM_SECONDS = 60
_FAILURES = {'login_required': (CredentialLoginRequiredError, 'Password manager sign-in is required.'),
             'unsupported': (CredentialCapabilityUnsupportedError, 'Password manager capability is unsupported.')}


def _key(source):
    return tuple(getattr(source, name) for name in _FIELDS)


def configured_sources(config, registry):
    """Every account UNATTENDED_AUTH configures, or none when it configures no manager."""
    from api_agent.auth.profiles import normalize_auth, validate_role_entries
    try:
        canonical = normalize_auth(config.get('UNATTENDED_AUTH', {}), registry=registry)
        manager = canonical['password_manager']
        if not manager['manager']:
            return ()
        sources = []
        for role, entries in canonical['roles'].items():
            validate_role_entries(role, entries)
            sources.extend(CredentialSource.from_mapping({**manager, **entry}, registry=registry)
                           for entry in entries)
    except (ValueError, CredentialError):
        # Invalid configuration fails where a test binds its role, as before.
        return ()
    return tuple(dict.fromkeys(sources))


class CredentialRelay:
    def __init__(self, sources, *, retrieve=None):
        from api_agent.auth.credentials import _retrieve_locally
        self._sources = {_key(source): source for source in sources}
        self._retrieve = _retrieve_locally if retrieve is None else retrieve
        self._token = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server = self._directory = None
        self.descriptor = None

    def start(self):
        root = trusted_short_root(('/var/tmp', '/tmp'), suffix='cr-xxxxxxxx/channel', limit=100, uid=os.geteuid())
        self._directory = tempfile.TemporaryDirectory(prefix='cr-', dir=root)
        path = str(Path(self._directory.name) / 'channel')
        try:
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(path)
            os.chmod(path, 0o600)
            self._server.listen(16)
            self._server.settimeout(0.2)
            threading.Thread(target=self._accept, daemon=True).start()
        except BaseException:
            self.close()
            raise
        self.descriptor = json.dumps({'version': 1, 'path': path, 'token': self._token})
        return self.descriptor

    def warm(self):
        """Ask for the one approval now, while the person starting the suite is present."""
        source = next(iter(self._sources.values()), None)
        if source is None:
            return
        with self._lock:
            try:
                self._retrieve(source, 'username', _WARM_SECONDS)
            except CredentialError:
                pass  # A test's own read reports the failure in its context.

    def _accept(self):
        while not self._stopping.is_set():
            try:
                sock, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _admit(self, sock):
        _peer(sock)
        request = _read(sock, deadline=time.monotonic() + 5)
        source, timeout = request.get('source'), request.get('timeout')
        if (set(request) != {'version', 'token', 'source', 'kind', 'timeout'} or request['version'] != 1
                or type(request['token']) is not str or not hmac.compare_digest(request['token'], self._token)
                or request['kind'] not in _KINDS or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0
                or type(source) is not dict or set(source) != set(_FIELDS)
                or any(type(value) is not str for value in source.values())):
            raise ValueError('invalid credential relay request')
        selected = self._sources.get(tuple(source[name] for name in _FIELDS))
        if selected is None:
            raise PermissionError('credential relay serves only configured accounts')
        return selected, request['kind'], min(timeout, _QUEUE_SECONDS)

    def _serve(self, sock):
        with sock:
            try:
                sock.settimeout(5)
                source, kind, timeout = self._admit(sock)
            except Exception:
                reply = {'status': 'refused'}
            else:
                with self._lock:
                    try:
                        reply = {'status': 'ok', 'value': self._retrieve(source, kind, timeout)}
                    except CredentialLoginRequiredError:
                        reply = {'status': 'login_required'}
                    except CredentialCapabilityUnsupportedError:
                        reply = {'status': 'unsupported'}
                    except CredentialError:
                        reply = {'status': 'error'}
            try:
                sock.settimeout(5)
                sock.sendall(_encode(reply))
            except (OSError, ValueError):
                pass

    def close(self):
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def relayed(source, kind, timeout):
    """Read through the launcher's relay; None when this process has no reachable relay."""
    raw = os.environ.get(ENVIRONMENT)
    if not raw:
        return None
    try:
        descriptor = json.loads(raw)
        if (type(descriptor) is not dict or set(descriptor) != {'version', 'path', 'token'}
                or descriptor['version'] != 1 or type(descriptor['path']) is not str
                or type(descriptor['token']) is not str or not re.fullmatch('[0-9a-f]{64}', descriptor['token'])):
            raise ValueError
    except ValueError:
        raise CredentialError('Invalid credential relay descriptor.') from None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        try:
            sock.connect(descriptor['path'])
        except OSError:
            return None  # The suite that owned the relay has ended; read directly.
        try:
            _peer(sock)
            sock.sendall(_encode({'version': 1, 'token': descriptor['token'], 'kind': kind, 'timeout': timeout,
                                  'source': {name: getattr(source, name) for name in _FIELDS}}))
            reply = _read(sock, deadline=time.monotonic() + timeout + _QUEUE_SECONDS)
        except (OSError, EOFError, ValueError):
            raise CredentialError('Credential relay failed.') from None
    if reply.get('status') == 'ok' and set(reply) == {'status', 'value'} and type(reply['value']) is str:
        return reply['value']
    error, message = _FAILURES.get(reply.get('status'), (CredentialError, 'Credential relay refused the request.'))
    raise error(message)
