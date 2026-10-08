"""Explicit stream leases; disposal is awaited and retryable."""
import asyncio

from .contracts import ContractError, SessionClosedError, StreamEvent
from .control import OperationalError, CleanupPendingError, _cancel_once
from .sessions import _drain, _spawn


class ManagedStream:
    def __init__(self, owner, context):
        self._owner = owner
        self._context = context
        self._iterator = None
        self._next = None
        self._closing = None
        self._closed = False
        self._terminal = False
        self._iterating = False
        self._cancel_requested = set()

    def bind(self, iterator):
        if callable(getattr(iterator, "aclose", None)):
            self._iterator = iterator
            if self._closed:
                self._closing = None
            self._owner._streams.add(self)
        if not callable(getattr(iterator, "__anext__", None)) or self._iterator is None:
            raise ContractError("Stream requires an async iterator and awaited close")

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._iterating:
            raise ContractError("Concurrent stream iteration is unsupported")
        self._iterating = True
        try:
            return await self._read_next()
        finally:
            self._iterating = False

    async def _read_next(self):
        self._owner._check()
        if self._closed:
            raise StopAsyncIteration
        if self._next is not None and not self._next.done():
            raise ContractError("Concurrent stream iteration is unsupported")
        if self._context is not None and self._context.remaining() == 0:
            raise OperationalError("deadline", execution="possibly_executed")
        self._next = _spawn(self._iterator.__anext__())
        try:
            if self._context is None:
                event = await asyncio.shield(self._next)
            else:
                remaining = self._context.remaining()
                done, _ = await asyncio.wait({self._next}, timeout=remaining)
                if not done:
                    raise OperationalError("deadline", execution="possibly_executed")
                event = self._next.result()
            if not isinstance(event, StreamEvent):
                raise ContractError("Invalid stream event")
            if self._terminal:
                raise ContractError("Stream event follows terminal usage")
            self._terminal = event.final
            return event
        except StopAsyncIteration:
            await self.aclose()
            if not self._terminal:
                raise ContractError("Stream ended without terminal usage") from None
            raise
        except (asyncio.CancelledError, OperationalError, ContractError, SessionClosedError):
            if not self._next.done():
                _cancel_once(self._next, self._cancel_requested)
            raise
        except Exception:
            raise OperationalError("unknown", execution="possibly_executed") from None

    async def _close(self):
        if self._next is not None:
            await _drain([self._next], self._owner._timeout, self._cancel_requested)
        if self._iterator is not None:
            await self._iterator.aclose()
        self._owner._streams.discard(self)

    async def aclose(self):
        self._owner._context()
        self._closed = True
        if self._closing is None or self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None):
            self._closing = _spawn(self._close())
        try:
            done, _ = await asyncio.wait({self._closing}, timeout=self._owner._timeout)
            if not done:
                # Keep a running close alive. Repeated retries await that same close.
                raise CleanupPendingError((self,), self.aclose)
            self._closing.result()
        except (asyncio.CancelledError, CleanupPendingError):
            raise
        except Exception:
            raise CleanupPendingError((self,), self.aclose) from None
