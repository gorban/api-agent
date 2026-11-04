"""Own the projection child and bound its lifetime.

On Darwin, sampled resident memory is a best-effort watchdog, not a hard
memory cap: allocations between samples can exceed the threshold.
"""
import asyncio
import ctypes
import inspect
from functools import partial
import os
import sys
import subprocess

from .contracts import ContractError
from .progress import report_timeout
from api_agent.runtime.tasks import settle

_RSS_LIMIT = 512 * 1024 * 1024
_WALL_SECONDS = 10
_POLL_SECONDS = .05


async def _offload_owned(action, *args, **options):
    """Retain a worker operation through cancellation of all ordinary tasks."""
    future = asyncio.get_running_loop().run_in_executor(None, partial(action, *args, **options))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await settle(future)
        except BaseException:
            pass
        raise


class OwnedProjection:
    """Keep the actual projector leader unreaped until group disposal."""
    def __init__(self, process):
        self.process = process
        self._exchange_started = False

    def _close_pipes(self):
        for name in ('stdin', 'stdout', 'stderr'):
            pipe = getattr(self.process, name)
            if pipe is not None:
                pipe.close()

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        return self.process.returncode

    async def communicate(self, payload):
        from api_agent.testing.processes import communicate_owned
        self._exchange_started = True
        try:
            return await _offload_owned(communicate_owned, self.process, payload)
        finally:
            self._close_pipes()

    def _dispose(self):
        from api_agent.testing.processes import finalize
        try:
            finalize(self.process)
        finally:
            if not self._exchange_started:
                self._close_pipes()

    async def dispose(self):
        await _offload_owned(self._dispose)

    async def resident_bytes(self):
        from api_agent.testing.processes import inspect_owned
        return await _offload_owned(inspect_owned, self.process, _darwin_resident_bytes)

async def spawn(*argv, **options):
    """Spawn without an asyncio child watcher consuming leader ownership."""
    # Executor futures retain the result even when shutdown cancels every task.
    starting = asyncio.get_running_loop().run_in_executor(
        None, partial(subprocess.Popen, list(argv), **options))
    try:
        return OwnedProjection(await asyncio.shield(starting))
    except asyncio.CancelledError:
        process = OwnedProjection(await settle(starting))
        from .control import finish_cleanup
        await finish_cleanup(process.dispose())
        raise


def _darwin_resident_bytes(pid):
    # proc_taskinfo begins with uint64 virtual_size and resident_size. The
    # kernel fills the fixed-size public PROC_PIDTASKINFO structure.
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = libproc.proc_pidinfo
    query.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    info = (ctypes.c_uint64 * 12)()
    ctypes.set_errno(0)
    if query(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError(ctypes.get_errno(), 'Process memory measurement unavailable')
    return info[1]


def _child_exited(pid):
    # WNOWAIT observes an exited owned child without consuming its status from
    # the owner. A missing child has already been reaped.
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


async def communicate(process, payload):
    """Communicate with an owned child; kill and reap it on every failure."""
    dispose = getattr(process, 'dispose', None)
    if not inspect.iscoroutinefunction(dispose):
        raise ContractError('Evidence projection ownership unavailable')
    exchange = asyncio.create_task(process.communicate(payload))
    try:
        async with report_timeout('Evidence projection', _WALL_SECONDS) as budget:
            while True:
                remaining = budget.remaining
                if remaining <= 0:
                    raise ContractError('Evidence projection timed out')
                done, _ = await asyncio.wait({exchange}, timeout=min(_POLL_SECONDS, remaining))
                # Completion can be delivered after the deadline even when the
                # child finished while this task was waiting to be scheduled.
                if budget.remaining <= 0:
                    raise ContractError('Evidence projection timed out')
                if done:
                    result = exchange.result()
                    break
                if sys.platform == 'darwin' and process.returncode is None:
                    try:
                        sample = getattr(process, 'resident_bytes', None)
                        resident = (await sample() if sample is not None
                                    else _darwin_resident_bytes(process.pid))
                    except (OSError, AttributeError, TypeError, ValueError) as error:
                        # A non-consuming kernel child-status check avoids
                        # depending on event-loop reaping latency.
                        try:
                            exited = _child_exited(process.pid)
                        except (OSError, AttributeError, ValueError):
                            exited = False
                        if exited or process.returncode is not None or exchange.done():
                            continue
                        number = getattr(error, 'errno', None)
                        detail = f' (errno {number})' if type(number) is int else ''
                        raise ContractError('Evidence projection memory measurement unavailable' + detail) from None
                    if budget.remaining <= 0:
                        raise ContractError('Evidence projection timed out')
                    if resident is None:
                        continue
                    if resident > _RSS_LIMIT:
                        raise ContractError('Evidence projection memory limit exceeded')
    finally:
        async def cleanup():
            try:
                await dispose()
            finally:
                # The pipe exchange has its own ownership barrier. Even repeated
                # cancellation must finish group disposal and the IO thread.
                try:
                    await settle(exchange)
                except BaseException:
                    pass
        from .control import finish_cleanup
        await finish_cleanup(cleanup())
    if budget.remaining <= 0:
        raise ContractError('Evidence projection timed out')
    return result
