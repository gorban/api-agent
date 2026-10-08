"""Private local credential persistence and serialized rotation."""
import asyncio
from contextlib import contextmanager, asynccontextmanager
from dataclasses import asdict
import fcntl
import hashlib
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
    def __init__(self, store, profile_id, refresh, clock=time.time, skew=60):
        self.store, self.profile_id, self.refresh = store, profile_id, refresh
        self.clock, self.skew = clock, _number(skew)
        if skew < 0:
            _fail()
        self._task = None
        self._closed = False

    async def __call__(self):
        if self._closed:
            raise AuthError("session_closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._current())
        return await asyncio.shield(self._task)

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
            if value.host_id != self.store.host_id():
                _fail()
            now = _number(self.clock())
            threshold = _number(value.expires_at - self.skew)
            if now < value.expires_at and (now < threshold or now < value.earliest_refresh_at):
                return value.access_token
            updated = _validate(await self.refresh(value, now))
            if (updated.subject, updated.client_id, updated.host_id) != (value.subject, value.client_id, value.host_id) or frozenset(updated.scopes) != frozenset(value.scopes) or updated.expires_at <= _number(self.clock()):
                _fail()
            self.store.save(updated)
            return updated.access_token
        finally:
            guard.__exit__(None, None, None)

    def aclose(self):
        if not self._closed and self._task is not None:
            self._task.cancel()
        self._closed = True
        return self._dispose()

    async def _dispose(self):
        if self._task is not None:
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
