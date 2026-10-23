"""One local application controller shared by terminal and workbench consumers."""
import asyncio
import os
from copy import deepcopy
from secrets import token_urlsafe
from types import MappingProxyType

from api_agent.api.composition import compose_runtime
from api_agent.api.studio import StudioBackend
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout, independent_timeout_scope
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.runtime.local_contracts import LocalComponent, LocalDestination, LocalSnapshot, LocalEvent
from api_agent.runtime.local_llm import LocalLLM
from api_agent.runtime.operation import local_operation
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.view_flow import _prepare_view
from api_agent.ui.session import PreparedUISession
from api_agent.workflows.local_application import LocalChatApplication


def _plan(config, schema, context, scope, *, engine_environment=None):
    """Admit exact consumed declarations without importing provider factories."""
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions
    from api_agent.runtime.infrastructure_plan import plan_infrastructure
    locations = tuple(config.values[key] for key in ('LLM_PROVIDER', 'UI_PROVIDER')
        if config.values.get(key) and (key != 'UI_PROVIDER' or config.values.get('RUN_UI')))
    contributions = ()
    if getattr(context, 'infrastructure_paths', ()) or locations:
        contributions = load_infrastructure_contributions(context, selected_provider_locations=locations)
    if contributions:
        if schema is None:
            raise ValueError('Selected infrastructure requires the resolved configuration schema.')
        return plan_infrastructure(config, schema, contributions, scope, engine_environment=engine_environment)
    return plan_compose(config.values, scope, engine_environment=engine_environment)


class LocalCleanup:
    """Retain an unsuccessful callback and all of its later dependencies."""
    def __init__(self):
        self.callbacks = []
        self.pending = None

    def push(self, callback):
        self.callbacks.insert(0, callback)

    async def aclose(self):
        while self.callbacks:
            try:
                if self.pending is not None:
                    await self.pending.retry()
                    self.pending = None
                await self.callbacks[0]()
            except BaseException as error:
                self.pending = error if isinstance(error, CleanupPendingError) else getattr(error, 'cleanup_pending', None)
                raise
            self.callbacks.pop(0)


