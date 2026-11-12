"""Authenticated local transport for suite-owned prerequisite leases.

The controller registers handlers before accepting connections. Workers supply only
an account scope; handlers resolve configuration and provenance in the controller.
A disconnected worker keeps its lease until a trusted host acknowledges disposal.
"""
import ctypes
from contextvars import copy_context
import hmac
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
import secrets
import select
import socket
import struct
import sys
import tempfile
import threading
import time

from api_agent.runtime.short_temp import trusted_short_root
from api_agent.testing.suite_prerequisite_pool import PoolPending, SuitePrerequisitePool

_LIMIT = 32768
_DESCRIPTOR = 'suite-prerequisite-broker.json'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def _reject_constant(_value):
    raise ValueError('non-finite JSON number')


def _decode(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_float=_finite_float, parse_constant=_reject_constant)


def _duration(value, *, allow_zero=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0 and (allow_zero or value != 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('invalid duration')


def _validate_descriptor(descriptor):
    if (type(descriptor) is not dict or set(descriptor) != {'version', 'path', 'token'}
            or type(descriptor['version']) is not int or descriptor['version'] != 1
            or type(descriptor['path']) is not str or type(descriptor['token']) is not str
            or not re.fullmatch('[0-9a-f]{64}', descriptor['token'])):
        raise ValueError('invalid broker descriptor')


def _scoped_key(handler, digest):
    SuitePrerequisitePool._check_key(digest)
    return hashlib.sha256((handler + ':' + digest).encode()).hexdigest()


def _encode(value):
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(raw) > _LIMIT:
        raise ValueError('message too large')
    return raw


def _read(sock, *, deadline=None):
    data = bytearray()
    while len(data) < _LIMIT:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('message deadline exceeded')
            sock.settimeout(remaining)
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError()
        if chunk == b'\n':
            value = _decode(data)
            if type(value) is not dict:
                raise ValueError('invalid message')
            return value
        data.extend(chunk)
    raise ValueError('message too large')


def _peer(sock):
    if hasattr(socket, 'SO_PEERCRED'):
        _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    elif sys.platform == 'darwin':
        uid_value, gid_value = ctypes.c_uint(), ctypes.c_uint()
        func = ctypes.CDLL(None, use_errno=True).getpeereid
        func.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))
        func.restype = ctypes.c_int
        if func(sock.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)):
            raise OSError('peer identity unavailable')
        uid = uid_value.value
    else:
        raise OSError('peer identity unavailable')
    if uid != os.geteuid():
        raise PermissionError('peer rejected')


def _gone(sock):
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable and not sock.recv(1, socket.MSG_PEEK))
    except OSError:
        return True


