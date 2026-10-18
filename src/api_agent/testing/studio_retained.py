"""Separate retained browser activity from a finalized integration execution."""
import asyncio
from datetime import datetime, timezone
import json
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

from api_agent.providers.contracts import SessionClosedError, ProviderAccessError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.costs import quote_cost, validate_quote
from api_agent.providers.inference import usage_to_wire
from api_agent.telemetry.usage import SuiteUsage
from .inference_usage import capture


class StudioCleanup:
    """Retry ordered disposal without discarding children or later duties."""
    def __init__(self, callbacks=()):
        self.callbacks = list(callbacks)
        self.pending = None

    def push(self, callback, *args):
        self.callbacks.insert(0, partial(callback, *args))

    async def aclose(self):
        try:
            while self.callbacks:
                if self.pending is not None:
                    await self.pending.retry()
                    self.pending = None
                # A child receipt may finish only part of this callback. Require
                # its idempotent parent to acknowledge the remaining duties too.
                await self.callbacks[0]()
                self.callbacks.pop(0)
        except BaseException as error:
            self.pending = (error if isinstance(error, CleanupPendingError)
                            else getattr(error, 'cleanup_pending', None))
            receipt = CleanupPendingError((self,), self.aclose)
            if not isinstance(error, Exception):
                error.cleanup_pending = receipt
                raise
            raise receipt from None


class RetainedStudio:
    """One automated prompt, then a lazy separately accounted interactive owner.

    The gate belongs to the recording operation. Retiring the test handle never
    grants manual access by itself. No pytest event publishers are used here.
    """
    def __init__(self, automated, factory, ready):
        self.automated, self.factory, self.ready = automated, factory, ready
        self.phase = 'test'
        self.manual = None
        self.usage = SuiteUsage(str(uuid4()))
        self.identity = SimpleNamespace(id='interactive:' + self.usage.run_id)
        self._creation = asyncio.Lock()
        self._cleanup = None

    def retire_test(self):
        if self.phase == 'test':
            self.phase = 'waiting'

    async def _provider(self):
        if self.phase == 'closed':
            raise SessionClosedError('Studio inspection has ended.')
        if self.phase == 'test':
            return None
        if not self.ready():
            raise SessionClosedError('Studio waits for finalized suite accounting.')
        async with self._creation:
            if self.phase == 'closed':
                raise SessionClosedError('Studio inspection has ended.')
            if self.manual is None:
                self.manual = self.factory(self.usage.observe_snapshot)
            self.phase = 'interactive'
        return self.manual

    async def describe(self):
        provider = await self._provider()
        if provider is None:
            from api_agent.runtime.tasks import offload
            return await offload(self.automated.describe)
        return await provider._describe()

    async def complete(self, request):
        provider = await self._provider()
        if provider is None:
            # Claim the only automated call before yielding to another request.
            self.retire_test()
            from api_agent.runtime.tasks import offload
            return await offload(self.automated.complete, request)
        from .provider_setup import _configured_request
        request = _configured_request(request, provider.config)
        call_id = str(uuid4())
        started = datetime.now(timezone.utc).isoformat()
        self._record(request, call_id, started)
        result = error = None
        try:
            result = await provider._complete(request)
            return result
        except BaseException as failure:
            error = failure
            raise
        finally:
            self._record(request, call_id, started, result=result, error=error, finished=True)

    def _record(self, request, call_id, started, *, result=None, error=None, finished=False):
        reported = error.usage if isinstance(error, ProviderAccessError) else None
        usage = result.usage if result is not None else reported
        row = dict(call_id=call_id, selection=request.selected_llm,
            usage=None if usage is None else usage_to_wire(usage),
            started_at=started, finished_at=datetime.now(timezone.utc).isoformat() if finished else None,
            attempt=1, turn_id=call_id, turn_kind='initial',
            outcome=result.outcome if result is not None else 'failed' if finished else 'unknown')
        effort = request.payload.generation.reasoning_effort
        if effort is not None:
            row['reasoning_effort'] = effort or None
        if result is not None:
            for name in ('actual_model', 'service_tier', 'serving_revision'):
                if getattr(result, name) is not None:
                    row[name] = getattr(result, name)
        try:
            if result is not None:
                try:
                    quote = self.manual.emissions.content_export(
                        quote_cost(self.manual.provider.implementation, request, result))
                    row['quote'] = validate_quote(quote)
                except Exception:
                    # Optional pricing cannot invalidate completed inference.
                    # Do not export provider or emission failure diagnostics.
                    pass
        finally:
            # Even an interrupted quote must retain terminal outcome and usage.
            capture(SimpleNamespace(text=json.dumps(row, allow_nan=False), identity=self.identity), self.usage)

    async def aclose(self):
        self.phase = 'closed'
        if self.manual is not None:
            if self._cleanup is None:
                async def inspection():
                    if self.manual.inspection is not None:
                        await self.manual.inspection.aclose()
                async def acknowledge():
                    self.manual.closed = True
                self._cleanup = StudioCleanup((self.manual._close, inspection, acknowledge))
            try:
                await self._cleanup.aclose()
            except BaseException as error:
                receipt = CleanupPendingError((self,), self.aclose)
                if not isinstance(error, Exception):
                    error.cleanup_pending = receipt
                    raise
                raise receipt from None


def retained_provider_factory(data, registry):
    """Use normal selected-account authentication without a retired IPC helper."""
    from .provider_setup import IntegrationLLM
    def create(observer):
        return IntegrationLLM(data['config'], data['owner_root'],
            unattended_index_source=data.get('unattended_index_source'),
            password_manager_registry=registry, loop=asyncio.get_running_loop(),
            quota_observer=observer, skip_unavailable=False)
    return create
