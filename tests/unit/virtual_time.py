"""Run a coroutine on controlled time, so a declared timeout expires by trigger rather than by latency.

The loop clock, the progress deadline clock and the workflow engine's own deadline reads all follow one
virtual clock. It advances only when every task is waiting on a timer and no executor call is in flight,
straight to the nearest timer. A scope's budget is therefore exhausted exactly when the scripted work
outwaits it, and never because a loaded host stalled the loop between two steps.
"""
import asyncio
import time
import types
from unittest import mock

from api_agent.providers import progress
from api_agent.workflows import execution


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _Selector:
    """Defer to the real selector for I/O and wake-ups; turn an idle wait for a timer into a clock jump."""

    def __init__(self, real, loop):
        self._real, self._loop = real, loop

    def select(self, timeout=None):
        ready = self._real.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None or self._loop.executing:
            # A worker thread will wake the loop; time must not pass while it runs.
            return self._real.select(None)
        self._loop.clock.now += timeout
        return []

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Loop(asyncio.SelectorEventLoop):
    def __init__(self, clock):
        super().__init__()
        self.clock, self.executing = clock, 0
        self._selector = _Selector(self._selector, self)

    def time(self):
        return self.clock.now

    def run_in_executor(self, executor, func, *args):
        self.executing += 1
        future = super().run_in_executor(executor, func, *args)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, _future):
        self.executing -= 1

    async def shutdown_default_executor(self, timeout=None):
        # Joining the pool is real work too; its own 300 second warning must not elapse virtually.
        self.executing += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self.executing -= 1


def run(coroutine):
    """``asyncio.run`` on virtual time."""
    clock = _Clock()
    shim = types.SimpleNamespace(monotonic=clock.monotonic, time=time.time)
    with (mock.patch.object(progress, '_clock', clock.monotonic), mock.patch.object(execution, 'time', shim),
          asyncio.Runner(loop_factory=lambda: _Loop(clock)) as runner):
        return runner.run(coroutine)
