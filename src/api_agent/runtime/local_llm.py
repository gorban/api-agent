"""Persistent local inference ownership, independent of test executions."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from itertools import islice
from types import MappingProxyType
import time
from uuid import uuid4

from api_agent.auth.bindings import bind_role_credentials
from api_agent.auth.managers import registry_for_owner
from api_agent.providers.cache_policy import configured_request, validate_provider_request
from api_agent.providers.contracts import SetupPrerequisiteError, ProviderAccessError
from api_agent.providers.control import CleanupPendingError, OperationContext
from api_agent.providers.costs import quote_cost, validate_quote
from api_agent.providers.inference import async_request_to_wire, usage_to_wire
from api_agent.providers.loading import aload_provider
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import _Owner, _drain, LLMSessionOwner, InspectionOwner, ScopedLLMCredentials
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy


def _create_task(coroutine):
    """Retain no fresh coroutine when the scheduler rejects its allocation."""
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


class _RegisteredSession(LLMSessionOwner):
    """Register refreshed string authentication before every owned callback."""
    def __init__(self, provider, *, emissions, on_authentication_failure, **options):
        super().__init__(provider, **options)
        self.emissions = emissions
        self.on_authentication_failure = on_authentication_failure

    async def _authenticate(self, context):
        try:
            return await super()._authenticate(context)
        except Exception:
            self.on_authentication_failure()
            raise

    async def call(self, operation, *, context=None):
        async def registered(*, auth):
            if type(auth) is str and auth:
                self.emissions.register_secret(auth)
            return await operation(auth=auth)
        return await super().call(registered, context=context)


@dataclass
class _RequestGeneration:
    initialization: asyncio.Task
    lifetime: asyncio.Future
    shared: object = None
    provider: object = None


class LocalLLM(_Owner):
    """Lazily bind one selected provider/account on its creating event loop.

    ``call`` adapts Studio callbacks, which themselves use describe/complete;
    it deliberately does not authenticate before calling those callbacks.
    Every request receives a fresh deadline unless its caller supplies a context.
    Usage is bounded, content-free and never published as test evidence.
    """
    def __init__(self, config, owner_root, *, emissions=None, registry=None,
                 unattended_index_source=None, interaction_owner=None,
                 loader=aload_provider, timeout=120, cleanup_timeout=5,
                 on_session=None, on_status=None, trace=None):
        super().__init__(cleanup_timeout)
        if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
            raise ValueError('Invalid local inference timeout')
        self.config = MappingProxyType(deepcopy(dict(config)))
        self.owner_root = owner_root
        self.emissions = emissions or EmissionBoundary(ContentPolicy())
        self.registry = registry
        self.unattended_index_source = unattended_index_source
        self.interaction_owner = interaction_owner
        self.loader, self.timeout = loader, timeout
        self.inspection = InspectionOwner(timeout=cleanup_timeout, on_session=on_session)
        self.provider = self.session = self.shared = None
        self._initialization = None
        self._helpers, self._calls = set(), set()
        self._active = set()
        self._requests = {}
        self._contexts = {}
        self._request_context = ContextVar('local_llm_request', default=None)
        self._failed_readiness = False
        self._recovery = None
        self._usage = []
        self._operation_id = str(uuid4())
        self.policy = ModelSelectionPolicy(self.config)
        self.on_status = on_status
        self.trace = trace

    def _status(self, category, outcome):
        """Publish fixed content-free status, never provider/session values."""
        if self.on_status is not None:
            try:
                self.on_status(MappingProxyType(dict(event='lifecycle', category=category,
                    outcome=outcome, operation_id=self._operation_id)))
            except BaseException:
                # Optional diagnostics cannot change execution or mask its error.
                pass

    @property
    def usage(self):
        return tuple(deepcopy(self._usage))

    async def _initialize(self):
        self._status('setup', 'started')
        location = self.config.get('LLM_PROVIDER')
        if not location:
            raise SetupPrerequisiteError('Select LLM_PROVIDER before requesting LLM setup.')
        services = {'owner_root': self.owner_root, 'emissions': self.emissions,
                    'browser_inspection': self.inspection}
        try:
            self.provider = await self.loader(location, deepcopy(dict(self.config)), services, kind='llm')
        except BaseException as error:
            self._resources.extend((owner, 'aclose') for owner in getattr(error, 'retained_owners', ()))
            raise
        # Reverse disposal retains inspection until provider and auth stop.
        self._resources.append((self.provider, 'aclose'))
        implementation = self.provider.implementation
        credentials = None
        index, role = self.config.get('UNATTENDED_USER_INDEX'), self.config.get('LLM_CREDENTIAL_ROLE')
        if index is not None and role:
            registry = self.registry if self.registry is not None else registry_for_owner(self.owner_root)
            reader = bind_role_credentials(self.config, (role,), index, registry=registry,
                emissions=self.emissions, pending_tasks=self._helpers,
                interaction_owner=self.interaction_owner, index_source=self.unattended_index_source)
            credentials = ScopedLLMCredentials(role, reader)
        def owner_factory(scoped):
            self.session = _RegisteredSession(implementation, emissions=self.emissions, user_index=index,
                on_authentication_failure=self._authentication_failed,
                credentials=scoped, interactive=bool(self.config.get('DEBUG')) or not self.config.get('NO_STD_IN', False),
                timeout=self._timeout)
            return self.session
        async def complete(request, *, auth):
            return await implementation.acomplete(request, auth=auth)
        self.shared = SharedSetupOwner(self._operation_id, self._operation_id, owner_factory,
            credentials=credentials, describe=getattr(implementation, 'adescribe', None),
            complete=complete, validate_request=lambda request: validate_provider_request(implementation, request),
            timeout=self._timeout, catalog_ttl=self.config.get('LLM_CATALOG_CACHE_TTL_SECONDS', 3600))
        self._resources.append((self.shared, 'aclose'))
        start = getattr(self.provider, 'astart', None)
        if start is not None:
            await start()
        self._status('setup', 'ready')

    def _authentication_failed(self):
        self._failed_readiness = True

    def _observe_readiness_failure(self, shared):
        ready = shared._ready
        if ready is not None and ready.done() and not ready.cancelled() and ready.exception() is not None:
            self._failed_readiness = True

    async def _recover(self):
        """Retire a failed generation before admitting another explicit action."""
        with independent_timeout_scope():
            async with report_timeout('Local LLM recovery', self._timeout):
                retained = tuple(self._active) + tuple(task for context in self._requests
                    for task in context.retained_tasks)
                if retained:
                    _, pending = await asyncio.wait(retained, timeout=self._timeout)
                    if pending:
                        raise CleanupPendingError((self,), self.ensure_ready)
                try:
                    await self._release()
                    if self._helpers:
                        _, pending = await asyncio.wait(tuple(self._helpers), timeout=self._timeout)
                        if pending:
                            raise CleanupPendingError((self,), self.ensure_ready)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise CleanupPendingError((self,), self.ensure_ready) from None
                self._check()
                self.provider = self.session = self.shared = None
                self._initialization = None
                self._failed_readiness = False

    def _watch_context(self, context):
        """A cancelled lifetime is not acknowledgement of its surviving work."""
        retained = context.retained_tasks
        if not retained:
            self._contexts.pop(context, None)
            self._requests.pop(context, None)
            return
        watched = self._contexts.setdefault(context, set())
        for task in retained:
            if task not in watched:
                watched.add(task)
                def released(finished):
                    watched.discard(finished)
                    self._watch_context(context)
                task.add_done_callback(released)

    @asynccontextmanager
    async def _lease(self):
        binding = await self._get()
        task = asyncio.current_task()
        self._active.add(task)
        try:
            yield binding.shared, binding.provider
        except BaseException:
            self._observe_readiness_failure(binding.shared)
            raise
        finally:
            self._active.discard(task)

    async def _get(self):
        self._check()
        context = self._request_context.get()
        binding = self._requests.get(context)
        if binding is None:
            if self._failed_readiness:
                if self._recovery is None or self._recovery.done():
                    with independent_timeout_scope():
                        self._recovery = _create_task(self._recover())
                await asyncio.shield(self._recovery)
                self._check()
            if self._initialization is None:
                async def initialize():
                    try:
                        await self._initialize()
                    except BaseException:
                        self._failed_readiness = True
                        self._status('setup', 'failed')
                        raise
                with independent_timeout_scope():
                    self._initialization = _create_task(initialize())
            # Studio transfers this context from catalog admission to another
            # task for completion. Its cleanup releases this generation even
            # when initialization failed before the first lease was admitted.
            lifetime = asyncio.get_running_loop().create_future()
            binding = _RequestGeneration(self._initialization, lifetime)
            self._requests[context] = binding
            context._tasks.add(lifetime)
            lifetime.add_done_callback(context._finished)
            self._watch_context(context)
        await asyncio.shield(binding.initialization)
        self._check()
        binding.shared, binding.provider = self.shared, self.provider
        return binding

    async def _run(self, operation, context=None):
        self._check()
        inherited = self._request_context.get()
        owned = context is None and inherited is None
        context = context or inherited or OperationContext(str(uuid4()), time.monotonic() + self.timeout)
        token = self._request_context.set(context)
        task = asyncio.current_task()
        self._calls.add(task)
        self._contexts.setdefault(context, set())
        async def tracked():
            self._watch_context(context)
            return await operation()
        try:
            async with component_timeout('Local LLM request', context.remaining() or None):
                return await context.run(tracked())
        finally:
            self._calls.discard(task)
            self._request_context.reset(token)
            try:
                if owned:
                    await context.aclose(timeout=self._timeout)
            finally:
                self._watch_context(context)

    async def ensure_ready(self):
        async def ready():
            async with self._lease() as (shared, provider):
                self._status('authentication', 'started')
                try:
                    await shared.ensure_ready()
                except BaseException:
                    self._status('authentication', 'failed')
                    raise
                self._status('authentication', 'ready')
        return await self._run(ready)

    async def describe(self, *, context=None):
        async def describe():
            async with self._lease() as (shared, provider):
                self._status('catalog', 'started')
                try:
                    result = self.policy.filter(await shared.describe())
                except BaseException:
                    self._status('catalog', 'failed')
                    raise
                if self.trace is not None:
                    try:
                        identities = tuple(item.selected_llm for item in islice(result, 256)
                            if type(item.selected_llm) is str and 0 < len(item.selected_llm) <= 200
                            and item.selected_llm.isprintable())
                        self.trace.bind_model_identities(identities)
                    except Exception:
                        # Optional trace metadata must not change catalog or request behavior.
                        pass
                self._status('catalog', 'ready')
                return result
        return await self._run(describe, context)

    async def complete(self, request, *, context=None):
        async def complete():
            nonlocal request
            request = configured_request(request, self.config.get('LLM_CACHE_POLICY'),
                reasoning_effort=self.config.get('LLM_REASONING_EFFORT'))
            await async_request_to_wire(request)
            self.policy.require_allowed(request.selected_llm)
            async with self._lease() as (shared, provider):
                self._status('request', 'accepted')
                result = None
                error = None
                receipts = []
                started = time.monotonic()
                try:
                    self._status('request', 'executing')
                    from api_agent.workflows.attribution import bind_terminal_call_receipts
                    with bind_terminal_call_receipts() as receipts:
                        result = await shared.complete(request)
                    self._status('request', 'completed')
                    return result
                except BaseException as failure:
                    error = failure
                    self._status('request', 'cancelled' if isinstance(failure, asyncio.CancelledError) else 'failed')
                    raise
                finally:
                    usage = result.usage if result is not None else error.usage if isinstance(error, ProviderAccessError) else None
                    row = {'call_id': str(uuid4()), 'selection': request.selected_llm,
                           'outcome': result.outcome if result is not None else
                               'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
                           'usage': None if usage is None else usage_to_wire(usage),
                           'actual_model': result.actual_model if result is not None else None,
                           'reasoning_effort': request.payload.generation.reasoning_effort or None}
                    try:
                        if result is not None:
                            row['quote'] = validate_quote(self.emissions.content_export(quote_cost(provider.implementation, request, result)))
                    except Exception:
                        pass
                    from api_agent.workflows.attribution import current_workflow_visit
                    owner = current_workflow_visit()
                    if owner is not None and not receipts:
                        journal, visit_id, attempt_id = owner
                        journal.record_call(call_id=row['call_id'], elapsed=time.monotonic() - started, visit_id=visit_id,
                            attempt_id=attempt_id, usage=row['usage'],
                            selection=row['selection'], outcome=row['outcome'],
                            actual_model=row['actual_model'], reasoning_effort=row['reasoning_effort'])
                    # A central publisher may already have admitted terminal calls
                    # during this exact operation. Reuse those receipts; a separate
                    # local UUID would charge and instrument the same work twice.
                    if len(receipts) == 1 and 'quote' in row:
                        receipts[0]['quote'] = row['quote']
                    self._usage.extend(receipts or [row])
                    del self._usage[:-128]
        return await self._run(complete, context)

    async def call(self, operation, *, context=None):
        return await self._run(lambda: operation(auth=None), context)

    async def _dispose(self):
        with independent_timeout_scope():
            async with report_timeout('Local LLM cleanup', self._timeout):
                await _drain(self._calls, self._timeout, self._cancel_requested)
                if self._recovery is not None:
                    await _drain((self._recovery,), self._timeout, self._cancel_requested)
                if self._initialization is not None:
                    await _drain((self._initialization,), self._timeout, self._cancel_requested)
                for context in tuple(self._contexts):
                    try:
                        await context.aclose(timeout=self._timeout)
                    except CleanupPendingError:
                        raise CleanupPendingError((self,), self.aclose) from None
                    self._watch_context(context)
                await self._release()
                # Thread-backed credential helpers are acknowledged by completion.
                if self._helpers:
                    done, pending = await asyncio.wait(self._helpers, timeout=self._timeout)
                    if pending:
                        raise CleanupPendingError((self,), self.aclose)
                await self.inspection.aclose()
