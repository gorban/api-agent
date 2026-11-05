"""Explicit HTTP tracing authority for automated and retained Studio operations."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import logging
import asyncio

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime, TraceDeliveryError, TRACE_DELIVERY_WARNING
from api_agent.workflows.capture_authority import bind_capture_services
from api_agent.workflows.observations import WorkflowObservationFactory
from .workflow_capture import capture_config


class StudioTraceCleanupPending(RuntimeError):
    """Tracing disposal is unacknowledged; its collector lease stays owned."""


class StudioTraceOwner:
    """Own separate exporters/collector leases without owning authentication.

    A selected provider still owns credentials, inference and manual-accounting
    admission. The supplied collector factory reuses the shared lease manager.
    """
    def __init__(self, config, sources, provider, *, capture_services=None,
                 collector_factory=None, runtime_factory=TraceRuntime, owner_root=None, collector_snapshot=None):
        self.config = capture_config(config, sources)
        self.provider = provider
        self.owner_root = owner_root
        self.capture_services = dict(capture_services or {})
        self.collector_snapshot = collector_snapshot
        self.collector_factory, self.runtime_factory = collector_factory, runtime_factory
        self._sessions = {}
        self._unavailable = set()
        self._creation = asyncio.Lock()
        self._current = ContextVar('studio_selected_trace', default=None)
        self._closed = False
        self._cleanup_fatal = False
        self._http = None
        self._http_bridge = False
        from api_agent.telemetry.http_tracing import diagnostics_level
        self.http_diagnostics_level = diagnostics_level(self.config.values)

    def bind_http(self, origin, nonce, *, backend=None):
        from api_agent.api.studio import StudioBackend
        self._http = (origin, nonce)
        self._http_bridge = type(backend) is StudioBackend

    def _authorized(self, scope):
        if self._http is None or type(scope) is not dict:
            return False
        import hmac
        import ipaddress
        try:
            if not ipaddress.ip_address(scope.get('client', ('',))[0]).is_loopback:
                return False
            headers = {}
            for name, value in scope.get('headers', ()):
                key = name.decode('ascii').lower()
                if key in headers:
                    return False
                headers[key] = value.decode('latin-1')
            origin, nonce = self._http
            return (headers.get('origin') == origin and
                    hmac.compare_digest(headers.get('x-api-agent-session', '').encode(), nonce.encode()))
        except (ValueError, UnicodeError, IndexError, TypeError):
            return False

    async def astart(self):
        # HTTP listener setup and help/configuration never authenticate or export.
        pass

    def current(self):
        return self._current.get()

    def bind_catalog(self, selections):
        runtime = self.current()
        if runtime is not None:
            runtime.bind_model_identities(tuple(row.selected_llm for row in selections))

    @asynccontextmanager
    async def request(self, scope=None):
        from api_agent.telemetry.http_tracing import HTTPMetadata, current_http, request_metadata, suppressed
        metadata = current_http(self, scope)
        if metadata is None and type(scope) is dict:
            metadata = request_metadata(scope, owned=self._http_bridge)
            if metadata is not None and metadata.route not in (
                    '/app/agent/models', '/app/agent/chat', '/app/agent/chat/stream',
                    '/app/agent/chat/cancel', '/app/agent/chat/heartbeat'):
                metadata = HTTPMetadata(metadata.method)
        if suppressed(self.http_diagnostics_level, metadata):
            yield None
            return
        if not self._authorized(scope):
            yield None
            return
        from api_agent.runtime.tracing_services import tracing_requested
        if self._closed or not tracing_requested(self.config, owner_root=self.owner_root):
            yield None
            return
        from .studio_retained import RetainedStudio
        manual = isinstance(self.provider, RetainedStudio) and self.provider.phase != 'test'
        if manual and not self.provider.ready():
            # The backend owns the ordinary closed-session response/diagnostic.
            # Finalized test traces cannot authorize a manual exporter early.
            yield None
            return
        key = 'manual' if manual else 'automated'
        if key in self._unavailable:
            yield None
            return
        async with self._creation:
            if key not in self._sessions and key not in self._unavailable:
                if manual and 'automated' in self._sessions:
                    automated = self._sessions['automated']
                    try:
                        await automated.aclose()
                    except StudioTraceCleanupPending:
                        if not automated.retirement_reported:
                            automated.retirement_reported = True
                            logging.getLogger(__name__).error('Studio tracing cleanup is incomplete; its resources remain owned for retry.')
                selected = await self.provider._provider() if manual else self.provider
                boundary = (getattr(selected, 'emissions', None) if manual else
                            self.capture_services.get('emissions'))
                boundary = boundary or EmissionBoundary()
                await boundary.aregister_secret(self._http[1])
                try:
                    from api_agent.telemetry.tracing_providers import admit_binding
                    from api_agent.runtime.tracing_context import restore_trace_context
                    trace_context = restore_trace_context(self.collector_snapshot or {},
                                                          required=self.collector_snapshot is not None, config=self.config)
                    binding = admit_binding(self.config, owner_root=self.owner_root, boundary=boundary, **trace_context)
                    if self.collector_snapshot is not None:
                        from api_agent.runtime.tracing_collector_snapshot import verify_collector_binding
                        if self.collector_snapshot.get('trace_collector_plan') == {}:
                            raise ValueError('Captured tracing collector is unavailable')
                        verify_collector_binding(self.collector_snapshot, binding)
                    runtime = self.runtime_factory(self.config, boundary=boundary,
                        **({'binding': binding} if binding is not None else {}))
                    manager = self.collector_factory(self.config.values) if self.collector_factory is not None else None
                except Exception:
                    self._unavailable.add(key)
                    logging.getLogger(__name__).error('Studio tracing setup failed; continuing without tracing.')
                else:
                    self._sessions[key] = _TraceSession(runtime, manager)
        if key in self._unavailable:
            yield None
            return
        session = self._sessions[key]
        await session.enter()
        runtime = session.runtime
        token = self._current.set(runtime)
        services = dict(self.capture_services) if not manual else {}
        services.update(emissions=runtime.boundary,
            observation_factory=WorkflowObservationFactory(self.config, boundary=runtime.boundary, trace=runtime))
        try:
            from api_agent.workflows.run_context import test_evidence
            with test_evidence(not manual), bind_capture_services(services):
                yield runtime
        finally:
            self._current.reset(token)
            await session.leave()

    async def aclose(self):
        self._closed = True
        # SDK disposal is acknowledged before the corresponding collector lease.
        for session in reversed(tuple(self._sessions.values())):
            try:
                await session.aclose()
            except StudioTraceCleanupPending:
                raise
            except BaseException:
                self._cleanup_fatal = True
                raise


class _TraceSession:
    def __init__(self, runtime, manager):
        self.runtime, self.manager = runtime, manager
        self.started = self.unavailable = self.closed = False
        self._startup = asyncio.Lock()
        self._drained = asyncio.Condition()
        self._active = 0
        self.retirement_reported = False
        self._closing = False
        self._cleanup = asyncio.Lock()

    async def enter(self):
        async with self._drained:
            if self._closing or self.closed:
                raise RuntimeError('Studio trace session is closed')
            self._active += 1
        try:
            await self.start()
        except BaseException:
            await self.leave()
            raise

    async def leave(self):
        async with self._drained:
            self._active -= 1
            self._drained.notify_all()

    async def start(self):
        async with self._startup:
            if self.started or self.unavailable or self.closed:
                return
            from api_agent.runtime.tasks import offload
            try:
                if self.manager is not None:
                    from api_agent.providers.progress import component_timeout
                    async with component_timeout('Studio trace collector readiness', self.manager.timeout) as budget:
                        await offload(self.manager.acquire, _deadline=self.manager.now() + budget.remaining)
                await self.runtime.astart()
            except Exception:
                self.unavailable = True
                logging.getLogger(__name__).error('Studio tracing setup failed; continuing without tracing.')
            else:
                self.started = True

    async def aclose(self):
        async with self._cleanup:
            if self.closed:
                return
            async with self._drained:
                self._closing = True
                await self._drained.wait_for(lambda: self._active == 0)
            try:
                await self.runtime.aclose()
            except TraceDeliveryError:
                logging.getLogger(__name__).error(TRACE_DELIVERY_WARNING)
            except Exception:
                if getattr(getattr(self.runtime, 'losses', None), '_diagnostic_failed', False) is True:
                    raise
                raise StudioTraceCleanupPending('Studio tracing disposal is unacknowledged.') from None
            if self.manager is not None:
                from api_agent.runtime.tasks import offload
                try:
                    await offload(self.manager.release, retain=False)
                except Exception:
                    raise StudioTraceCleanupPending('Studio trace collector release is unacknowledged.') from None
            self.closed = True


def capture_collector_plan(context, config, tool_plan=None, *, trace_context=None):
    """Preserve the protected snapshot entry point over the captured helper."""
    from api_agent.runtime.tracing_collector_snapshot import capture_collector_plan as capture
    return capture(context, config, tool_plan, trace_context=trace_context)


def collector_from_snapshot(snapshot, config):
    """Restore only admitted captured inputs through the shared lease manager."""
    if snapshot.get('trace_collector_plan') == {}:
        raise ValueError('Captured tracing collector is unavailable')
    from api_agent.runtime.tracing_collector_snapshot import collector_from_snapshot as restore
    return restore(snapshot, config)
