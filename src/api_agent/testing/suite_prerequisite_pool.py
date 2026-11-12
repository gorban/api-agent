"""Parent-owned coordination for suite-scoped prerequisite owners.

This coordinator lives in the suite owner process. A transport must explicitly
associate each borrower with its worker and release that lease on confirmed
worker disposal; a lost connection alone does not establish disposal. Owners
are inert until ``acquire`` and retain the canonical acquisition and recovery
machinery. Keys are private, caller-derived digests of admitted provenance.
"""
import contextvars
import math
import re
import threading
import time


class PoolCancelled(RuntimeError):
    """A borrower stopped waiting; the suite owner continues preparation."""


class PoolClosed(RuntimeError):
    """The suite no longer admits borrowers."""


class PoolPending(RuntimeError):
    """Suite resources remain active or their disposal was not acknowledged."""


class _Entry:
    def __init__(self, deadline):
        self.deadline = deadline
        self.owner = None
        self.value = None
        self.error = None
        self.starting = True
        self.borrowers = 0
        self.disposing = False
        self.cleanup_required = False
        self.cleanup_error = None


class Borrower:
    """An idempotent use lease; releasing it never disposes the suite owner."""
    def __init__(self, pool, entry):
        self._pool = pool
        self._entry = entry
        self.value = entry.value
        self._released = False

    def release(self):
        with self._pool._condition:
            if not self._released:
                self._released = True
                self._entry.borrowers -= 1
                self._pool._condition.notify_all()

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        self.release()


class SuitePrerequisitePool:
    """Coalesce startup and retain owners until explicit suite drain.

    ``owner_factory`` must only construct an inert owner; it must not allocate
    resources. Its ``acquire(deadline=...)`` must honor the monotonic deadline and
    return only after readiness and identity verification. ``release()`` must
    retain its own recovery state on failure and be safe to retry.
    """
    def __init__(self, *, owner_budget):
        if (isinstance(owner_budget, bool) or not isinstance(owner_budget, (int, float))
                or not math.isfinite(owner_budget) or owner_budget <= 0):
            raise ValueError('owner budget must be positive')
        self._budget = owner_budget
        self._condition = threading.Condition()
        self._entries = {}
        self._closed = False

    @staticmethod
    def _check_key(key):
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('pool key must be an admitted provenance digest')

    def _start(self, entry, factory, deadline):
        try:
            owner = factory()
            with self._condition:
                entry.owner = owner
            value = owner.acquire(deadline=deadline)
            if time.monotonic() > deadline:
                raise TimeoutError('Suite prerequisite preparation exceeded its budget')
            with self._condition:
                entry.value = value
        except BaseException as error:
            with self._condition:
                entry.error = error
        finally:
            with self._condition:
                entry.starting = False
                self._condition.notify_all()

    def borrow(self, key, owner_factory, *, cancelled=None):
        self._check_key(key)
        with self._condition:
            if self._closed:
                raise PoolClosed('Suite is draining')
            entry = self._entries.get(key)
            if entry is None:
                deadline = time.monotonic() + self._budget
                entry = self._entries[key] = _Entry(deadline)
                context = contextvars.copy_context()
                try:
                    threading.Thread(target=context.run, args=(self._start, entry, owner_factory, deadline),
                                     daemon=True).start()
                except BaseException as error:
                    entry.error = error
                    entry.starting = False
                    self._condition.notify_all()
            if entry.cleanup_required:
                raise PoolPending('Prerequisite cleanup remains pending')
            while entry.starting:
                if self._closed:
                    raise PoolClosed('Suite is draining')
                if cancelled is not None and cancelled():
                    raise PoolCancelled('Borrower stopped waiting')
                remaining = entry.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Suite prerequisite preparation exceeded its budget')
                self._condition.wait(min(0.05, remaining))
            if self._closed:
                raise PoolClosed('Suite is draining')
            if entry.cleanup_required or self._entries.get(key) is not entry:
                raise PoolPending('Prerequisite cleanup remains pending')
            if cancelled is not None and cancelled():
                raise PoolCancelled('Borrower stopped waiting')
            if entry.error is not None:
                raise entry.error
            entry.borrowers += 1
            return Borrower(self, entry)

    def _cleanup(self, key, entry):
        with self._condition:
            if self._entries.get(key) is not entry:
                entry.disposing = False
                self._condition.notify_all()
                return
        error = None
        try:
            if entry.owner is not None:
                entry.owner.release()
        except BaseException as exc:
            error = exc
        with self._condition:
            # No other path can remove an entry while cleanup is running.
            # Still check identity before removing anything: a stale caller
            # must never delete a replacement.
            if self._entries.get(key) is entry:
                entry.cleanup_error = error
                entry.disposing = False
                if error is None:
                    del self._entries[key]
            self._condition.notify_all()

    def _dispose(self, key, entry, deadline):
        with self._condition:
            if self._entries.get(key) is not entry:
                return
            if entry.starting or entry.borrowers:
                raise PoolPending('Prerequisite remains in use or preparation')
            if not entry.disposing:
                entry.cleanup_required = True
                entry.cleanup_error = None
                entry.disposing = True
                context = contextvars.copy_context()
                try:
                    threading.Thread(target=context.run, args=(self._cleanup, key, entry), daemon=True).start()
                except BaseException as error:
                    entry.cleanup_error = error
                    entry.disposing = False
                    self._condition.notify_all()
            while entry.disposing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolPending('Prerequisite cleanup remains pending')
                self._condition.wait(remaining)
            if entry.cleanup_error is not None:
                raise entry.cleanup_error

    @staticmethod
    def _deadline(timeout):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('timeout must be finite and nonnegative')
        return time.monotonic() + timeout

    def invalidate(self, key, *, timeout=None):
        """Settle cleanup before allowing a new owner for this digest."""
        self._check_key(key)
        deadline = self._deadline(self._budget if timeout is None else timeout)
        with self._condition:
            entry = self._entries.get(key)
            if entry is None:
                return
        self._dispose(key, entry, deadline)

    def stop_admission(self):
        """Atomically stop future borrowers without draining existing owners."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def close(self, *, timeout):
        """Stop admission, drain, and dispose; retry after pending cleanup."""
        deadline = self._deadline(timeout)
        self.stop_admission()
        with self._condition:
            while any(e.starting or e.borrowers for e in self._entries.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolPending('Suite prerequisite drain remains pending')
                self._condition.wait(remaining)
        while True:
            with self._condition:
                if not self._entries:
                    return
                key, entry = next(iter(self._entries.items()))
            self._dispose(key, entry, deadline)