class LocalRunOwner:
    """Creating-loop owner; consumers observe state and request cooperative stop."""
    def __init__(self, context, resolve, *, argv=(), on_event=None,
                 schema=None, engine_environment=None, plan_factory=None, operation_factory=local_operation, browser_factory=None,
                 prepare_view=_prepare_view, application_factory=LocalChatApplication,
                 llm_factory=LocalLLM):
        self.context, self.resolve, self.argv = context, resolve, tuple(argv)
        self.on_event = on_event
        self._engine_environment_source = engine_environment
        self._engine_environment = None
        self.schema, self.plan_factory = schema, plan_factory or _plan
        self._available_sources = ('agent',)
        self._planned_sources = {}
        self.operation_factory, self.browser_factory = operation_factory, browser_factory
        self.prepare_view, self.application_factory = prepare_view, application_factory
        self.llm_factory = llm_factory
        self.snapshot = LocalSnapshot('idle')
        self.pending_cleanup = None
        self._stack = LocalCleanup()
        self._stop = asyncio.Event()
        self._start_done = asyncio.Event()
        self._start_done.set()
        self._started = False
        self._closed = False
        self._closing = None
        self._sequence = 0
        self._starting = None
        self._log_task = None
        self._logs = {}
        self._log_fingerprints = {}
        self._infra_sources = {}
        self._config = self._prepared = self._graph = self._operation = self._browser = self._llm = None
        self._session = None
        self._emissions = None
        self._tracing_dashboard = None
        self._diagnostic_projection = None

    def _publish(self, phase, *, components=(), destinations=(), inspection=None):
        self.snapshot = LocalSnapshot(phase, tuple(components), tuple(destinations), self._available_sources,
            self._prepared[0].definition_id if self._prepared else None, inspection)
        self._sequence += 1
        if self.on_event is not None:
            self.on_event(LocalEvent('status', snapshot=self.snapshot, sequence=self._sequence))

    def _status(self):
        return {'components': {item.name: item.state for item in self.snapshot.components},
                'destinations': {item.name: item.url for item in self.snapshot.destinations}}

    def request_stop(self):
        self._stop.set()
        if self._starting is not None and self._starting is not asyncio.current_task() and not self._starting.done():
            if not self._starting.cancelling():
                self._starting.cancel()

    def _runtime(self, config, origin, material):
        from api_agent.ui.process import StudioProcesses
        from api_agent.runtime.client_tls import verified_client_context
        runtime = compose_runtime(self.context, config, callback_origin=origin,
            tls_material=material, required_kinds=())
        self._emissions = runtime.emissions
        self._llm = self.llm_factory(config.values, self.context.owner_root, emissions=runtime.emissions,
                                     on_status=self._workflow_event, trace=runtime.tracing)
        from api_agent.workflows.observations import WorkflowObservationFactory
        self._graph.observations = WorkflowObservationFactory(config, boundary=runtime.emissions,
            sink=self._workflow_event, trace=runtime.tracing)
        async def describe(*, auth):
            return await self._llm.describe()
        async def complete(request, *, auth, context):
            return await self._graph.complete(request, llm=self._llm, context=context)
        nonce = token_urlsafe(32)
        backend = StudioBackend(owner=self._llm, describe=describe, complete=complete,
            policy=ModelSelectionPolicy(config.values),
            origin=config.values.get('UI_LISTENER_URL', 'https://localhost:9443').rstrip('/'),
            nonce=nonce, scrub=lambda message: 'Selected provider rejected the request.',
            reasoning_default=config.values.get('LLM_REASONING_EFFORT') or None)
        services = {**runtime.services, 'studio_backend': backend, 'ui_access_token': nonce,
                    'ensure_llm_ready': self._llm.ensure_ready,
                    'ui_processes': StudioProcesses(ssl_context=verified_client_context(str(material.ca_file)),
                        log_callback=lambda text: self._log('ui', text), emissions=runtime.emissions)}
        runtime.emissions.register_secret(nonce)
        runtime.services = MappingProxyType(services)
        return runtime

    async def _ui(self, *, browser_owner):
        from api_agent.auth.bindings import bind_role_credentials
        from api_agent.auth.managers import registry_for_owner
        values, runtime = self._config.values, self._session.runtime
        index = values.get('UNATTENDED_USER_INDEX')
        def credentials_for(roles, selected):
            return bind_role_credentials(values, roles, selected,
                registry=registry_for_owner(self.context.owner_root), emissions=runtime.emissions,
                pending_tasks=self._llm._helpers)
        owner = PreparedUISession(values, runtime.services, browser_owner=browser_owner,
            user_index=index, credentials_for=credentials_for)
        # Register before preparation so partial setup remains owned.
        self._stack.push(owner.aclose)
        await owner.start()
        await self._llm.ensure_ready()
        return owner

    async def start(self):
        if self._started or self._closed or self._stop.is_set():
            raise RuntimeError('local_operation_already_started')
        self._started = True
        self._starting = asyncio.current_task()
        self._start_done.clear()
        try:
            async with component_timeout('Local application preparation', 120):
                self._config = self.resolve()
                if self._config.errors or self._config.missing:
                    raise ValueError('invalid_runtime_configuration')
                from api_agent.runtime.local_diagnostics import LocalDiagnosticProjection
                self._diagnostic_projection = LocalDiagnosticProjection(
                    full_content=self._config.values.get('RUN_LOG_FULL_CONTENT') is True)
                from api_agent.runtime.compose import ENGINE_ENVIRONMENT_KEYS, controlled_engine_environment
                source = self._engine_environment_source
                if source is None:
                    source = {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
                selected = controlled_engine_environment(source)
                if 'CONTAINER_HOST' in selected and 'DOCKER_HOST' not in selected:
                    # The default Docker-compatible command consumes DOCKER_HOST.
                    # Translate the explicitly selected endpoint without guessing.
                    selected['DOCKER_HOST'] = selected['CONTAINER_HOST']
                self._engine_environment = MappingProxyType(selected)
                self._prepared = self.prepare_view(self.context)
                self._graph = self.application_factory(self._prepared[0], config=self._config.values)
                scope = StackScope.from_roots(self.context.base_root, self.context.pack_root)
                plan = self.plan_factory(self._config, self.schema, self.context, scope,
                                         engine_environment=self._engine_environment)
                self._planned_sources = {item.compose_name: item.id for item in plan.conditional}
                self._available_sources = ('agent', *(('ui',) if self._config.values.get('RUN_UI') else ()),
                    *(self._planned_sources.get(service, 'api-agent/' + service) for service in plan.services))
                source = self._config.values.get('RUN_LOG_SOURCE', 'agent')
                if source != 'all' and source not in self._available_sources:
                    raise ValueError('Unknown RUN_LOG_SOURCE; choose all or an available named local source.')
                self._publish('starting', components=(LocalComponent('agent', 'starting'),))
                from api_agent.runtime.local_status import LocalStatusApplication
                self._operation = self.operation_factory(self.context, resolve=lambda: self._config,
                    argv=self.argv, scope=scope, groups=('browser',),
                    ownership_directory=self.context.owner_root / 'artifacts/prerequisites',
                    required_kinds={'llm', 'ui'} if self._config.values.get('RUN_UI') else {'llm'},
                    runtime_factory=self._runtime,
                    application_wrapper=lambda app: LocalStatusApplication(app, self._status),
                    prepared_plan=plan, engine_environment=self._engine_environment)
                opening = self._operation.open(retain=False)
                exited = False
                async def close_operation():
                    nonlocal exited
                    pending = self._operation.pending_cleanup
                    if pending is not None:
                        await pending.retry()
                    if not exited:
                        exited = True
                        await opening.__aexit__(None, None, None)
                # Its context owns partial startup cleanup even if entering fails.
                self._stack.push(close_operation)
                try:
                    self._session = await opening.__aenter__()
                except BaseException:
                    exited = True
                    raise
                from api_agent.runtime.local_tracing import LocalTracing
                self._tracing_dashboard = LocalTracing(self._config.values,
                    emissions=self._session.runtime.emissions)
                self._stack.push(self._tracing_dashboard.aclose)
                await self._tracing_dashboard.start()
                if self._tracing_dashboard.diagnostic:
                    self._log('agent', self._tracing_dashboard.diagnostic)
                from api_agent.runtime.local_browser import LocalBrowserOwner
                factory = self.browser_factory or LocalBrowserOwner
                home = self._session.callback.origin + '/'
                self._browser = factory(self.context, self._config.values, home,
                    prepare_ui=self._ui, prepare_view=lambda context: self._prepared,
                    tracing=self._tracing_dashboard)
                self._stack.push(self._browser.aclose)
                await self._browser.start()
                if self._stop.is_set():
                    raise asyncio.CancelledError
                destinations = [LocalDestination('agent', home), LocalDestination('flow', self._browser.flow_url)]
                components = [LocalComponent('agent', 'ready'), LocalComponent('tracing', self._tracing_dashboard.state),
                              LocalComponent('flow', 'ready'), LocalComponent('ui', 'disabled')]
                if self._tracing_dashboard.viewer_url is not None:
                    destinations.insert(1, LocalDestination('tracing', self._tracing_dashboard.viewer_url))
                if self._config.values.get('RUN_UI'):
                    components[-1] = LocalComponent('ui', 'ready')
                    destinations.append(LocalDestination('ui', self._config.values['UI_LISTENER_URL']))
                infrastructure = self._session.infrastructure
                if infrastructure is not None:
                    from api_agent.runtime.tasks import offload
                    inventory = await offload(infrastructure.status)
                    self._infra_sources = {self._planned_sources.get(item.service, 'api-agent/' + item.service): item.id for item in inventory}
                    components.extend(LocalComponent(
                        'service:' + self._planned_sources.get(item.service, 'api-agent/' + item.service),
                        'unavailable' if not item.running else 'degraded' if item.health == 'unhealthy' else 'ready')
                        for item in inventory)
                    from api_agent.runtime.local_status import safe_destination
                    for service, endpoints in infrastructure.endpoints.items():
                        for endpoint in endpoints:
                            if safe_destination(endpoint.host):
                                destinations.append(LocalDestination('service:' + service + ':' + endpoint.name, endpoint.host))
                    with independent_timeout_scope():
                        self._log_task = asyncio.create_task(self._capture_logs())
                    self._stack.push(self._close_logs)
                session = self._browser.session
                self._publish('ready', components=components, destinations=destinations,
                    inspection=session.inspection_endpoint if session is not None else None)
                self._starting = None
                self._start_done.set()
                self._log('agent', 'Local application is ready.')
                return self.snapshot
        except BaseException as primary:
            self._starting = None
            self._start_done.set()
            self._publish('failed', components=(LocalComponent('agent', 'unavailable'),))
            try:
                await self.aclose()
            except BaseException:
                primary.cleanup_pending = self.pending_cleanup or CleanupPendingError((self,), self.aclose)
            raise

    def _safe_text(self, text):
        from api_agent.testing.diagnostics import CredentialScrubber
        values = dict(self._config.values) if self._config is not None else {}
        boundary = self._emissions
        if boundary is None and self._session is not None:
            boundary = self._session.runtime.emissions
        safe = CredentialScrubber(values, boundary=deepcopy(boundary))(text)
        return ''.join(character if character.isprintable() or character in '\n\t'
                       else f'\\u{ord(character):04x}' for character in safe)

    def diagnostic(self, error):
        """Preserve the actionable cause without exposing owned credentials."""
        return self._safe_text(str(error))

    def interactive_dashboard_credentials(self):
        """Literal interactive status only; excluded from snapshots and events."""
        return (self._tracing_dashboard.interactive_credentials()
                if self._tracing_dashboard is not None else None)

    def _workflow_event(self, row):
        """Consume only the source's admitted log projection, never trace data."""
        if self._diagnostic_projection is None:
            return
        text = self._diagnostic_projection.format(row)
        self._sequence += 1
        if self.on_event is not None:
            self.on_event(LocalEvent('diagnostic', source='agent', text=text,
                                    diagnostic=row, sequence=self._sequence))

    def _log(self, source, text):
        # Scrub a complete snapshot before truncation or terminal escaping.
        safe = self._safe_text(text)[-16384:]
        from hashlib import sha256
        self._log_fingerprints[source] = sha256(text.encode()).hexdigest()
        self._logs[source] = safe
        self._sequence += 1
        if self.on_event is not None:
            self.on_event(LocalEvent('log', source=source, text=safe, sequence=self._sequence))

    async def _capture_logs(self):
        from api_agent.runtime.tasks import offload
        while not self._stop.is_set():
            for source, identifier in self._infra_sources.items():
                try:
                    text = await offload(self._session.infrastructure.logs, identifier,
                                         tail=100, output_limit=16384)
                except Exception:
                    text = 'Service logs are unavailable.'
                from hashlib import sha256
                if sha256(text.encode()).hexdigest() != self._log_fingerprints.get(source):
                    self._log(source, text)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _close_logs(self):
        if self._log_task is not None:
            from api_agent.runtime.tasks import settle
            try:
                await settle(self._log_task)
            except Exception:
                # A finished collector owns no live resource; its failure must
                # not prevent the listener or prerequisite lease from stopping.
                pass

    async def wait(self):
        if not self._started:
            raise RuntimeError('local_operation_not_started')
        status = 0
        try:
            async with component_timeout('Local application lifetime', None):
                while not self._stop.is_set():
                    session = self._browser.session if self._browser is not None else None
                    listener = self._session.listener if self._session is not None else None
                    task = getattr(listener, 'task', None)
                    if task is not None and task.done():
                        status = 3
                        break
                    if session is not None and not session.browser.is_connected():
                        break
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=.1)
                    except TimeoutError:
                        pass
        finally:
            await self.aclose()
        return status

    async def _close(self):
        self._closed = True
        with independent_timeout_scope():
            async with component_timeout('Local application cleanup', None):
                try:
                    async with component_timeout('Local startup settlement', 15):
                        await self._start_done.wait()
                    await self._stack.aclose()
                    pending = getattr(self._operation, 'pending_cleanup', None)
                    if pending is not None:
                        await pending.retry()
                except BaseException as error:
                    self.pending_cleanup = CleanupPendingError((self,), self.aclose)
                    if isinstance(error, asyncio.CancelledError):
                        error.cleanup_pending = self.pending_cleanup
                        raise
                    raise self.pending_cleanup from None
        self.pending_cleanup = None
        self._publish('stopped')

    async def aclose(self):
        self.request_stop()
        if self._closing is None or self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None):
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)
