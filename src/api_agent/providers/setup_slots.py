"""Bound per-operation worker identities while preserving replay high-water marks."""
import os
from queue import Empty, Queue
from threading import Lock
from uuid import uuid4

from .contracts import ContractError

_POOLS = {}
_LOCK = Lock()


class _Slots:
    def __init__(self):
        self._available = Queue(maxsize=8)
        for _ in range(8):
            self._available.put([str(uuid4()), 0])

    def acquire(self, timeout):
        try:
            slot = self._available.get(timeout=timeout)
        except Empty:
            raise ContractError('Setup worker slots are unavailable') from None
        slot[1] += 1
        return slot

    def release(self, slot):
        self._available.put_nowait(slot)


def slots_for(descriptor):
    # A capability identifies an operation; include the process to prevent a
    # fork from inheriting the parent's queue and identity high-water marks.
    key = (os.getpid(), descriptor['capability'], descriptor['host'], descriptor['port'])
    with _LOCK:
        return _POOLS.setdefault(key, _Slots())
