"""Own the projection child and bound its lifetime.

On Darwin, sampled resident memory is a best-effort watchdog, not a hard
memory cap: allocations between samples can exceed the threshold.
"""
import asyncio
import ctypes
import os
import signal
import sys

from .contracts import ContractError
from .progress import report_timeout
from api_agent.runtime.tasks import settle

_RSS_LIMIT = 512 * 1024 * 1024
_WALL_SECONDS = 10
_POLL_SECONDS = .05


def _darwin_resident_bytes(pid):
    # proc_taskinfo begins with uint64 virtual_size and resident_size. The
    # kernel fills the fixed-size public PROC_PIDTASKINFO structure.
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = libproc.proc_pidinfo
    query.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    info = (ctypes.c_uint64 * 12)()
    if query(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError('Process memory measurement unavailable')
    return info[1]


def _child_exited(pid):
    # WNOWAIT observes an exited owned child without consuming its status from
    # asyncio's child watcher. A missing child has already been reaped.
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


async def communicate(process, payload):
    """Communicate with an owned child; kill and reap it on every failure."""
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
                    return exchange.result()
                if sys.platform == 'darwin' and process.returncode is None:
                    try:
                        resident = _darwin_resident_bytes(process.pid)
                    except (OSError, AttributeError, TypeError, ValueError):
                        # A non-consuming kernel child-status check avoids
                        # depending on event-loop reaping latency.
                        try:
                            exited = _child_exited(process.pid)
                        except (OSError, AttributeError, ValueError):
                            exited = False
                        if exited or process.returncode is not None or exchange.done():
                            continue
                        raise ContractError('Evidence projection memory measurement unavailable') from None
                    if resident > _RSS_LIMIT:
                        raise ContractError('Evidence projection memory limit exceeded')
    finally:
        if not exchange.done():
            # The projector starts a fresh session. Kill its entire group even
            # if its leader exited, since a child may still hold a pipe open.
            try:
                if os.name == 'posix' and hasattr(os, 'killpg'):
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            # Finish draining pipes and reaping even under repeated cancellation.
            try:
                await settle(exchange)
            except BaseException:
                pass
        elif process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await settle(asyncio.create_task(process.wait()))
