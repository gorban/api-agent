"""One lazy, operation-owned setup host across integration workers."""
import asyncio
from copy import deepcopy
from concurrent.futures import TimeoutError as FutureTimeout
import hashlib
import json
from pathlib import Path
import threading
import time
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
        from api_agent.workflows.inference_capture import bind_boundary
        bind_boundary(self.provider.emissions)
        return await self.provider._complete(request)

    def quote_cost(self, request, result):
        from api_agent.providers.costs import quote_cost
        return self.provider.emissions.content_export(quote_cost(self.provider.provider.implementation, request, result))

    async def validate_request(self, request):
        await self.provider._preflight(request)

    async def aclose(self):
        await self.provider._close()
        self.provider.closed = True

    async def finish_execution(self):
        await self.provider._finish_execution()


from .suite_lock import CleanupLeaseOwner


class SetupBackend(CleanupLeaseOwner):
    """Initialization is inert; start allocates IPC, the first request loads setup.

    Pending disposal and retained inspection use the same consumer cleanup handle.
    """
    timeout = 300
    cleanup_timeout = 5
    lost = False
    retains_browser = False

    def __init__(self, config, owner_root, *, unattended_index_source=None,
                 password_manager_registry=None, interaction=None, quota_observer=None):
        from api_agent.providers.setup_transport import _timeout
        self.timeout = _timeout(config.get('LLM_SETUP_REQUEST_TIMEOUT_SECONDS', self.timeout))
        self.quota_observer = quota_observer
        self.interaction = interaction
        self._config = deepcopy(dict(config))
        self.debug = self._config.get('DEBUG') is True
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
        self._publication_future = None
        self._publication_prepared = False
        self._disposed = self._stopping = False
        self.run_complete = False
        self.inspection_open = False
        self._inspection_wait_tasks = {}
        self._inspection_monitor = None

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
            if self._publication_future is not None and not self._publication_future.done():
                try:
                    await asyncio.wrap_future(self._publication_future)
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

    def _inspector(self):
        owner = getattr(self.registry, '_owner', None)
        provider = getattr(owner, 'provider', None)
        return getattr(provider, 'inspection', None)

    async def _prepare_publication(self):
        try:
            async with report_timeout('Shared setup execution finish', self.cleanup_timeout):
                if self.server is not None:
                    await self.server.aclose()
                if self.registry is not None:
                    await self.registry.finish_execution()
        except BaseException as error:
            if not isinstance(error, Exception):
                return error
            raise

    def prepare_publication(self):
        """Stop worker calls and capture the final quota before suite publication."""
        with self._lock:
            if self._publication_prepared or self.closed:
                return
            if self.pending:
                raise self._receipt() from None
            self._start_thread()
            self._publication_future = asyncio.run_coroutine_threadsafe(self._prepare_publication(), self.loop)
            try:
                interruption = self._publication_future.result(timeout=self.cleanup_timeout)
                if interruption is not None:
                    raise interruption
            except BaseException as error:
                self.pending = True
                if not isinstance(error, Exception):
                    error.inspections = (self,)
                    error.cleanup_pending = self._receipt()
                    raise
                raise self._receipt() from None
            self._publication_prepared = True

    def finish_run(self):
        """Revoke worker leases after reporting, retaining only owned debug windows."""
        with self._lock:
            if self.run_complete:
                return self.inspection_endpoints
            if self.closed:
                self.run_complete = True
                return ()
            if self.pending:
                raise self._receipt()
            if not self.debug:
                self.close()
                self.run_complete = True
                return ()
            if not self._disposed:
                self._start_thread()
                self._close_future = asyncio.run_coroutine_threadsafe(self._dispose(), self.loop)
                try:
                    interruption = self._close_future.result(timeout=self.cleanup_timeout)
                    if interruption is not None:
                        raise interruption
                except BaseException as error:
                    self.pending = True
                    if not isinstance(error, Exception):
                        error.inspections = (self,)
                        error.cleanup_pending = self._receipt()
                        raise
                    raise self._receipt() from None
                self._disposed = True
            inspector = self._inspector()
            self.inspection_open = bool(inspector is not None and inspector.sessions)
            self.retains_browser = self.inspection_open
            if not self.inspection_open:
                self.close()
            self.run_complete = True
            return self.inspection_endpoints

    @property
    def inspection_endpoints(self):
        inspector = self._inspector()
        if inspector is None:
            return ()
        return tuple(session.inspection_endpoint for session in inspector.sessions
                     if session.inspection_endpoint)

    def watch_inspection(self, on_event):
        """Release naturally closed windows and report the confirmed disposition."""
        with self._lock:
            if not self.inspection_open or self._inspection_monitor is not None:
                return
            def monitor():
                try:
                    self.wait_closed()
                    kind = 'inspection_closed'
                except BaseException:
                    if self._stopping and self.closed:
                        return
                    kind = 'inspection_closed' if self.closed else 'inspection_cleanup_pending'
                try:
                    on_event(kind)
                except Exception:
                    pass
                finally:
                    if self.closed:
                        with self._lock:
                            self._acknowledge_closed()
            watcher = threading.Thread(target=monitor,
                name='api-agent-inspection-monitor', daemon=True)
            self._inspection_monitor = watcher
            try:
                watcher.start()
            except BaseException:
                if watcher.ident is None:
                    self._inspection_monitor = None
                raise

    async def _dispose_inspection(self):
        inspector = self._inspector()
        if inspector is not None and (not inspector._closed or inspector._resources):
            await inspector.aclose()

    async def _cancel_inspection_wait(self):
        tasks = [task for task in self._inspection_wait_tasks.values()
                 if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _close_owned(self):
        with self._lock:
            if self.closed:
                self.pending = False
                return
            if self.loop is None and (self.thread is None or not self.thread.is_alive()):
                self.closed = True
                self.pending = False
                return
            try:
                if self.loop is not None and self.loop.is_closed():
                    if not self._disposed or self.debug and not self._stopping:
                        raise self._receipt() from None
                    self.thread.join(timeout=self.cleanup_timeout)
                    if self.thread.is_alive():
                        raise self._receipt() from None
                    self.closed = True
                    self.pending = False
                    return
                self._start_thread()
                if not self._disposed:
                    if (self._close_future is None or (self._close_future.done()
                            and (self._close_future.cancelled() or self._close_future.exception() is not None
                                 or isinstance(self._close_future.result(), BaseException)))):
                        self._close_future = asyncio.run_coroutine_threadsafe(self._dispose(), self.loop)
                    interruption = self._close_future.result(timeout=self.cleanup_timeout)
                    if interruption is not None:
                        raise interruption
                    self._disposed = True
                if self.debug:
                    inspection = self._inspector()
                    if inspection is not None and (not inspection._closed or inspection._resources):
                        future = asyncio.run_coroutine_threadsafe(self._dispose_inspection(), self.loop)
                        future.result(timeout=self.cleanup_timeout)
                    self.inspection_open = False
                # Disposal acknowledgement precedes stopping the creating loop.
                self._stopping = True
                if self.debug and self._inspection_wait_tasks:
                    future = asyncio.run_coroutine_threadsafe(self._cancel_inspection_wait(), self.loop)
                    future.result(timeout=self.cleanup_timeout)
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=self.cleanup_timeout)
                if self.thread.is_alive():
                    raise self._receipt() from None
                self.closed = True
                self.pending = False
            except BaseException as error:
                self.pending = True
                if isinstance(error, Exception):
                    raise self._receipt() from None
                error.inspections = (self,)
                error.cleanup_pending = self._receipt()
                raise

    def close(self):
        self._close_owned()
        monitor = self._inspection_monitor
        if monitor is threading.current_thread():
            # The monitor acknowledges only after its callback has returned.
            return
        if monitor is not None and monitor.ident is not None:
            monitor.join(timeout=self.cleanup_timeout)
            if monitor.is_alive():
                self.pending = True
                raise self._receipt() from None
        with self._lock:
            self._acknowledge_closed()

    async def aclose(self):
        from api_agent.runtime.tasks import offload
        await offload(self.close)

    def end_inspection(self, *, timeout=None):
        try:
            self.close()
        except Exception:
            return False
        return self.closed

    def abort(self):
        self.close()

    def wait_closed(self, *, timeout=None):
        if self.inspection_open:
            token = uuid4()
            async def wait():
                self._inspection_wait_tasks[token] = asyncio.current_task()
                inspector = self._inspector()
                try:
                    if inspector is not None:
                        await inspector.wait_closed()
                finally:
                    self._inspection_wait_tasks.pop(token, None)
            async def drain():
                task = self._inspection_wait_tasks.get(token)
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            with self._lock:
                if self.loop.is_closed():
                    raise self._receipt() from None
                self._start_thread()
                future = asyncio.run_coroutine_threadsafe(wait(), self.loop)
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                try:
                    future.result(timeout=min(self.cleanup_timeout, remaining)
                                  if remaining is not None else self.cleanup_timeout)
                    break
                except FutureTimeout:
                    if deadline is not None and time.monotonic() >= deadline:
                        future.cancel()
                        try:
                            asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=self.cleanup_timeout)
                        except BaseException:
                            self.pending = True
                            raise self._receipt() from None
                        return False
                    with self._lock:
                        if self.loop.is_closed():
                            self.pending = True
                            raise self._receipt() from None
                        self._start_thread()
        self.close()
        return self.closed
