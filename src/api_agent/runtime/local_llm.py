"""Persistent local inference ownership, independent of test executions."""
import asyncio
from copy import deepcopy
from types import MappingProxyType
import time
from uuid import uuid4

from api_agent.auth.bindings import bind_role_credentials
from api_agent.auth.managers import registry_for_owner
from api_agent.providers.cache_policy import configured_request, validate_provider_request
from api_agent.providers.contracts import SetupPrerequisiteError, ProviderAccessError
from api_agent.providers.control import OperationContext
from api_agent.providers.costs import quote_cost, validate_quote
from api_agent.providers.inference import async_request_to_wire, usage_to_wire
from api_agent.providers.loading import aload_provider
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import _Owner, _drain, LLMSessionOwner, InspectionOwner, ScopedLLMCredentials
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy


class _RegisteredSession(LLMSessionOwner):
    """Register refreshed string authentication before every owned callback."""
    def __init__(self, provider, *, emissions, **options):
        super().__init__(provider, **options)
        self.emissions = emissions

    async def call(self, operation, *, context=None):
        async def registered(*, auth):
            if type(auth) is str and auth:
                self.emissions.register_secret(auth)
            return await operation(auth=auth)
        return await super().call(registered, context=context)


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
                 on_session=None):
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
        self._usage = []
        self._operation_id = str(uuid4())
        self.policy = ModelSelectionPolicy(self.config)

    @property
    def usage(self):
        return tuple(deepcopy(self._usage))

    async def _initialize(self):
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
                credentials=scoped, interactive=bool(self.config.get('DEBUG')) or not self.config.get('NO_STD_IN', False),
                timeout=self._timeout)
            return self.session
        async def complete(request, *, auth):
            return await implementation.acomplete(request, auth=auth)
        self.shared = SharedSetupOwner(self._operation_id, self._operation_id, owner_factory,
            credentials=credentials, describe=getattr(implementation, 'adescribe', None),
            complete=complete, validate_request=lambda request: validate_provider_request(implementation, request),
            timeout=self._timeout)
        self._resources.append((self.shared, 'aclose'))
        start = getattr(self.provider, 'astart', None)
        if start is not None:
            await start()

    async def _get(self):
        self._check()
        if self._initialization is None:
            with independent_timeout_scope():
                self._initialization = asyncio.create_task(self._initialize())
        await asyncio.shield(self._initialization)
        self._check()
        return self.shared

    async def _run(self, operation, context=None):
        self._check()
        owned = context is None
        context = context or OperationContext(str(uuid4()), time.monotonic() + self.timeout)
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            async with component_timeout('Local LLM request', context.remaining() or None):
                return await context.run(operation())
        finally:
            self._calls.discard(task)
            if owned:
                await context.aclose(timeout=self._timeout)

    async def ensure_ready(self):
        async def ready():
            shared = await self._get()
            await shared.ensure_ready()
        return await self._run(ready)

    async def describe(self, *, context=None):
        async def describe():
            shared = await self._get()
            return self.policy.filter(await shared.describe())
        return await self._run(describe, context)

    async def complete(self, request, *, context=None):
        async def complete():
            nonlocal request
            request = configured_request(request, self.config.get('LLM_CACHE_POLICY'),
                reasoning_effort=self.config.get('LLM_REASONING_EFFORT'))
            await async_request_to_wire(request)
            self.policy.require_allowed(request.selected_llm)
            shared = await self._get()
            result = None
            error = None
            try:
                result = await shared.complete(request)
                return result
            except BaseException as failure:
                error = failure
                raise
            finally:
                usage = result.usage if result is not None else error.usage if isinstance(error, ProviderAccessError) else None
                row = {'call_id': str(uuid4()), 'selection': request.selected_llm,
                       'outcome': result.outcome if result is not None else 'failed',
                       'usage': None if usage is None else usage_to_wire(usage)}
                try:
                    if result is not None:
                        row['quote'] = validate_quote(self.emissions.content_export(quote_cost(self.provider.implementation, request, result)))
                except Exception:
                    pass
                self._usage.append(row)
                del self._usage[:-128]
        return await self._run(complete, context)

    async def call(self, operation, *, context=None):
        return await self._run(lambda: operation(auth=None), context)

    async def _dispose(self):
        with independent_timeout_scope():
            async with report_timeout('Local LLM cleanup', self._timeout):
                await _drain(self._calls, self._timeout, self._cancel_requested)
                if self._initialization is not None:
                    await _drain((self._initialization,), self._timeout, self._cancel_requested)
                await self._release()
                # Thread-backed credential helpers are acknowledged by completion.
                if self._helpers:
                    done, pending = await asyncio.wait(self._helpers, timeout=self._timeout)
                    if pending:
                        from api_agent.providers.control import CleanupPendingError
                        raise CleanupPendingError((self,), self.aclose)
                await self.inspection.aclose()
