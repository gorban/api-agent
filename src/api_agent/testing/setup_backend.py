"""One lazy, operation-owned setup host across ordinary integration workers."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import report_timeout


class _SetupOwner:
    """Keep provider and credential objects in the host's creating event loop."""
    def __init__(self, backend):
        from .provider_setup import IntegrationLLM
        self.provider = IntegrationLLM(backend.config, backend.owner_root,
            unattended_index_source=backend.unattended_index_source,
            loop=asyncio.get_running_loop(),
            password_manager_registry=backend.password_manager_registry,
            interaction=backend.interaction, quota_observer=backend.quota_observer)

    async def ensure_ready(self):
        await self.provider._ensure()

    async def describe(self):
        return await self.provider._describe()

    async def complete(self, request):
        return await self.provider._complete(request)

    def export_content(self, request, result=None):
        from .inference_content import export_content
        return export_content(self.provider.emissions, request, result)

    def quote_cost(self, request, result):
        from api_agent.providers.costs import quote_cost
        return self.provider.emissions.content_export(quote_cost(self.provider.provider.implementation, request, result))

    async def validate_request(self, request):
        await self.provider._preflight(request)

    async def aclose(self):
        await self.provider._close()
        self.provider.closed = True


from .suite_lock import CleanupLeaseOwner


class SetupBackend(CleanupLeaseOwner):
    """Initialization is inert; start allocates IPC, the first request loads setup.

    Pending disposal also implements the existing consumer cleanup-handle seam.
    It represents backend cleanup, rather than a retained browser inspection.
    """
    timeout = 300
    cleanup_timeout = 5
    lost = False
    retains_browser = False

    def __init__(self, config, owner_root, *, unattended_index_source=None,
                 password_manager_registry=None, interaction=None, quota_observer=None):
        self.quota_observer = quota_observer
        self.interaction = interaction
        self._config = deepcopy(dict(config))
        if self._config.get('DEBUG'):
            raise ContractError('Debug setup must remain worker-local.')
        self._owner_root = Path(owner_root).resolve()
        from api_agent.auth.managers import registry_for_owner
        self._password_manager_registry = (password_manager_registry if password_manager_registry is not None
                                           else registry_for_owner(self._owner_root))
        self._unattended_index_source = unattended_index_source
        self._operation_id = str(uuid4())
        managers = [(item.id, str(item.implementation), item.cli_default)
                    for item in self._password_manager_registry.definitions]
        frozen = json.dumps({'config': self._config, 'owner_root': str(self._owner_root),
                             'password_managers': managers},
                            sort_keys=True, separators=(',', ':'), default=str)
        self._selected_identity = hashlib.sha256(frozen.encode()).hexdigest()
        self.loop = self.thread = self.registry = self.server = None
        self.descriptor = None
        self.closed = self.pending = False
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._thread_error = None
        self._start_future = self._close_future = None
        self._disposed = self._stopping = False

    @property
    def config(self):
        return deepcopy(self._config)

    @property
    def owner_root(self):
        return self._owner_root

    @property
    def password_manager_registry(self):
        return self._password_manager_registry

    @property
    def unattended_index_source(self):
        return self._unattended_index_source

    @property
    def operation_id(self):
        return self._operation_id

    @property
    def selected_identity(self):
        return self._selected_identity

    def _run(self):
        try:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._ready.set()
            self.loop.run_forever()
        except BaseException as error:
            self._thread_error = error
            self._ready.set()
        finally:
            if self._stopping and self.loop is not None:
                self.loop.close()

    def _start_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self._ready.clear()
            self._thread_error = None
            self.thread = threading.Thread(target=self._run, name='api-agent-setup-backend', daemon=True)
            self.thread.start()
        if not self._ready.wait(self.cleanup_timeout):
            raise self._receipt() from None
        if self._thread_error is not None:
            raise ContractError('Setup backend loop is unavailable.') from None

    async def _initialize(self):
        from api_agent.providers.setup_registry import SetupRegistry
        from api_agent.providers.setup_transport import SetupServer
        async with report_timeout('Shared setup backend startup', self.cleanup_timeout):
            self.registry = SetupRegistry(self.operation_id, self.selected_identity,
                                          lambda: _SetupOwner(self), timeout=self.cleanup_timeout)
            self.server = SetupServer(self.registry, str(uuid4()), timeout=self.timeout)
            self.descriptor = await self.server.start()
            return deepcopy(self.descriptor)

    def _receipt(self):
        error = CleanupPendingError((self,), self.aclose)
        error.inspections = (self,)
        return error

    def start(self):
        with self._lock:
            if self.closed or self.pending:
                raise ContractError('Setup backend is closed or cleanup is pending.')
            if self.descriptor is not None:
                return deepcopy(self.descriptor)
            try:
                self._start_thread()
                self._start_future = asyncio.run_coroutine_threadsafe(self._initialize(), self.loop)
                return self._start_future.result(timeout=self.cleanup_timeout)
            except BaseException as error:
                try:
                    self.close()
                except BaseException:
                    error.inspections = (self,)
                    error.cleanup_pending = self._receipt()
                raise

    async def _dispose(self):
        async with report_timeout('Shared setup backend cleanup', self.cleanup_timeout):
            if self._start_future is not None and not self._start_future.done():
                try:
                    await asyncio.wrap_future(self._start_future)
                except Exception:
                    pass
            failed = False
            interruption = None
            for owner in (self.server, self.registry):
                if owner is not None:
                    try:
                        await owner.aclose()
                    except BaseException as error:
                        failed = True
                        if not isinstance(error, Exception):
                            interruption = interruption or error
            if interruption is not None:
                # Raising KeyboardInterrupt inside an asyncio task stops its loop.
                # Return it for the synchronous owner to preserve after disposal.
                return interruption
            if failed:
                raise self._receipt() from None

    def close(self):
        with self._lock:
            if self.closed:
                return
            if self.loop is None and (self.thread is None or not self.thread.is_alive()):
                self.closed = True
                self._acknowledge_closed()
                return
            try:
                if not self._disposed:
                    self._start_thread()
                    if (self._close_future is None or (self._close_future.done()
                            and (self._close_future.cancelled() or self._close_future.exception() is not None
                                 or isinstance(self._close_future.result(), BaseException)))):
                        self._close_future = asyncio.run_coroutine_threadsafe(self._dispose(), self.loop)
                    interruption = self._close_future.result(timeout=self.cleanup_timeout)
                    if interruption is not None:
                        raise interruption
                    self._disposed = True
                # Disposal acknowledgement precedes stopping the creating loop.
                self._stopping = True
                if not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=self.cleanup_timeout)
                if self.thread.is_alive():
                    raise self._receipt() from None
                self.closed = True
                self.pending = False
                self._acknowledge_closed()
            except BaseException as error:
                self.pending = True
                if isinstance(error, Exception):
                    raise self._receipt() from None
                error.inspections = (self,)
                error.cleanup_pending = self._receipt()
                raise

    async def aclose(self):
        from api_agent.runtime.tasks import offload
        await offload(self.close)

    def end_inspection(self):
        try:
            self.close()
        except Exception:
            return False
        return self.closed

    def abort(self):
        self.close()

    def wait_closed(self):
        self.close()
