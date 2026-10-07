import asyncio
import inspect
from django.core.handlers.asgi import ASGIHandler
from api_agent.providers.loading import ProviderError
from .routes import RouteRegistrar
"""Compose a Django ASGI application without opening listeners or loading secrets."""

import asyncio
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
    emissions: object = field(default=None, repr=False)
    routes: object = field(default_factory=RouteRegistrar, repr=False)
    urlconf: object = field(default=None, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _draining: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _start_task: object = field(default=None, init=False, repr=False)
    _close_task: object = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.pack_id, str) or not self.pack_id.strip():
            raise ValueError('pack_id must be a nonempty string')

    @property
    def started(self): return self._started

    @property
    def ready(self): return self._started and not self._draining

    @property
    def draining(self): return self._draining

    def require_llm_provider(self):
        if self.llm_provider is None:
            raise ProviderError('provider_location_required')
        return self.llm_provider.implementation

    def start(self):
        if self._closed:
            raise RuntimeLifecycleError('runtime_closed')
        self._started, self._draining = True, False

    def stop(self):
        self._started, self._draining = False, True

    async def _start_owned(self):
        services = {'sessions': self.sessions, 'emissions': self.emissions}
        for owner in (self.llm_provider, self.identity_adapter):
            setup = getattr(owner, 'setup_routes', None)
            if setup is not None:
                result = setup(self.routes, services)
                if inspect.isawaitable(result):
                    await result
            start = getattr(owner, 'astart', None)
            if start is not None:
                await start()
        self.urlconf = self.routes.freeze()
        self.start()

    async def astart(self):
        if self._closed:
            raise RuntimeLifecycleError('runtime_closed')
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start_owned())
        try:
            await self._start_task
        except BaseException as error:
            await self.aclose()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeLifecycleError('runtime_start_failed') from None

    async def _close_owned(self):
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            await asyncio.gather(self._start_task, return_exceptions=True)
        failed = False
        for owner in (self.sessions, self.identity_adapter, self.llm_provider):
            close = getattr(owner, 'aclose', None)
            if close is not None:
                try:
                    await close()
                except BaseException:
                    failed = True
        if failed:
            raise RuntimeLifecycleError('runtime_cleanup_failed')

    async def aclose(self):
        if self._close_task is None:
            self._closed = True
            self.stop()
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)


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
            await self._django_app({**scope, "api_agent_runtime": self.runtime}, receive, send)
        else:
            raise ValueError(f"Unsupported ASGI scope: {scope['type']}")


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
