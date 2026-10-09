"""Operation-local timing and optional nested deadline enforcement.

Names must be fixed descriptions, never URLs, account names or credentials.
Reporting observes deadlines without enforcing a second timeout. The optional
component_timeout context enforces the same budget. Nested components retain
their own clocks and cannot extend the earliest enclosing deadline.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import time
from uuid import uuid4


@dataclass(frozen=True)
class ComponentProgress:
    id: str
    name: str
    timeout: float | None
    elapsed: float
    active: bool
    parent_id: str | None = None

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or len(self.id) > 100
                or not isinstance(self.name, str) or not self.name
                or len(self.name) > 100 or not self.name.isprintable()
                or type(self.active) is not bool
                or (self.parent_id is not None and (not isinstance(self.parent_id, str)
                    or not self.parent_id or len(self.parent_id) > 100 or self.parent_id == self.id))
                or type(self.elapsed) not in (int, float) or not math.isfinite(self.elapsed)
                or (self.timeout is not None and (type(self.timeout) not in (int, float)
                    or not math.isfinite(self.timeout) or self.timeout < 0))
                or self.elapsed < 0):
            raise ValueError('Invalid component progress')


_sink = ContextVar('component_progress_sink', default=None)
_scope = ContextVar('component_timeout_scope', default=None)
_clock = time.monotonic


@dataclass(frozen=True)
class TimeoutBudget:
    """A scope's fixed entry budget and live remaining time, in seconds."""
    id: str
    parent_id: str | None
    started: float
    deadline: float | None
    timeout: float | None

    @property
    def remaining(self):
        return None if self.deadline is None else max(0, min(self.timeout, self.deadline - _clock()))


@contextmanager
def progress_sink(callback):
    """Bind timing reports to the current operation/test, including child tasks."""
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


@asynccontextmanager
async def report_timeout(name, timeout):
    """Observe a scope, yielding its budget clipped by all enclosing deadlines.

    None declares an untimed scope. Its inherited deadline still constrains
    timed descendants; progress marks it untimed for ancestor-display fallback.
    Zero is valid only as a computed exhausted budget, not a declared limit.
    """
    if timeout is not None and (type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Invalid component timeout')
    identifier = str(uuid4())
    started = _clock()
    parent = _scope.get()
    deadline = None if timeout is None else started + timeout
    effective_timeout = timeout
    if parent is not None and parent.deadline is not None:
        deadline = parent.deadline if deadline is None else min(deadline, parent.deadline)
        remaining = max(0, parent.deadline - started)
        effective_timeout = remaining if timeout is None else min(timeout, remaining)
    budget = TimeoutBudget(identifier, parent.id if parent else None, started, deadline, effective_timeout)
    reported_timeout = None if timeout is None else budget.timeout
    ComponentProgress(identifier, name, reported_timeout, 0, True, budget.parent_id)
    token = _scope.set(budget)
    try:
        callback = _sink.get()
        if callback is None:
            yield budget
            return

        def emit(active):
            try:
                callback(ComponentProgress(identifier, name, reported_timeout,
                                           max(0, _clock() - started), active, budget.parent_id))
            except Exception:
                # Presentation failures must not change authentication or its limits.
                pass

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                emit(True)

        emit(True)
        task = asyncio.create_task(heartbeat())
        try:
            yield budget
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            emit(False)
    finally:
        _scope.reset(token)


@asynccontextmanager
async def component_timeout(name, timeout=None):
    """Observe and enforce the scope's deadline; callers own nested resources.

    Ordinary async finally blocks finish before TimeoutError propagates. Work
    in threads/processes still needs its existing cancellation/drain ownership.
    """
    async with report_timeout(name, timeout) as budget:
        remaining = budget.remaining
        if remaining == 0:
            raise TimeoutError('Component deadline already expired')
        async with asyncio.timeout(remaining):
            yield budget
