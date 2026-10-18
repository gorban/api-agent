"""One local suite owns execution/accounting; idle launchers remain independent."""
from _thread import start_new_thread
from concurrent.futures import Future
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

    closing: object = field(default=None, repr=False)
    receipts: dict = field(default_factory=dict, repr=False)

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
            # Only this disposal task touches the numeric descriptor. Interrupting
            # its acknowledgement never resubmits close against a reused fd.
            if self.closing is None:
                ticket = Future()
                try:
                    self.closing = ticket
                    start_new_thread(_dispose_descriptor, (self.fd, ticket))
                except BaseException:
                    # A cancelled launch ticket prevents even a delayed thread
                    # from disposing the fd; a running ticket owns close once.
                    if ticket.cancel():
                        self.closing = None
                    raise
            self.closing.result()
            self.fd = None

    def owns_context(self):
        current = _task()
        delegate = _DELEGATED_THREAD.get()
        return (self.active and self.pid == os.getpid()
                and (current is self.task if current is not None else
                     get_ident() == self.thread or
                     (delegate is not None and delegate[0] is self and delegate[1] == get_ident())))


def _dispose_descriptor(fd, acknowledgement):
    # Native closerange ignores close errors and has no Python callback between
    # the syscall and completion. Signals interrupt the caller, not this thread.
    if acknowledgement.set_running_or_notify_cancel():
        os.closerange(fd, fd + 1)
        acknowledgement.set_result(None)


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
        # Called under the owner's lifecycle lock (which need not be reentrant).
        callbacks = getattr(self, '_cleanup_callbacks', [])
        while callbacks:
            callbacks[0]()
            del callbacks[0]


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


def _cleanup_receipts(error):
    """Enumerate direct and attached duties, once each, including cycles."""
    from api_agent.providers.control import CleanupPendingError
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, CleanupPendingError) and callable(error.retry):
            yield error
        error = getattr(error, 'cleanup_pending', None)


def retain_cleanup_receipt(error):
    """Retain every outer adapter duty before its suite context exits."""
    lease = _CURRENT.get()
    if lease is None or not lease.owns_context():
        return
    for pending in _cleanup_receipts(error):
        _bind_cleanup_receipt(pending, lease)


def _bind_cleanup_receipt(pending, lease):
    with lease.guard:
        identifier = id(pending)
        if identifier not in lease.receipts:
            lease.retain(identifier)
            state = _ReceiptRetention(pending, lease)
            lease.receipts[identifier] = state
            pending.retry = state.retry
        return lease.receipts[identifier]


class _ReceiptRetention:
    """One cleanup duty; successor duties must all acknowledge its replacement."""
    def __init__(self, pending, lease):
        self.pending, self.lease = pending, lease
        self.original_retry = pending.retry
        self.disposed = self.acknowledged = False
        self.successors = set()
        self.parents = []
        self.notifying = False

    def _acknowledge(self):
        with self.lease.guard:
            if not self.acknowledged:
                self.lease.release(id(self.pending))
                self.acknowledged = True
            if self.notifying:
                return
            self.notifying = True
            try:
                while self.parents:
                    self.parents[0]._successor_finished(self)
                    del self.parents[0]
            finally:
                self.notifying = False

    def _successor_finished(self, state):
        self.successors.discard(state)
        if not self.successors:
            self.disposed = True
            self._acknowledge()

    async def retry(self, *args, **kwargs):
        await self._retry(args, kwargs, set())

    async def _retry(self, args, kwargs, ancestors):
        # Ancestors have already completed their own disposal before visiting
        # successors. A cycle needs no second invocation of that same body.
        if self in ancestors:
            return
        # A stored parent can keep reporting the same replacement until its
        # child acknowledges. Advance that known duty before calling it again.
        if not self.disposed:
            for state in tuple(self.successors):
                await state._retry((), {}, ancestors | {self})
        if not self.disposed:
            try:
                await self.original_retry(*args, **kwargs)
            except BaseException as error:
                following = tuple(_bind_cleanup_receipt(pending, self.lease)
                                  for pending in _cleanup_receipts(error))
                # A direct self receipt still describes this original duty.
                # Distinct attachments always remain independent lease owners.
                if following and self not in following:
                    with self.lease.guard:
                        self.successors.update(following)
                        for state in following:
                            if self not in state.parents:
                                state.parents.append(self)
                            if state.acknowledged:
                                state._acknowledge()
                raise
            self.disposed = True
        # Stable receipts remain supported: settle observations left by earlier
        # failed attempts through their real retry, including distinct duties.
        for state in tuple(self.successors):
            await state._retry((), {}, ancestors | {self})
        self._acknowledge()


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
                try:
                    return await function(*args, **kwargs)
                except BaseException as error:
                    retain_cleanup_receipt(error)
                    raise
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        with suite_lock():
            try:
                return function(*args, **kwargs)
            except BaseException as error:
                retain_cleanup_receipt(error)
                raise
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
