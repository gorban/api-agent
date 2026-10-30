"""Compose a Django ASGI application without opening listeners or loading secrets."""
import asyncio
from api_agent.runtime.tasks import settle
import os
import inspect
import logging
from io import BytesIO
from django.core.handlers.asgi import ASGIHandler, ASGIRequest, get_script_prefix
from django.urls import resolve, Resolver404
from api_agent.providers.loading import ProviderError, _task
from .routes import RouteRegistrar
from api_agent.providers.control import CleanupPendingError, OperationalError
from .http_cleanup import close_http_callbacks

from dataclasses import dataclass, field
import re
from threading import Lock

from django.conf import settings
from django.core.asgi import get_asgi_application
from django.core.exceptions import DisallowedHost
from django.http import HttpResponseBadRequest


_SETTINGS_MARKER = "api_agent.api.foundation.v1"
_settings_lock = Lock()
_HOST_PATTERN = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")


class RuntimeLifecycleError(RuntimeError):
    pass


@dataclass
class Runtime:
    pack_id: str
    identity_adapter: object = field(default=None, repr=False)
    sessions: object = field(default=None, repr=False)
    llm_provider: object = field(default=None, repr=False)
    api_provider: object = field(default=None, repr=False)
    tool_plan: object = field(default=None, repr=False)
    tool_owners: dict = field(default_factory=dict, repr=False)
    _tool_views: list = field(default_factory=list, init=False, repr=False)
    upstream_provider: object = field(default=None, repr=False)
    access_provider: object = field(default=None, repr=False)
    pending_cleanup: object = field(default=None, init=False, repr=False)
    cleanup_warnings: tuple[str, ...] = field(default=(), init=False)
    ui_provider: object = field(default=None, repr=False)
    identity_plan: object = field(default=None, repr=False)
    factory_plan: object = field(default=None, repr=False)
    services: object = field(default=None, repr=False)
    emissions: object = field(default=None, repr=False)
    tracing: object = field(default=None, repr=False)
    routes: object = field(default_factory=RouteRegistrar, repr=False)
    urlconf: object = field(default=None, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _draining: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _start_task: object = field(default=None, init=False, repr=False)
    _close_task: object = field(default=None, init=False, repr=False)
    timeout: float = field(default=5, repr=False)
    _pid: int = field(default_factory=os.getpid, init=False, repr=False)
    _loop: object = field(default=None, init=False, repr=False)
    _cleanup_tasks: dict = field(default_factory=dict, init=False, repr=False)
    _cleaned: set = field(default_factory=set, init=False, repr=False)
    _retained_owners: list = field(default_factory=list, init=False, repr=False)
    _retained_kinds: dict = field(default_factory=dict, init=False, repr=False)
    _cancel_requested: set = field(default_factory=set, init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.pack_id, str) or not self.pack_id.strip():
            raise ValueError('pack_id must be a nonempty string')

    def _context(self):
        if self._pid != os.getpid():
            raise RuntimeLifecycleError('runtime_execution_context_invalid')
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is not None and self._loop is not loop:
            raise RuntimeLifecycleError('runtime_execution_context_invalid')
        self._loop = loop

    @property
    def started(self): return self._started

    @property
    def ready(self): return self._started and not self._draining

    @property
    def draining(self): return self._draining

    def require_provider(self, kind, operation):
        self._context()
        if self._closed or self._draining:
            raise RuntimeLifecycleError("runtime_closed")
        owner = getattr(self, kind + "_provider", None)
        if owner is None:
            raise ProviderError("provider_location_required")
        return owner.require(kind, operation)

    def borrow_tool(self, instance):
        self._context()
        if not self.ready or instance not in self.tool_owners:
            raise RuntimeLifecycleError('runtime_tool_unavailable')
        from api_agent.tooling.runtime import BorrowedTool
        owner = self.tool_owners[instance].require_tool()
        factory = getattr(owner, 'for_invocation', None)
        if factory is not None and (not callable(factory) or inspect.iscoroutinefunction(factory)):
            raise RuntimeLifecycleError('runtime_tool_view_invalid')
        view = factory() if factory is not None else owner
        borrowed = BorrowedTool(self, owner, view)
        self._tool_views.append(borrowed)
        if any(not inspect.iscoroutinefunction(getattr(view, name, None))
               for name in ('describe', 'validate', 'invoke', 'aclose')):
            raise RuntimeLifecycleError('runtime_tool_view_invalid')
        return borrowed

    def tools_for(self, definition, *, endpoints=None):
        from api_agent.tooling.runtime import WorkflowTools
        return WorkflowTools(self, definition, endpoints=endpoints)

    def require_llm_provider(self, operation="complete"):
        return self.require_provider("llm", operation)

    def start(self):
        self._context()
        if self._closed:
            raise RuntimeLifecycleError('runtime_closed')
        self._started, self._draining = True, False

    def stop(self):
        self._context()
        self._started, self._draining = False, True

    async def _start_owned(self):
        configurations = ()
        if self.tool_plan is not None:
            from api_agent.providers.loading import aload_tool
            from api_agent.providers.progress import component_timeout
            from api_agent.tooling.services import prepare_tool_configurations
            async with component_timeout('Tool admission', 180):
                configurations = await prepare_tool_configurations(self.tool_plan, self.services or {})
        if self.factory_plan is not None:
            for kind, location, config, services in self.factory_plan:
                from api_agent.providers.loading import aload_provider, provider_owner_root
                if kind == 'upstream' and config.get('API_PROVIDER'):
                    from types import MappingProxyType
                    owner_root = provider_owner_root(kind, config, services['owner_root'])
                    services = MappingProxyType({**services, 'owner_root': owner_root})
                try:
                    owner = await aload_provider(location, config, services, kind=kind)
                except BaseException as error:
                    retained = getattr(error, 'retained_owners', ())
                    self._retained_owners.extend(retained)
                    self._retained_kinds.update((id(owner), kind) for owner in retained)
                    raise
                setattr(self, kind + "_provider", owner)
                if self._closed:
                    raise RuntimeLifecycleError('runtime_closed')
            self.factory_plan = None
        if self.tool_plan is not None:
            for tool, config, services in configurations:
                if tool.id in self.tool_owners:
                    raise RuntimeLifecycleError('runtime_tool_duplicate')
                try:
                    async with component_timeout('Tool acquisition', 180):
                        owner = await aload_tool(tool.admission, config, services)
                        self.tool_owners[tool.id] = owner
                except BaseException as error:
                    retained = getattr(error, 'retained_owners', ())
                    self._retained_owners.extend(retained)
                    self._retained_kinds.update((id(owner), 'tool') for owner in retained)
                    raise
                if self._closed:
                    raise RuntimeLifecycleError('runtime_closed')
        if self.identity_plan is not None:
            self.identity_adapter = self.identity_plan()
            self.identity_plan = None
        from types import MappingProxyType
        services = self.services or MappingProxyType({'sessions': self.sessions, 'emissions': self.emissions})
        for owner in (self.tracing, self.upstream_provider, self.api_provider, self.llm_provider, self.access_provider, self.ui_provider, self.identity_adapter):
            if self._closed:
                raise RuntimeLifecycleError('runtime_closed')
            setup = getattr(owner, 'setup_routes', None)
            if setup is not None:
                result = setup(self.routes, getattr(owner, 'services', None) or services)
                if inspect.isawaitable(result):
                    await result
            if self._closed:
                raise RuntimeLifecycleError('runtime_closed')
            if owner is not None and owner is self.ui_provider:
                owner.require('ui', 'launch')
            start = getattr(owner, 'astart', None)
            if start is not None:
                await start()
        for owner in self.tool_owners.values():
            if self._closed:
                raise RuntimeLifecycleError('runtime_closed')
            from api_agent.providers.progress import component_timeout
            async with component_timeout('Tool startup', 180):
                await owner.astart()
        self.urlconf = self.routes.freeze()
        self.start()

    async def astart(self):
        self._context()
        if self._closed:
            raise RuntimeLifecycleError('runtime_closed')
        if self._start_task is None:
            self._start_task = _task(self._start_owned())
        try:
            await asyncio.shield(self._start_task)
        except BaseException as primary:
            try:
                await settle(_task(self.aclose()))
            except CleanupPendingError as pending:
                primary.cleanup_pending = pending
            if isinstance(primary, (asyncio.CancelledError, OperationalError)):
                raise
            error = RuntimeLifecycleError('runtime_start_failed')
            error.cleanup_pending = self.pending_cleanup
            error.primary_failure = primary
            raise error from None

    async def _close_owned(self):
        from api_agent.providers.sessions import _drain
        try:
            await _drain([self._start_task] if self._start_task is not None else [],
                         self.timeout, self._cancel_requested)
        except Exception:
            raise CleanupPendingError((self,), self.aclose) from None
        studio_backend = (self.services or {}).get('studio_backend')
        service_registry = (self.services or {}).get('service_registry')
        owners = (studio_backend, self.sessions, *reversed(self._tool_views), *self._retained_owners,
                  self.identity_adapter, self.ui_provider, self.llm_provider, self.access_provider,
                  *reversed(tuple(self.tool_owners.values())), self.api_provider, service_registry, self.upstream_provider, self.tracing)
        pending = []
        # API owners require upstream clients; every unresolved child retains tracing.
        for owner in owners:
            if owner is None:
                continue
            if any(any(child is view for view in self._tool_views) for child in pending) and any(owner is tool for tool in self.tool_owners.values()):
                pending.append(owner)
                continue
            if any(any(child is tool for tool in self.tool_owners.values())
                   or any(child is view for view in self._tool_views)
                   or self._retained_kinds.get(id(child)) == 'tool'
                   for child in pending) and (owner is service_registry or owner is self.upstream_provider):
                pending.append(owner)
                continue
            if service_registry is not None and any(child is service_registry for child in pending) and owner is self.upstream_provider:
                pending.append(owner)
                continue
            if any(child is studio_backend for child in pending) and owner is not studio_backend:
                pending.append(owner)
                continue
            if any(child is self.sessions or child is self.ui_provider or child is self.identity_adapter
                   or self._retained_kinds.get(id(child)) == 'ui'
                   for child in pending) and (owner is self.api_provider or owner is self.upstream_provider):
                pending.append(owner)
                continue
            if any(child is self.sessions for child in pending) and owner is not self.sessions:
                pending.append(owner)
                continue
            if owner is self.upstream_provider and any(child is self.api_provider or self._retained_kinds.get(id(child)) == 'api' for child in pending):
                pending.append(owner)
                continue
            if owner is self.tracing and pending:
                pending.append(owner)
                continue
            close = getattr(owner, 'aclose', None)
            key = id(owner)
            if close is None or key in self._cleaned:
                continue
            if key not in self._cleanup_tasks:
                async def dispose(selected=owner):
                    await selected.aclose()
                self._cleanup_tasks[key] = _task(dispose())
            task = self._cleanup_tasks[key]
            done, _ = await asyncio.wait({task}, timeout=self.timeout)
            if not done:
                pending.append(owner)
                continue
            try:
                task.result()
            except BaseException as error:
                from api_agent.telemetry.tracing import TraceDeliveryError, TraceRuntime, TRACE_DELIVERY_WARNING
                if (owner is self.tracing and isinstance(owner, TraceRuntime)
                        and isinstance(error, TraceDeliveryError) and error.disposal_confirmed
                        and owner.disposal_confirmed):
                    self._cleaned.add(key)
                    self.cleanup_warnings = (TRACE_DELIVERY_WARNING,)
                    logging.getLogger(__name__).error(TRACE_DELIVERY_WARNING)
                    continue
                del self._cleanup_tasks[key]
                pending.append(owner)
                continue
            self._cleaned.add(key)
        if pending:
            raise CleanupPendingError(tuple(pending), self.aclose)
        self.pending_cleanup = None

    async def aclose(self):
        self._context()
        if self._close_task is None or self._close_task.done() and self._close_task.exception() is not None:
            self._closed = True
            self.stop()
            self._close_task = _task(self._close_owned())
        try:
            await asyncio.shield(self._close_task)
        except CleanupPendingError as pending:
            self.pending_cleanup = pending
            raise


class RuntimeHandler(ASGIHandler):
    async def get_response_async(self, request):
        runtime = request.scope['api_agent_runtime']
        if runtime.urlconf is not None:
            request.urlconf = runtime.urlconf
        return await super().get_response_async(request)


class Application:
    def __init__(self, runtime, django_app):
        self.runtime = runtime
        self._django_app = django_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        await self.runtime.astart()
                    except Exception:
                        await send({"type": "lifespan.startup.failed", "message": "runtime_start_failed"})
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    try:
                        await self.runtime.aclose()
                    except Exception:
                        await send({"type": "lifespan.shutdown.failed", "message": "runtime_cleanup_failed"})
                        return
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            # The copy prevents a caller's reusable scope from retaining runtime state.
            cleanups = []
            try:
                request_scope = {**scope, "api_agent_runtime": self.runtime,
                                 "api_agent_http_cleanup": cleanups}
                backend = (self.runtime.services or {}).get('studio_backend')
                ingress = getattr(backend, 'handle_asgi', None)
                if ingress is None:
                    if (backend is not None or type(self._django_app) is not RuntimeHandler
                            or not await self._deferred_response(request_scope, send)):
                        await self._django_app(request_scope, receive, send)
                else:
                    await ingress(request_scope, receive, send, self._django_app)
            finally:
                await close_http_callbacks(cleanups)
        else:
            raise ValueError(f"Unsupported ASGI scope: {scope['type']}")


    async def _deferred_response(self, scope, send):
        """Resolve unavailable built-in capabilities without acquiring a request body."""
        from . import views
        path = scope.get('path', '').removeprefix(get_script_prefix(scope))
        urlconf = self.runtime.urlconf if self.runtime.urlconf is not None else settings.ROOT_URLCONF
        try:
            match = resolve(path, urlconf=urlconf)
        except Resolver404:
            return False
        if match.func not in (views.models, views.chat, views.chat_stream,
                views.chat_cancel, views.chat_heartbeat, views.result_set, views.result_sets):
            return False
        with BytesIO() as body:
            try:
                request = ASGIRequest(scope, body)
                request.get_host()
            except (DisallowedHost, UnicodeDecodeError):
                response = HttpResponseBadRequest()
            else:
                response = await match.func(request, *match.args, **match.kwargs)
        await send({'type': 'http.response.start', 'status': response.status_code,
                    'headers': [(name.lower().encode('latin1'), value.encode('latin1'))
                                for name, value in response.items()]})
        await send({'type': 'http.response.body', 'body': response.content})
        return True


class ValidateHostMiddleware:
    """Force Django's configured Host validation before any URL is resolved."""

    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        try:
            request.get_host()
        except DisallowedHost:
            return HttpResponseBadRequest()
        return await self.get_response(request)


def _validated_hosts(allowed_hosts):
    if isinstance(allowed_hosts, (str, bytes)):
        raise ValueError("allowed_hosts must be a sequence of host names")
    try:
        hosts = tuple(allowed_hosts)
    except TypeError as error:
        raise ValueError("allowed_hosts must be a sequence of host names") from error
    if not hosts or not all(
        isinstance(host, str)
        and host
        and _HOST_PATTERN.fullmatch(host) is not None
        for host in hosts
    ):
        raise ValueError("allowed_hosts must contain exact host names without schemes or ports")
    return hosts


def create_application(runtime: Runtime, *, allowed_hosts=("localhost", "127.0.0.1", "testserver"), workers=1, reload=False):
    """Build an ASGI app; caller owns its single-worker listener and lifespan."""
    if not isinstance(runtime, Runtime):
        raise TypeError("runtime must be a Runtime")
    if type(workers) is not int or workers != 1 or reload is not False:
        raise ValueError("The in-memory runtime requires one worker and reload=False")
    hosts = _validated_hosts(allowed_hosts)
    with _settings_lock:
        if settings.configured:
            if getattr(settings, "API_AGENT_API_MARKER", None) != _SETTINGS_MARKER:
                raise RuntimeError("Foreign Django settings are already configured")
            if settings.ALLOWED_HOSTS != list(hosts):
                raise RuntimeError("Django settings already configured with different allowed_hosts")
        else:
            settings.configure(
                API_AGENT_API_MARKER=_SETTINGS_MARKER,
                DEBUG=False,
                SECRET_KEY="api-agent-api-no-session-or-crypto-use",
                ROOT_URLCONF="api_agent.api.urls",
                ALLOWED_HOSTS=list(hosts),
                APPEND_SLASH=False,
                INSTALLED_APPS=[],
                MIDDLEWARE=["api_agent.api.application.ValidateHostMiddleware"],
                USE_TZ=True,
            )
        django_app = RuntimeHandler()
    return Application(runtime, django_app)
