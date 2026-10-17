"""Selected UI-provider preparation with one browser and cleanup owner."""
import asyncio
from types import MappingProxyType

from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.control import _cancel_once, CleanupPendingError, finish_cleanup
from api_agent.providers.loading import aload_provider
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.sessions import ScopedRoleCredentials
from .provider import listener_plan


class PreparedUISession:
    """Own launch, original-browser handoff and retryable reverse cleanup.

    Inject runtime services (including TLS/process ownership) from the operation.
    Retain this owner until inspection ends; closing it closes all three owners.
    No LLM setup, account fallback or legacy authentication hook is invoked here.
    """
    def __init__(self, config, services, *, browser_owner, user_index=None,
                 credentials=None, loader=aload_provider, runtime_owner=None, credentials_for=None,
                 retain_on_failure=False):
        if user_index is not None and (type(user_index) is not int or user_index < 0):
            raise ContractError('Invalid selected UI account')
        self.config = MappingProxyType(dict(config))
        self.services = services
        self.browser_owner = browser_owner
        self.user_index, self.credentials = user_index, credentials
        self.credentials_for = credentials_for
        self.retain_on_failure = retain_on_failure
        self.loader, self.runtime_owner = loader, runtime_owner
        self.provider = self.session = None
        self.closed = False
        self._ready = False
        self._lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._starting = None
        self._cancel_requested = set()
        self._released = set()
        self._release_task = None
        self._failed_owners = []

    async def start(self):
        owns_start = False
        try:
            async with component_timeout('UI session preparation', 60), self._lock:
                if self.closed:
                    raise SessionClosedError('UI session owner is closed')
                if self._ready:
                    return self.session
                self._starting = asyncio.current_task()
                owns_start = True
                location = self.config.get('UI_PROVIDER')
                if not location:
                    raise ContractError('Selected UI provider is required')
                origin = self.config.get('UI_LISTENER_URL', 'https://localhost:9443')
                listener_plan(origin)
                self.provider = await self.loader(location, self.config, self.services, kind='ui')
                implementation = self.provider.require('ui', 'launch')
                roles = implementation.capabilities.credential_roles
                if self.user_index is not None and roles:
                    if self.credentials is not None and self.credentials_for is not None:
                        raise ContractError('Ambiguous UI credential binding')
                    if self.credentials_for is not None:
                        self.credentials = self.credentials_for(roles, self.user_index)
                    reader = self.credentials
                    if (not isinstance(reader, ScopedRoleCredentials)
                            or not reader.matches_binding(roles, self.user_index)):
                        raise ContractError('UI credentials do not match selected roles/account')
                elif self.credentials is not None:
                    raise ContractError('Unexpected UI credential binding')
                await self.provider.astart()
                self.session = await self.browser_owner.start()
                browser, context = self.session.browser, self.session.context
                await self.session.page.goto(origin)
                if 'prepare_session' in implementation.capabilities.operations:
                    result = await self.provider.require('ui', 'prepare_session').aprepare_session(
                        session=self.session, user_index=self.user_index, credentials=self.credentials)
                    if result is not None:
                        raise ContractError('UI preparation must return None')
                if (self.session.browser is not browser or self.session.context is not context
                        or self.session.page.context is not context or self.session.page.is_closed()
                        or not browser.is_connected()):
                    raise ContractError('UI preparation replaced or closed the original session')
                if self.closed:
                    raise SessionClosedError('UI session closed during preparation')
                self._ready = True
                return self.session
        except BaseException as error:
            if not owns_start:
                raise
            self._failed_owners.extend(getattr(error, 'retained_owners', ()))
            if self.retain_on_failure:
                self.closed = True
                error.retained_owners = (self,)
                raise
            try:
                await finish_cleanup(self._release())
            except BaseException as cleanup:
                error.cleanup_pending = CleanupPendingError((self,), self.aclose)
                error.cleanup_error = cleanup
            raise
        finally:
            if self._starting is asyncio.current_task():
                self._starting = None

    async def aclose(self):
        """Release each owner once; retain incomplete duties for explicit retry."""
        self.closed = True
        starting = self._starting
        if starting is not None and starting is not asyncio.current_task() and not starting.done():
            _cancel_once(starting, self._cancel_requested)
            with independent_timeout_scope():
                async with report_timeout('UI preparation cancellation', 15) as budget:
                    done, _ = await asyncio.wait({starting}, timeout=budget.remaining)
                    if not done:
                        raise CleanupPendingError((self,), self.aclose)
        await self._release()

    async def _release(self):
        self.closed = True
        with independent_timeout_scope():
            async with component_timeout('UI cleanup ownership', 15), self._cleanup_lock:
                async with report_timeout('UI session cleanup', 15) as budget:
                    duties = [('browser', self.browser_owner),
                              *((f'failed-{index}', owner) for index, owner in enumerate(self._failed_owners)),
                              ('provider', self.provider), ('runtime', self.runtime_owner)]
                    for name, owner in duties:
                        if owner is None or name in self._released:
                            continue
                        if self._release_task is None:
                            self._release_task = asyncio.create_task(owner.aclose())
                        task = self._release_task
                        try:
                            done, _ = await asyncio.wait({task}, timeout=budget.remaining)
                            if not done:
                                raise CleanupPendingError((self,), self.aclose)
                            task.result()
                        except BaseException as error:
                            if task.done():
                                self._release_task = None
                            raise CleanupPendingError((self,), self.aclose) from error
                        self._release_task = None
                        self._released.add(name)
