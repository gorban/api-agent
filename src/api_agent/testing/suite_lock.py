"""One local suite owns execution/accounting; idle launchers remain independent."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import asyncio
import errno
import inspect
import os
import stat
import sys
from threading import get_ident, RLock

from api_agent.telemetry.history import cache_path

_CURRENT = ContextVar('api_agent_suite_lease', default=None)
_DELEGATED_THREAD = ContextVar('api_agent_suite_worker', default=None)


class SuiteBusyError(ValueError):
    """Another operation owns the machine/user's test-suite write lease."""


def suite_lock_path():
    # A stable sidecar survives cache rebuilds. Never unlink a locking inode.
    return cache_path().parent / 'test-suite.lock'


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass
class _Lease:
    pid: int
    thread: int
    task: object
    active: bool = True
    fd: int | None = None
    owners: set = field(default_factory=set, repr=False)
    guard: object = field(default_factory=RLock, repr=False)

    def retain(self, identifier):
        with self.guard:
            if identifier in self.owners:
                return False
            self.owners.add(identifier)
            return True

    def release(self, identifier):
        with self.guard:
            self.owners.discard(identifier)
            self._close_if_finished()

    def finish(self):
        with self.guard:
            self.active = False
            self._close_if_finished()

    def _close_if_finished(self):
        if not self.active and not self.owners and self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def owns_context(self):
        current = _task()
        delegate = _DELEGATED_THREAD.get()
        return (self.active and self.pid == os.getpid()
                and (current is self.task if current is not None else
                     get_ident() == self.thread or
                     (delegate is not None and delegate[0] is self and delegate[1] == get_ident())))


class CleanupLeaseOwner:
    """Owned cleanup calls its acknowledgement only after disposal completes."""
    def _when_closed(self, callback):
        with self._lock:
            if self.closed:
                callback()
            else:
                callbacks = getattr(self, '_cleanup_callbacks', [])
                callbacks.append(callback)
                self._cleanup_callbacks = callbacks

    def _acknowledge_closed(self):
        callbacks = getattr(self, '_cleanup_callbacks', ())
        self._cleanup_callbacks = []
        for callback in callbacks:
            callback()


def retain_suite_cleanup(owner):
    """Transfer the current OS lease to an acknowledged cleanup owner."""
    lease = _CURRENT.get()
    subscribe = getattr(owner, '_when_closed', None)
    if lease is None or not lease.owns_context() or not callable(subscribe):
        return
    identifier = id(owner)
    if not lease.retain(identifier):
        return
    try:
        subscribe(lambda: lease.release(identifier))
    except BaseException:
        lease.release(identifier)
        raise


def _lock(fd):
    if sys.platform == 'win32':
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def suite_lock():
    """Fail before work starts; hold through pending cleanup or process death.

    Reuse a lease only within its owner execution context, including the async
    owner's worker thread. Independent threads/tasks must acquire their own lock.
    Retained cleanup releases the descriptor only on acknowledgement; process
    death also releases the non-inherited OS lock. A remaining file
    is harmless and is never interpreted as an active/stale PID marker.
    """
    lease = _CURRENT.get()
    if lease is not None and lease.owns_context():
        yield
        return
    path = suite_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    acquired = None
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Test-suite lock must be a regular file.')
        os.set_inheritable(fd, False)
        try:
            _lock(fd)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise SuiteBusyError('Another test suite is running. Wait for it to finish or stop it before starting this suite.') from None
        lease = _Lease(os.getpid(), get_ident(), _task(), fd=fd)
        acquired = lease
        token = _CURRENT.set(lease)
        try:
            yield
        finally:
            _CURRENT.reset(token)
    finally:
        if acquired is not None:
            acquired.finish()
        else:
            os.close(fd)


def suite_locked(function):
    """Share the same suite lifetime guard across synchronous/async adapters."""
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with suite_lock():
                return await function(*args, **kwargs)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        with suite_lock():
            return function(*args, **kwargs)
    return synchronous


def suite_worker(function, *args, **kwargs):
    """Authorize an owned async-to-thread bridge before scheduling its work."""
    lease = _CURRENT.get()
    if lease is not None and not lease.owns_context():
        raise SuiteBusyError('Only the active test-suite owner can delegate its worker.')
    def invoke():
        if lease is not None and (not lease.active or lease.pid != os.getpid()):
            raise SuiteBusyError('The test-suite owner has already stopped.')
        claim = _CURRENT.set(lease)
        token = _DELEGATED_THREAD.set((lease, get_ident()))
        try:
            return function(*args, **kwargs)
        finally:
            _DELEGATED_THREAD.reset(token)
            _CURRENT.reset(claim)
    return invoke