class SuitePrerequisiteBroker:
    """Own the pool and a private socket for one pytest controller."""
    def __init__(self, context_directory, handlers, *, owner_budget, event=None, on_failure=None):
        _duration(owner_budget)
        self._context_directory = Path(context_directory)
        self._handlers = dict(handlers)
        self._owner_budget = owner_budget
        self._pool = SuitePrerequisitePool(owner_budget=owner_budget)
        self._event = event
        self._on_failure = on_failure
        self._reported_failures = []
        self._token = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._preparing = 0
        self._leases = {}
        self._workers_disposed = False
        self._known = set()
        self._slots = threading.BoundedSemaphore(128)
        self._server = self._directory = None
        self._stopping = threading.Event()

    def _emit(self, phase, start, **counts):
        if self._event:
            try:
                self._event({'phase': phase, 'seconds': max(0, time.monotonic() - start), **counts})
            except Exception:
                pass  # Diagnostics cannot affect ownership.

    def _report_failure(self, error):
        if self._on_failure is None:
            return
        with self._lock:
            if any(previous is error for previous in self._reported_failures):
                return
            # Bound retained exceptions and diagnostic volume per controller.
            if len(self._reported_failures) >= 128:
                return
            self._reported_failures.append(error)
        try:
            self._on_failure(error)
        except BaseException:
            pass  # Diagnostics cannot affect ownership or the worker response.

    def start(self):
        if self._server is not None:
            raise RuntimeError('broker already started')
        info = self._context_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('context directory must be private and owned')
        root = trusted_short_root(('/var/tmp', '/tmp'), suffix='sp-xxxxxxxx/channel', limit=100, uid=os.geteuid())
        self._directory = tempfile.TemporaryDirectory(prefix='sp-', dir=root)
        path = str(Path(self._directory.name) / 'channel')
        descriptor_created = False
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server = server
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(128)
            server.settimeout(0.2)
            descriptor = {'version': 1, 'path': path, 'token': self._token}
            fd = os.open(self._context_directory / _DESCRIPTOR, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            descriptor_created = True
            with os.fdopen(fd, 'wb') as stream:
                stream.write(_encode(descriptor))
            context = copy_context()
            threading.Thread(target=context.run, args=(self._accept,), daemon=True).start()
            return descriptor
        except BaseException:
            if descriptor_created:
                (self._context_directory / _DESCRIPTOR).unlink(missing_ok=True)
            if self._server is not None:
                self._server.close()
                self._server = None
            self._directory.cleanup()
            self._directory = None
            raise

    def _accept(self):
        while not self._stopping.is_set():
            try:
                sock, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                sock.close()
                continue
            try:
                context = copy_context()
                threading.Thread(target=context.run, args=(self._serve, sock), daemon=True).start()
            except Exception:
                sock.close()
                self._slots.release()

    def _serve(self, sock):
        started = time.monotonic()
        acquiring = False
        try:
            sock.settimeout(5)
            _peer(sock)
            request = _read(sock, deadline=time.monotonic() + 5)
            if (set(request) != {'version', 'token', 'handler', 'scope'} or type(request['version']) is not int or request['version'] != 1
                    or type(request['token']) is not str or not hmac.compare_digest(request['token'], self._token)
                    or type(request['handler']) is not str or request['handler'] not in self._handlers
                    or type(request['scope']) is not dict or set(request['scope']) != {'account_scope'}
                    or type(request['scope']['account_scope']) is not str
                    or not re.fullmatch('[0-9a-f]{64}', request['scope']['account_scope'])):
                raise ValueError('invalid request')
            acquiring = True
            with self._condition:
                if self._stopping.is_set():
                    raise RuntimeError('broker is draining')
                self._preparing += 1
            prepare_deadline = time.monotonic() + self._owner_budget
            try:
                digest, factory = self._handlers[request['handler']].prepare(
                    request['scope'], deadline=prepare_deadline)
                if time.monotonic() > prepare_deadline:
                    raise TimeoutError('prerequisite resolution exceeded its budget')
            finally:
                with self._condition:
                    self._preparing -= 1
                    self._condition.notify_all()
            digest = _scoped_key(request['handler'], digest)
            with self._lock:
                reused = digest in self._known
                self._known.add(digest)
            self._emit('prepare', started, reuse=int(reused), miss=int(not reused))
            wait = time.monotonic()
            borrower = self._pool.borrow(digest, factory, cancelled=lambda: _gone(sock))
            acquiring = False
            lease_id = secrets.token_hex(32)
            try:
                if type(borrower.value) is not dict:
                    raise ValueError('capability must be an object')
                response = _encode({'status': 'ready', 'lease_id': lease_id, 'value': borrower.value})
            except Exception:
                borrower.release()
                raise
            with self._lock:
                disposed = self._workers_disposed
                if not disposed:
                    self._leases[lease_id] = borrower
            if disposed:
                borrower.release()
                raise RuntimeError('workers already disposed')
            self._emit('readiness', wait, count=1)
            # Once delivery starts, loss of the connection is not disposal evidence.
            sock.sendall(response)
            sock.settimeout(None)
            select.select([sock], [], [])
            release = _read(sock, deadline=time.monotonic() + 5)
            if release != {'release': lease_id}:
                raise ValueError('invalid release')
            self.acknowledge_disposal(lease_id)
            sock.sendall(_encode({'status': 'released'}))
        except Exception as error:
            if acquiring:
                self._report_failure(error)
            self._emit('error', started, count=1)
            try:
                sock.sendall(_encode({'status': 'error'}))
            except OSError:
                pass
        finally:
            sock.close()
            self._slots.release()

    def invalidate(self, handler, digest):
        """Trusted host invalidation; cleanup must settle before reacquisition."""
        if type(handler) is not str or handler not in self._handlers:
            raise ValueError('unknown handler')
        key = _scoped_key(handler, digest)
        self._pool.invalidate(key)
        with self._lock:
            self._known.discard(key)

    def pending_lease_ids(self):
        """Return opaque IDs for host-owned disposal correlation."""
        with self._lock:
            return tuple(self._leases)

    def acknowledge_all_disposed(self):
        """Latch trusted proof that all workers have been disposed.

        The outer host must establish native worker disposal before invoking this.
        Socket loss alone is never evidence of disposal.
        """
        with self._lock:
            self._stopping.set()
            self._pool.stop_admission()
            self._workers_disposed = True
            borrowers = tuple(self._leases.values())
            self._leases.clear()
        for borrower in borrowers:
            borrower.release()

    def acknowledge_disposal(self, lease_id):
        """Release only after the trusted outer host confirms worker disposal."""
        with self._lock:
            borrower = self._leases.pop(lease_id, None)
        if borrower is not None:
            borrower.release()

    def close(self, *, timeout):
        started = time.monotonic()
        _duration(timeout, allow_zero=True)
        deadline = started + timeout
        self._stopping.set()
        if self._server is not None:
            self._server.close()
        try:
            self._pool.close(timeout=max(0, deadline - time.monotonic()))
            with self._condition:
                while self._preparing:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PoolPending('Prerequisite resolution remains pending')
                    self._condition.wait(remaining)
        finally:
            with self._lock:
                pending = len(self._leases)
            self._emit('cleanup', started, pending=pending)
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
            (self._context_directory / _DESCRIPTOR).unlink(missing_ok=True)


def read_descriptor(context_directory):
    """Read the controller's protected descriptor without following symlinks."""
    directory = Path(context_directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('context directory must be private and owned')
    fd = os.open(directory / _DESCRIPTOR, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('invalid broker descriptor permissions')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError('broker descriptor too large')
        descriptor = _decode(raw)
        _validate_descriptor(descriptor)
        return descriptor
    finally:
        os.close(fd)


class PrerequisiteLease:
    def __init__(self, sock, lease_id, value):
        self._socket, self.lease_id, self.value = sock, lease_id, value

    def release(self):
        if self._socket is None:
            return
        sock, self._socket = self._socket, None
        try:
            sock.settimeout(5)
            sock.sendall(_encode({'release': self.lease_id}))
            if _read(sock, deadline=time.monotonic() + 5) != {'status': 'released'}:
                raise RuntimeError('lease release not acknowledged')
        finally:
            sock.close()

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        self.release()


def borrow_prerequisite(descriptor, handler, request, *, timeout):
    """Borrow an admitted JSON capability over a held connection."""
    _validate_descriptor(descriptor)
    _duration(timeout)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + timeout
        sock.settimeout(timeout)
        sock.connect(descriptor['path'])
        _peer(sock)
        sock.sendall(_encode({'version': 1, 'token': descriptor['token'], 'handler': handler, 'scope': request}))
        result = _read(sock, deadline=deadline)
        if (set(result) != {'status', 'lease_id', 'value'} or result['status'] != 'ready'
                or type(result['lease_id']) is not str or type(result['value']) is not dict):
            raise RuntimeError('prerequisite acquisition failed')
        return PrerequisiteLease(sock, result['lease_id'], result['value'])
    except BaseException:
        sock.close()
        raise
