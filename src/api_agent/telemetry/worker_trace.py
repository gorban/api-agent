"""Invocation-local worker receipt; never serialized or retained in an event row."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock

_CURRENT = ContextVar('worker_trace_receipt', default=None)
_MISSING = object()


class WorkerTraceOffer:
    def __init__(self, owner, call_id, kwargs, release=None, *, local=False):
        self.owner, self.call_id, self.kwargs = owner, call_id, kwargs
        self._local = local
        self.state = 'inactive' if local else None
        self._release = release
        self._closed = self._lost = False
        self._lock = RLock()
        self._evidence_done = False
        self._deferred = None

    def receipt(self, state):
        if not self._closed and not self._local:
            self.state = state if state in ('granted','inactive') else None

    @contextmanager
    def completion(self):
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)
            with self._lock:
                if self._deferred is None:
                    self.close()

    def defer(self, callback):
        """Retain one already-safe terminal trace row until owned evidence settles."""
        with self._lock:
            if self.state != 'inactive' or self._evidence_done:
                return False
            if self._deferred is not None:
                self.loss()
                return False
            self._deferred = callback
            return True

    def finalize_evidence(self, capture):
        # The existing evidence job owns this receipt; no ambient owner adoption.
        with self._lock:
            try:
                if self.state == 'inactive':
                    capture()
            finally:
                self._evidence_done = True
                callback, self._deferred = self._deferred, None
                if callback is not None:
                    try:
                        callback()
                    finally:
                        self.close()

    def loss(self):
        if not self._lost:
            self._lost = True
            self.owner.trace_losses.record()

    def close(self):
        if not self._closed:
            self._closed = True
            if self._release is not None:
                self._release()


def ownership(owner, call_id):
    receipt = _CURRENT.get()
    if receipt is None or receipt.owner is not owner or receipt.call_id != call_id or receipt._closed:
        return _MISSING
    if receipt.state is None:
        receipt.loss()
    return receipt.state


def defer(owner, call_id, callback):
    receipt = _CURRENT.get()
    return (receipt is not None and receipt.owner is owner and receipt.call_id == call_id
            and not receipt._closed and receipt.defer(callback))
