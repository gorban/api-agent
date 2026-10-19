"""Reasoning-only concurrent leases under the ordinary fixture's backend owner."""
import asyncio
from contextlib import asynccontextmanager
from threading import RLock

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.tasks import offload
from api_agent.testing.provider_setup import RemoteIntegrationLLM
from api_agent.testing.studio_http import _cleanup
from api_agent.testing.storage import _Cleanup
from api_agent.testing.suite_lock import _cleanup_receipts


def _pending(owner, *errors):
    # Preserve the existing direct/attached chain. Appending the new owner duty
    # keeps receipt consumers from retrying an acknowledged primary twice.
    receipts = tuple({id(receipt): receipt for error in errors
                      for receipt in _cleanup_receipts(error)}.values())
    pending = CleanupPendingError((owner,), owner.aclose)
    if not receipts:
        return pending
    first = receipts[0]
    for following in (*receipts[1:], pending):
        _Cleanup._append(first, following)
    return first


def _retain(error, pending, owner):
    if error is not pending:
        error.cleanup_pending = pending
    owners = (*getattr(error, 'retained_owners', ()), owner)
    error.retained_owners = tuple({id(item): item for item in owners}.values())


class _ReasoningProvider:
    def __init__(self, provider):
        self.provider = provider
        self._guard = RLock()
        self._catalog = RLock()
        self._calls = {}
        self._closed = False

    def describe(self):
        with self._catalog:
            with self._guard:
                if self._closed:
                    raise ContractError('Reasoning consumer is closed.')
            return self.provider.describe()

    def complete(self, request):
        if not isinstance(self.provider, RemoteIntegrationLLM):
            # Other fixtures may own a single synchronous creating loop.
            with self._catalog:
                with self._guard:
                    if self._closed:
                        raise ContractError('Reasoning consumer is closed.')
                return self.provider.complete(request)
        with self._guard:
            if self._closed:
                raise ContractError('Reasoning consumer is closed.')
            # Each lease uses the same backend capability and selected identity.
            # Ordinary clients retain their own loop, evidence and cost quote.
            child = RemoteIntegrationLLM(self.provider.client._descriptor, self.provider.config)
            guard = RLock()
            self._calls[child] = guard
        primary = None
        try:
            with guard:
                return child.complete(request)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self._close_call(child)
            except BaseException as error:
                pending = _pending(self, primary, error)
                if primary is not None:
                    _retain(primary, pending, self)
                elif not isinstance(error, Exception):
                    _retain(error, pending, self)
                    raise
                else:
                    raise pending from None

    def _close_call(self, child):
        with self._guard:
            guard = self._calls.get(child)
        if guard is None:
            return
        with guard:
            child.close()
        with self._guard:
            self._calls.pop(child, None)

    def close(self):
        with self._guard:
            self._closed = True
            children = tuple(self._calls)
        # Wait for any borrowed standalone/catalog callback before releasing.
        with self._catalog:
            pass
        failures = []
        for child in children:
            try:
                self._close_call(child)
            except BaseException as error:
                failures.append(error)
        if failures:
            pending = _pending(self, *failures)
            interruption = next((error for error in failures if not isinstance(error, Exception)), None)
            if interruption is not None:
                _retain(interruption, pending, self)
                raise interruption
            raise pending from None

    async def aclose(self):
        await offload(self.close)


@asynccontextmanager
async def reasoning_provider(provider):
    """Own only child transport leases; the ordinary fixture owns authentication."""
    parallel = _ReasoningProvider(provider)
    try:
        yield parallel
    except BaseException as primary:
        with parallel._guard:
            parallel._closed = True
        if any(_cleanup_receipts(primary)):
            # Native/endpoint duties must acknowledge before child loops close.
            _retain(primary, _pending(parallel, primary), parallel)
        else:
            try:
                await _cleanup(parallel.aclose)
            except BaseException as error:
                _retain(primary, _pending(parallel, primary, error), parallel)
        raise
    else:
        with parallel._guard:
            parallel._closed = True
        await _cleanup(parallel.aclose)
