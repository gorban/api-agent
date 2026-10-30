"""Private local credential persistence and serialized rotation."""
import asyncio
from contextlib import contextmanager, asynccontextmanager
from dataclasses import asdict
import fcntl
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from .contracts import AuthError, Connection

_LIMIT = 65536
_KEYS = frozenset(Connection.__dataclass_fields__)


def _fail():
    raise AuthError("invalid_connection") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _number(number):
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        _fail()
    try:
        finite = math.isfinite(number)
    except OverflowError:
        _fail()
    if not finite:
        _fail()
    return number


def _validate(value):
    if not isinstance(value, Connection):
        _fail()
    for key in ("subject", "client_id", "host_id", "access_token", "refresh_token", "id_token"):
        if not isinstance(getattr(value, key), str) or not getattr(value, key):
            _fail()
    if not isinstance(value.email, str) or not isinstance(value.scopes, tuple) or not value.scopes:
        _fail()
    if any(not isinstance(item, str) or not item for item in value.scopes):
        _fail()
    for number in (value.expires_at, value.earliest_refresh_at):
        _number(number)
    return value


class ConnectionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _directory(self):
        fd = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail()
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            _fail()

    def _path(self, profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[0-9a-f]{64}", profile_id):
            _fail()
        return self.root / (profile_id + ".json")

    @staticmethod
    def profile_id(connection):
        _validate(connection)
        return hashlib.sha256(json.dumps([connection.client_id, connection.subject], separators=(",", ":")).encode()).hexdigest()

    def _open(self, path, flags, directory=None):
        owned = directory is None
        if owned:
            directory = self._directory()
        fd = None
        try:
            fd = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                _fail()
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            _fail()
        finally:
            if owned:
                os.close(directory)

    def _read(self, path):
        with os.fdopen(self._open(path, os.O_RDONLY), "rb") as stream:
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            _fail()
        try:
            return json.loads(raw, object_pairs_hook=_unique)
        except (ValueError, UnicodeError):
            _fail()

    def _write(self, path, data):
        encoded = json.dumps(data, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > _LIMIT:
            _fail()
        directory = self._directory()
        temp = Path("." + uuid.uuid4().hex)
        try:
            try:
                os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                os.close(self._open(path, os.O_RDONLY, directory))
            with os.fdopen(self._open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory), "w") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp.name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            _fail()
        finally:
            pending = sys.exception()
            try:
                try:
                    os.unlink(temp.name, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    if pending is None:
                        _fail()
            finally:
                os.close(directory)

    @contextmanager
    def lock(self, profile_id):
        path = self._path(profile_id).with_suffix(".lock")
        fd = self._open(path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def host_id(self):
        key = "0" * 64
        with self.lock(key):
            path = self.root / "host.json"
            if path.exists() or path.is_symlink():
                data = self._read(path)
                if not isinstance(data, dict) or set(data) != {"version", "host_id"} or type(data["version"]) is not int or data["version"] != 1:
                    _fail()
                try:
                    parsed = uuid.UUID(data["host_id"])
                    if parsed.version != 4 or parsed.urn != data["host_id"]:
                        _fail()
                except (ValueError, TypeError, AttributeError):
                    _fail()
                return parsed.urn
            value = uuid.uuid4().urn
            self._write(path, {"version": 1, "host_id": value})
            return value

    @asynccontextmanager
    async def async_lock(self, profile_id):
        """Share the refresh lock without blocking the callback event loop."""
        fd = self._open(self._path(profile_id).with_suffix('.lock'), os.O_RDWR | os.O_CREAT)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.01)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def save_serialized(self, connection):
        key = self.profile_id(connection)
        async with self.async_lock(key):
            path = self._path(key)
            if (path.exists() or path.is_symlink()) and self.load(key).host_id != connection.host_id:
                _fail()
            return self.save(connection)

    def save(self, connection):
        key = self.profile_id(connection)
        data = asdict(connection)
        data["version"] = 1
        self._write(self._path(key), data)
        return key

    def load(self, profile_id):
        data = self._read(self._path(profile_id))
        if not isinstance(data, dict) or set(data) != _KEYS | {"version"} or type(data["version"]) is not int or data.pop("version") != 1 or not isinstance(data["scopes"], list):
            _fail()
        data["scopes"] = tuple(data["scopes"])
        value = _validate(Connection(**data))
        if self.profile_id(value) != profile_id:
            _fail()
        return value

    def list(self):
        directory = self._directory()
        try:
            names = sorted(os.listdir(directory))
        finally:
            os.close(directory)
        return tuple(self.load(Path(name).stem) for name in names if name.endswith(".json") and name != "host.json")


class SubscriptionSession:
    def __init__(self, store, profile_id, refresh, clock=time.time, skew=60, register_connection=None, on_saved=None, has_pending=None, recover_saved=None):
        self.store, self.profile_id, self.refresh = store, profile_id, refresh
        self.register_connection = register_connection
        self.on_saved = on_saved
        self.has_pending = has_pending
        self.recover_saved = recover_saved
        self.clock, self.skew = clock, _number(skew)
        if skew < 0:
            _fail()
        self._task = None
        self._closed = False
        self._failure = None
        self._retry = asyncio.Event()

    async def __call__(self):
        if self._closed:
            raise AuthError("session_closed")
        if self._task is None or self._task.done():
            self._failure = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._current())
        elif self._failure is not None and self._failure.done():
            self._failure = asyncio.get_running_loop().create_future()
            self._retry.set()
        failure = self._failure
        done, _ = await asyncio.wait((self._task, failure), return_when=asyncio.FIRST_COMPLETED)
        if self._task in done:
            return self._task.result()
        raise failure.result()

    async def _current(self):
        guard = self.store.lock(self.profile_id)
        acquisition = asyncio.create_task(asyncio.to_thread(guard.__enter__))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            await acquisition
            guard.__exit__(None, None, None)
            raise
        try:
            value = self.store.load(self.profile_id)
            if self.register_connection is not None:
                registered = self.register_connection(value)
                if inspect.isawaitable(registered):
                    await registered
            if value.host_id != self.store.host_id():
                _fail()
            now = _number(self.clock())
            threshold = _number(value.expires_at - self.skew)
            if (not (self.has_pending is not None and self.has_pending())
                    and now < value.expires_at and (now < threshold or now < value.earliest_refresh_at)):
                return value.access_token
            current = value
            staged = None
            async def rotate():
                nonlocal current, staged
                index = 0
                while True:
                    if staged is None:
                        previous = current
                        updated = _validate(await self.refresh(previous, _number(self.clock())))
                        if self.register_connection is not None:
                            registered = self.register_connection(updated)
                            if inspect.isawaitable(registered):
                                await registered
                        if ((updated.subject, updated.client_id, updated.host_id) != (previous.subject, previous.client_id, previous.host_id)
                                or frozenset(updated.scopes) != frozenset(previous.scopes)):
                            _fail()
                        expired = updated.expires_at <= _number(self.clock())
                        pending = self.has_pending is not None and self.has_pending()
                        if expired and not pending:
                            _fail()
                        staged = dict(previous=previous, updated=updated, uncertain=True, ack_failed=False)
                        try:
                            self.store.save(updated)
                        except BaseException:
                            # Keep the verified rotation and its original owner.
                            # The next caller reconciles before any new exchange.
                            raise
                        staged['uncertain'] = False
                    else:
                        previous, updated = staged['previous'], staged['updated']
                        if staged['uncertain']:
                            stored = self.store.load(self.profile_id)
                            if stored != previous and stored != updated:
                                _fail()
                            # Reload proves identity, not durability. Require a
                            # complete successful save before acknowledging it.
                            self.store.save(updated)
                            staged['uncertain'] = False
                        expired = updated.expires_at <= _number(self.clock())
                    current = updated
                    if staged['ack_failed']:
                        if self.store.load(self.profile_id) != updated:
                            _fail()
                        if self.recover_saved is None:
                            _fail()
                    acknowledgement_failed = False
                    try:
                        if staged['ack_failed']:
                            self.recover_saved(previous, updated)
                        elif self.on_saved is not None:
                            self.on_saved(updated)
                    except asyncio.CancelledError:
                        staged['ack_failed'] = True
                        raise
                    except Exception:
                        staged['ack_failed'] = True
                        acknowledgement_failed = True
                    if acknowledgement_failed:
                        # Do not retain arbitrary callback diagnostics in the
                        # exception chain, even after partial acknowledgement.
                        raise AuthError('refresh_admission_failed') from None
                    staged = None
                    if not expired:
                        return updated.access_token
                    if index:
                        _fail()
                    index += 1
            interrupted = False
            while True:
                try:
                    result = await rotate()
                    if interrupted:
                        raise asyncio.CancelledError
                    return result
                except asyncio.CancelledError:
                    if self.has_pending is None or not self.has_pending():
                        raise
                    # Keep ownership of an issued rotation through shutdown.
                    interrupted = True
                    self._retry.set()
                except Exception as error:
                    if self.has_pending is None or not self.has_pending():
                        raise
                    if self._failure is not None and not self._failure.done():
                        # Never expose arbitrary registration or response error text.
                        self._failure.set_result(TimeoutError() if isinstance(error, TimeoutError)
                                                 else AuthError('refresh_admission_failed'))
                # Do not release the store lock while the only copy of the
                # rotated credential is pending. Each retry is caller driven.
                await self._retry.wait()
                self._retry.clear()
        finally:
            guard.__exit__(None, None, None)

    def aclose(self):
        if (not self._closed and self._task is not None
                and not (self.has_pending and self.has_pending()
                         and self._failure is not None and self._failure.done())):
            self._task.cancel()
        self._closed = True
        return self._dispose()

    async def _dispose(self):
        if self._task is not None:
            if self.has_pending is not None and self.has_pending() and not self._task.done():
                from api_agent.providers.control import CleanupPendingError
                if self._failure is None or self._failure.done():
                    self._failure = asyncio.get_running_loop().create_future()
                self._retry.set()
                done, _ = await asyncio.wait((self._task, self._failure), return_when=asyncio.FIRST_COMPLETED)
                if self._task not in done:
                    raise CleanupPendingError((self,), self.aclose) from None
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
