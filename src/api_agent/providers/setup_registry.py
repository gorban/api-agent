"""Backend-local leases for one lazily created authentication owner."""
import asyncio
from collections.abc import Mapping

from .contracts import ContractError, SessionClosedError
from .sessions import _Owner, _spawn, _drain
from .progress import independent_timeout_scope


_KEYS = frozenset(('version', 'operation_id', 'selected_identity', 'worker_id', 'attempt'))


def _identifier(value):
    if type(value) is not str or not value or len(value) > 512:
        raise ContractError('Invalid setup identifier')
    return value


class SetupRegistry(_Owner):
    def __init__(self, operation_id, selected_identity, owner_factory, *, timeout=5):
        super().__init__(timeout)
        self._operation_id = _identifier(operation_id)
        self._selected_identity = _identifier(selected_identity)
        if not callable(owner_factory):
            raise ContractError('Invalid setup owner factory')
        self._factory = owner_factory
        self._owner = None
        self._ready = None
        self._leases = {}
        self._history = {}

    @property
    def operation_id(self):
        return self._operation_id

    @property
    def selected_identity(self):
        return self._selected_identity

    def register(self, worker_id, attempt=1):
        self._check()
        _identifier(worker_id)
        if type(attempt) is not int or attempt < 1:
            raise ContractError('Invalid setup attempt')
        old = self._leases.get(worker_id)
        if old is not None and (not old['revoked'] or attempt <= old['binding']['attempt']):
            raise ContractError('Duplicate or stale setup registration')
        binding = dict(version=1, operation_id=self.operation_id,
                       selected_identity=self.selected_identity, worker_id=worker_id, attempt=attempt)
        self._leases[worker_id] = dict(binding=binding, revoked=False, waiters=set())
        self._history[worker_id, attempt] = self._leases[worker_id]
        return binding.copy()

    def _lease(self, binding):
        self._context()
        if not isinstance(binding, Mapping) or set(binding) != _KEYS:
            raise ContractError('Invalid setup binding')
        if type(binding['version']) is not int or binding['version'] != 1:
            raise ContractError('Invalid setup version')
        _identifier(binding['operation_id'])
        _identifier(binding['selected_identity'])
        _identifier(binding['worker_id'])
        if type(binding['attempt']) is not int or binding['attempt'] < 1:
            raise ContractError('Invalid setup attempt')
        lease = self._history.get((binding['worker_id'], binding['attempt']))
        if lease is None or lease['binding'] != binding:
            raise ContractError('Setup binding is outside operation')
        return lease

    async def _checkpoint(self):
        with independent_timeout_scope():
            if self._owner is None:
                owner = self._factory()
                self._owner = owner
                # A returned disposer transfers ownership even when readiness is invalid.
                if callable(getattr(owner, 'aclose', None)):
                    self._resources.append((owner, 'aclose'))
                if not callable(getattr(owner, 'ensure_ready', None)) or not callable(getattr(owner, 'aclose', None)):
                    raise ContractError('Invalid setup owner')
            self._check()
            await self._owner.ensure_ready()
            self._check()

    async def ensure_ready(self, binding):
        self._check()
        lease = self._lease(binding)
        if lease['revoked']:
            raise SessionClosedError('Setup lease is revoked')
        if self._ready is None or (self._ready.done() and not self._ready.cancelled()
                                   and self._ready.exception() is None):
            self._ready = _spawn(self._checkpoint())
        async def wait():
            await asyncio.shield(self._ready)
        waiter = _spawn(wait())
        lease['waiters'].add(waiter)
        try:
            await asyncio.shield(waiter)
            self._check()
            if lease['revoked']:
                raise SessionClosedError('Setup lease is revoked')
        finally:
            await _drain([waiter], self._timeout, self._cancel_requested)
            lease['waiters'].discard(waiter)

    async def revoke(self, binding):
        lease = self._lease(binding)
        lease['revoked'] = True
        await _drain(lease['waiters'], self._timeout, self._cancel_requested)

    async def _dispose(self):
        for lease in self._leases.values():
            lease['revoked'] = True
        await _drain([waiter for lease in self._leases.values() for waiter in lease['waiters']],
                     self._timeout, self._cancel_requested)
        await _drain([self._ready] if self._ready is not None else [], self._timeout, self._cancel_requested)
        await self._release()
