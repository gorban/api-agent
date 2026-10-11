"""Backend-local operation authentication owner; no worker IPC transport.

A backend must route all consumers for the selected identity to this instance.
Only readiness and bounded catalog metadata are consumer results. Provider
sessions, credential readers and browsers remain inside the creating process.
"""
import asyncio

from .contracts import ContractError, SessionClosedError
from .sessions import _Owner, _drain, _spawn
from .control import OperationalError, CleanupPendingError, finish_cleanup
from .progress import independent_timeout_scope


class RevocableCredentials(_Owner):
    """Own asynchronous reads and reject replies after operation revocation.

    The supplied reader must already enforce the selected role/account/kinds.
    Cancellation does not prove a helper stopped; surviving work stays owned.
    """
    def __init__(self, operation_id, reader, *, timeout=5):
        super().__init__(timeout)
        if not isinstance(operation_id, str) or not operation_id or not callable(reader):
            raise ContractError('Invalid operation credential binding')
        self.operation_id = operation_id
        self._reader = reader
        self._reads = set()

    def revoke(self):
        self._context()
        self._closed = True

    async def __call__(self, *args, **kwargs):
        self._check()
        try:
            task = _spawn(self._reader(*args, **kwargs))
            self._reads.add(task)
            task.add_done_callback(self._reads.discard)
            result = await asyncio.shield(task)
        except (asyncio.CancelledError, ContractError, SessionClosedError, OperationalError, CleanupPendingError):
            raise
        except Exception:
            raise OperationalError("unknown") from None
        self._check()
        return result

    async def _dispose(self):
        await _drain(self._reads, self._timeout, self._cancel_requested)


class SharedSetupOwner(_Owner):
    """One lazy owner for one operation's selected provider/account identity.

    ``owner_factory(credentials)`` synchronously creates an LLMSessionOwner
    (or an equivalent owner with async call/aclose). It must not acquire resources
    before returning ownership. Concurrent checkpoints share a task, including
    the provider's cache/refresh path. A cancelled waiter never cancels setup.
    Failed checkpoints remain failed for this operation: replay requires a new
    operation owner. This is deliberately not a cross-process registry.
    """
    def __init__(self, operation_id, selected_identity, owner_factory, *, credentials=None, describe=None, complete=None, timeout=5):
        super().__init__(timeout)
        if (not isinstance(operation_id, str) or not operation_id
                or not isinstance(selected_identity, str) or not selected_identity
                or not callable(owner_factory)):
            raise ContractError('Invalid shared setup binding')
        self.operation_id = operation_id
        self._selected_identity = selected_identity
        self._factory = owner_factory
        self._credentials = (RevocableCredentials(operation_id, credentials, timeout=timeout)
                             if credentials is not None else None)
        self._owner = None
        self._ready = None
        self._disposal_started = False
        self._describe = describe
        self._catalog = None
        self._complete = complete
        self._calls = set()

    @property
    def selected_identity(self):
        return self._selected_identity

    async def _checkpoint(self):
        if self._owner is None:
            try:
                owner = self._factory(self._credentials)
                if not callable(getattr(owner, 'call', None)) or not callable(getattr(owner, 'aclose', None)):
                    raise ContractError('Invalid shared session owner')
                self._owner = owner
            except ContractError:
                raise
            except Exception:
                raise OperationalError("unknown") from None
        self._check()

        async def ready(*, auth):
            if auth is None:
                raise ContractError('Provider did not provide authentication')
            self._check()
        await self._owner.call(ready)
        self._check()

    async def ensure_ready(self, *, selected_identity=None):
        self._check()
        if selected_identity is not None and selected_identity != self._selected_identity:
            raise ContractError("Selected identity is outside shared binding")
        # Success may be checked again to let the one provider owner refresh.
        # Failures are sticky so multiple waiters cannot initiate auth replay.
        if self._ready is None or (self._ready.done() and not self._ready.cancelled()
                                   and self._ready.exception() is None):
            with independent_timeout_scope():
                self._ready = _spawn(self._checkpoint())
        await asyncio.shield(self._ready)
        self._check()

    async def describe(self, *, selected_identity=None):
        await self.ensure_ready(selected_identity=selected_identity)
        if not callable(self._describe):
            from .contracts import SetupPrerequisiteError
            raise SetupPrerequisiteError('The selected LLM provider has no catalog capability.')
        if self._catalog is None:
            with independent_timeout_scope():
                self._catalog = _spawn(self._owner.call(self._describe))
        result = await asyncio.shield(self._catalog)
        self._check()
        from .catalog import catalog_to_wire
        catalog_to_wire(result)
        return result

    async def complete(self, request, *, selected_identity=None):
        from .inference import request_to_wire, result_to_wire
        request_to_wire(request)
        await self.ensure_ready(selected_identity=selected_identity)
        if not callable(self._complete):
            from .contracts import SetupPrerequisiteError
            raise SetupPrerequisiteError('The selected LLM provider has no text completion capability.')
        async def invoke(*, auth):
            result = await self._complete(request, auth=auth)
            self._check()
            result_to_wire(result)
            return result
        task = _spawn(self._owner.call(invoke))
        self._calls.add(task)
        try:
            result = await asyncio.shield(task)
            self._check()
            return result
        finally:
            try:
                await finish_cleanup(_drain([task], self._timeout, self._cancel_requested))
            finally:
                if task.done():
                    self._calls.discard(task)
                    self._cancel_requested.discard(task)

    async def aclose(self):
        self._context()
        if self._credentials is not None:
            self._credentials.revoke()
        await super().aclose()

    async def _dispose(self):
        await _drain(self._calls, self._timeout, self._cancel_requested)
        await _drain([task for task in (self._ready, self._catalog) if task is not None],
                     self._timeout, self._cancel_requested)
        if not self._disposal_started:
            self._disposal_started = True
            # Reverse release order keeps credential helpers until auth stops.
            if self._credentials is not None:
                self._resources.append((self._credentials, 'aclose'))
            if self._owner is not None:
                self._resources.append((self._owner, 'aclose'))
        await self._release()
