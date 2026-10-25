"""Retain display readers until their threads finish, even after UI cancellation."""
import asyncio
from contextvars import copy_context
from functools import partial


class BackgroundReads:
    def __init__(self):
        self.pending = {}

    async def run(self, name, function, *args, cancel=None, **kwargs):
        call = partial(function, *args, **kwargs)
        future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, call)
        self.pending[future] = name, cancel
        future.add_done_callback(self._finished)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if cancel is not None:
                cancel()
            raise

    def _finished(self, future):
        self.pending.pop(future)
        # A cancelled UI waiter must not leave a late read failure unobserved.
        future.exception()

    def cancel(self, name=None):
        for label, cancel in tuple(self.pending.values()):
            if cancel is not None and (name is None or name == label):
                cancel()

    async def close(self, output):
        self.cancel()
        if not self.pending:
            return
        _, pending = await asyncio.wait(tuple(self.pending), timeout=.2)
        if not pending:
            return
        names = sorted({self.pending[future][0] for future in pending})
        output('Finishing background work: ' + ', '.join(names)
               + '. Waiting for current reads to finish.')
        await asyncio.gather(*pending, return_exceptions=True)
        output('Background work finished.')
