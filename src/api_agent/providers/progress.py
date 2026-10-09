"""Operation-local component timing, independent of whole-test deadlines.

Names must be fixed descriptions, never URLs, account names or credentials.
Reporting observes the component's existing timeout; it does not enforce a
second deadline or extend that timeout. Nested components keep separate clocks.
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
    timeout: float
    elapsed: float
    active: bool

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or len(self.id) > 100
                or not isinstance(self.name, str) or not self.name
                or len(self.name) > 100 or not self.name.isprintable()
                or type(self.active) is not bool
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (self.timeout, self.elapsed))
                or self.timeout <= 0 or self.elapsed < 0):
            raise ValueError('Invalid component progress')


_sink = ContextVar('component_progress_sink', default=None)


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
    """Report start, one-second heartbeats and end for an existing timeout."""
    identifier = str(uuid4())
    ComponentProgress(identifier, name, timeout, 0, True)
    callback = _sink.get()
    if callback is None:
        yield
        return
    started = time.monotonic()

    def emit(active):
        try:
            callback(ComponentProgress(identifier, name, timeout,
                                       max(0, time.monotonic() - started), active))
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
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        emit(False)
