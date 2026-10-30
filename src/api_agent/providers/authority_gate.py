"""Process-local asynchronous exclusion for overlapping authority upgrades.

The bookkeeping lock protects only short in-memory operations. Waiters sleep on
futures belonging to their own event loops, including when callers use threads.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from threading import Lock

_lock = Lock()
_states = {}


class _Waiter:
    def __init__(self, loop):
        self.loop = loop
        self.future = loop.create_future()
        self.granted = False


def _notify(waiter):
    if not waiter.future.done():
        waiter.future.set_result(None)


def _advance(key, state):
    """Grant the oldest live waiter; called with the bookkeeping lock held."""
    while state:
        waiter = state[0]
        waiter.granted = True
        try:
            waiter.loop.call_soon_threadsafe(_notify, waiter)
            return
        except RuntimeError:  # The waiting loop has already closed.
            state.popleft()
    del _states[key]


async def _acquire(authority):
    key = id(authority)
    waiter = _Waiter(asyncio.get_running_loop())
    with _lock:
        # Retain the object while the id is in use, preventing id reuse.
        entry = _states.get(key)
        if entry is None:
            state = deque([waiter])
            _states[key] = (authority, state)
            waiter.granted = True
            return waiter
        state = entry[1]
        state.append(waiter)
    try:
        await waiter.future
        return waiter
    except BaseException:
        with _lock:
            state.remove(waiter)
            if waiter.granted:
                _advance(key, state)
        raise


def _release(authority, waiter):
    key = id(authority)
    with _lock:
        state = _states[key][1]
        assert state[0] is waiter
        state.popleft()
        _advance(key, state)


@asynccontextmanager
async def acquire_authorities(authorities, timeout):
    """Acquire distinct authorities in canonical order within one deadline.

    Timeout and cancellation release any already acquired authorities. The
    caller owns the gate until the context exits, including during cancellation.
    """
    ordered = sorted({id(authority): authority for authority in authorities}.values(), key=id)
    acquired = []
    try:
        async with asyncio.timeout(timeout):
            for authority in ordered:
                waiter = await _acquire(authority)
                acquired.append((authority, waiter))
        yield
    finally:
        for authority, waiter in reversed(acquired):
            _release(authority, waiter)
