"""One local application controller shared by terminal and workbench consumers."""
import asyncio
import os
from threading import Event
from secrets import token_urlsafe
from types import MappingProxyType

from api_agent.api.composition import compose_runtime
from api_agent.api.studio import StudioBackend
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout, independent_timeout_scope, report_timeout
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.runtime.local_contracts import LocalComponent, LocalDestination, LocalSnapshot, LocalEvent
from api_agent.runtime.local_llm import LocalLLM, _create_task
from api_agent.runtime.operation import local_operation
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.view_flow import _prepare_view
from api_agent.ui.session import PreparedUISession
from api_agent.runtime.local_tools import selected_application, bind_local_tools


def _plan(config, schema, context, scope, *, engine_environment=None, requires_api=False,
          cancelled=lambda: False):
    """Admit exact consumed declarations without importing provider factories."""
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions
    from api_agent.runtime.infrastructure_plan import MissingEngineTransport, plan_infrastructure
    locations = tuple(config.values[key] for key in ('LLM_PROVIDER', 'UI_PROVIDER', 'API_PROVIDER')
        if config.values.get(key) and (key != 'UI_PROVIDER' or config.values.get('RUN_UI'))
        and (key != 'API_PROVIDER' or requires_api))
    contributions = ()
    if getattr(context, 'infrastructure_paths', ()) or locations:
        contributions = load_infrastructure_contributions(context, selected_provider_locations=locations)
    tool_plan = getattr(config, 'tool_plan', None)
    if tool_plan is not None and tool_plan.tools:
        from api_agent.tooling.services import selected_contributions
        merged = {item.id: item for item in contributions}
        for item in selected_contributions(tool_plan):
            if item.id in merged and merged[item.id] != item:
                raise ValueError('Conflicting selected infrastructure identity')
            merged[item.id] = item
        contributions = tuple(merged.values())
    from api_agent.runtime.inventory import capture_inventory
    inventory = capture_inventory(context, tool_plan)
    if contributions:
        if schema is None:
            raise ValueError('Selected infrastructure requires the resolved configuration schema.')
        try:
            return plan_infrastructure(config, schema, contributions, scope,
                                       engine_environment=engine_environment, inventory=inventory)
        except MissingEngineTransport:
            from api_agent.runtime.reporting_engine import verified_engine_binding
            _, transport = verified_engine_binding(engine_environment, cancelled=cancelled)
            pinned = {**dict(engine_environment or {}), 'DOCKER_HOST': transport}
            return plan_infrastructure(config, schema, contributions, scope,
                                       engine_environment=pinned, inventory=inventory)
    from dataclasses import replace
    return replace(plan_compose(config.values, scope, engine_environment=engine_environment), inventory=inventory)


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
    """Creating-loop owner; an optional interaction is transferred, never borrowed.

    Callers sharing an interaction root must supply a dedicated operation fork.
    """
    CLEANUP_ATTEMPT_SECONDS = 15
    CLEANUP_RETRY_SECONDS = 1
    _STARTUP_STAGES = frozenset({
        'configuration', 'view preparation', 'application construction',
        'infrastructure planning', 'dependencies', 'local HTTPS', 'operation plan',
        'prerequisites', 'runtime', 'listener', 'listener ready', 'tracing', 'browser',
    })

    def __init__(self, context, resolve, *, argv=(), on_event=None,
                 schema=None, engine_environment=None, plan_factory=None, operation_factory=local_operation, browser_factory=None,
                 prepare_view=_prepare_view, application_factory=selected_application,
                 llm_factory=LocalLLM, interaction_owner=None):
        self.context, self.resolve, self.argv = context, resolve, tuple(argv)
        self.on_event = on_event
        self._engine_environment_source = engine_environment
        self._engine_environment = None
        self.schema, self.plan_factory = schema, plan_factory or _plan
        self._available_sources = ('agent',)
        self._planned_sources = {}
        self._planned_components = ()
        self.operation_factory, self.browser_factory = operation_factory, browser_factory
        self.prepare_view, self.application_factory = prepare_view, application_factory
        self.llm_factory = llm_factory
        self.interaction_owner = interaction_owner
        self._interaction_cancel = None
        self._interaction_close = None
        self.snapshot = LocalSnapshot('idle')
        self.pending_cleanup = None
        self._stack = LocalCleanup()
        if interaction_owner is not None:
            self._stack.push(self._close_interaction)
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
        self._cleanup_warnings_published = set()
        self._diagnostic_projection = None
        self._requires_api = False
        self._startup_failure_reported = False

    def _publish(self, phase, *, components=(), destinations=(), inspection=None, activity=None):
        self.snapshot = LocalSnapshot(phase, tuple(components), tuple(destinations), self._available_sources,
            self._prepared[0].definition_id if self._prepared else None, inspection, activity)
        self._sequence += 1
        self._notify(LocalEvent('status', snapshot=self.snapshot, sequence=self._sequence))

    def _notify(self, event):
        """A presentation observer cannot take ownership of startup or cleanup."""
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _status(self):
        return {'components': {item.name: item.state for item in self.snapshot.components},
                'destinations': {item.name: item.url for item in self.snapshot.destinations}}

    def _startup_stage(self, name):
        """Publish only fixed local stage names, never provider or config values."""
        if not isinstance(name, str) or name not in self._STARTUP_STAGES:
            raise ValueError('invalid_local_startup_stage')
        self._publish('starting', components=self.snapshot.components, activity=name)
        self._log('agent', 'Local startup: ' + name + '.\n')

    def request_stop(self):
        self._stop.set()
        if self.interaction_owner is not None and (self._interaction_cancel is None
                or self._interaction_cancel.cancelled()
                or self._interaction_cancel.done() and self._interaction_cancel.exception() is not None):
            self._interaction_cancel = _create_task(asyncio.to_thread(self.interaction_owner.cancel))
        if self._starting is not None and self._starting is not asyncio.current_task() and not self._starting.done():
            if not self._starting.cancelling():
                self._starting.cancel()

    async def _close_interaction(self):
        from api_agent.runtime.tasks import settle
        if self._interaction_cancel is not None:
            await settle(self._interaction_cancel)
        if self._interaction_close is None or self._interaction_close.cancelled() or (self._interaction_close.done()
                and self._interaction_close.exception() is not None):
            self._interaction_close = _create_task(asyncio.to_thread(self.interaction_owner.close))
        await settle(self._interaction_close)

    def _runtime(self, config, origin, material):
        from api_agent.ui.process import StudioProcesses
        from api_agent.runtime.client_tls import verified_client_context
        runtime = compose_runtime(self.context, config, callback_origin=origin,
            tls_material=material, required_kinds=())
        self._emissions = runtime.emissions
        tool_plan = getattr(config, 'tool_plan', None)
        if tool_plan is not None and tool_plan.tools:
            runtime.tool_plan = tool_plan
            self._graph.tools = runtime.tools_for(self._graph.definition)
        elif self._requires_api:
            bind_local_tools(runtime, self._graph, config)
        options = ({'interaction_owner': self.interaction_owner}
                   if self.interaction_owner is not None else {})
        self._llm = self.llm_factory(config.values, self.context.owner_root, emissions=runtime.emissions,
                                     on_status=self._workflow_event, trace=runtime.tracing, **options)
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
            on_error=self._execution_error,
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
                self._publish('starting', components=(LocalComponent('agent', 'starting'),))
                self._startup_stage('configuration')
                await asyncio.sleep(0)
                self._config = self.resolve()
                if self._config.errors or self._config.missing:
                    raise ValueError('invalid_runtime_configuration')
                if self.prepare_view is _prepare_view:
                    from api_agent.runtime.frontend import prepare_source_frontend
                    await prepare_source_frontend()
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
                self._startup_stage('view preparation')
                await asyncio.sleep(0)
                from api_agent.runtime.tasks import offload
                self._prepared = await offload(self.prepare_view, self.context)
                self._startup_stage('application construction')
                await asyncio.sleep(0)
                self._graph = self.application_factory(self._prepared[0], config=self._config.values)
                selected_tools = getattr(self._config, 'tool_plan', None)
                self._requires_api = (not (selected_tools is not None and selected_tools.tools)
                    and getattr(self._graph, 'requires_api', False) is True)
                bundled_demo = (getattr(self.context, 'pack_id', None) == 'demo'
                    and not getattr(self.context, 'external_pack', False))
                if selected_tools is not None and selected_tools.tools:
                    from api_agent.tooling.services import tool_operation_values
                    self._config = tool_operation_values(selected_tools, self._config)
                elif bundled_demo:
                    from api_agent.runtime.demo_api import prepare_demo_api
                    self._config, _ = prepare_demo_api(self._config, self.context,
                        requires_api=self._requires_api)
                scope = StackScope.from_roots(self.context.base_root, self.context.pack_root)
                plan_options = {'engine_environment': self._engine_environment}
                if self._requires_api:
                    plan_options['requires_api'] = True
                plan_cancelled = Event()
                if self.plan_factory is _plan:
                    plan_options['cancelled'] = plan_cancelled.is_set
                self._startup_stage('infrastructure planning')
                await asyncio.sleep(0)
                plan = await offload(self.plan_factory, self._config, self.schema, self.context, scope,
                                     cancellation=plan_cancelled, **plan_options)
                plan_environment = getattr(plan, 'environment', None)
                if plan_environment is not None:
                    selected = {key: plan_environment[key] for key in ENGINE_ENVIRONMENT_KEYS
                                if key in plan_environment}
                    self._engine_environment = MappingProxyType(selected)
                self._planned_sources = {item.compose_name: item.id for item in plan.conditional}
                self._available_sources = ('agent', *(('ui',) if self._config.values.get('RUN_UI') else ()),
                    *(self._planned_sources.get(service, 'api-agent/' + service) for service in plan.services))
                source = self._config.values.get('RUN_LOG_SOURCE', 'agent')
                if source != 'all' and source not in self._available_sources:
                    raise ValueError('Unknown RUN_LOG_SOURCE; choose all or an available named local source.')
                self._planned_components = tuple(dict.fromkeys((
                    'agent', 'api', *(('ui',) if self._config.values.get('RUN_UI') else ()),
                    *('service:' + self._planned_sources.get(service, service) for service in plan.services),
                    *('profile:' + profile for profile in getattr(plan, 'profiles', ())))))
                self._publish('starting', components=tuple(
                    LocalComponent(name, 'starting') for name in self._planned_components))
                from api_agent.runtime.local_status import LocalStatusApplication
                operation_options = {}
                if selected_tools is not None and selected_tools.tools:
                    from dataclasses import replace
                    from api_agent.tooling.services import bind_tool_outputs
                    def bind_tools(config, infrastructure):
                        return replace(config, tool_plan=bind_tool_outputs(config.tool_plan, infrastructure))
                    operation_options['bind_infrastructure'] = bind_tools
                if self._requires_api and bundled_demo:
                    from api_agent.runtime.demo_api import bind_demo_api_endpoint
                    operation_options['bind_infrastructure'] = bind_demo_api_endpoint
                required_kinds = {'llm', 'ui'} if self._config.values.get('RUN_UI') else {'llm'}
                if self._requires_api:
                    required_kinds.add('api')
                self._operation = self.operation_factory(self.context, resolve=lambda: self._config,
                    argv=self.argv, scope=scope, groups=('browser',),
                    ownership_directory=self.context.owner_root / 'artifacts/prerequisites',
                    required_kinds=required_kinds,
                    runtime_factory=self._runtime,
                    application_wrapper=lambda app: LocalStatusApplication(app, self._status),
                    prepared_plan=plan, engine_environment=self._engine_environment,
                    on_stage=self._startup_stage, on_failure=self._report_startup_failure, **operation_options)
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
                self._startup_stage('tracing')
                self._tracing_dashboard = LocalTracing(self._config.values,
                    emissions=self._session.runtime.emissions)
                self._stack.push(self._tracing_dashboard.aclose)
                await self._tracing_dashboard.start()
                if self._tracing_dashboard.diagnostic:
                    self._log('agent', self._tracing_dashboard.diagnostic.rstrip('\n') + '\n')
                from api_agent.runtime.local_browser import LocalBrowserOwner
                factory = self.browser_factory or LocalBrowserOwner
                home = self._session.callback.origin + '/'
                self._browser = factory(self.context, self._config.values, home,
                    prepare_ui=self._ui, prepare_view=lambda context: self._prepared,
                    tracing=self._tracing_dashboard)
                self._stack.push(self._browser.aclose)
                self._startup_stage('browser')
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
                self._log('agent', 'Local application is ready.\n')
                return self.snapshot
        except BaseException as primary:
            self._starting = None
            self._start_done.set()
            self._report_startup_failure(primary)
            try:
                self._publish('failed', components=(LocalComponent('agent', 'unavailable'),))
            except BaseException:
                # Reporting cannot prevent disposal of an admitted owner.
                pass
            try:
                await self.aclose()
            except BaseException as cleanup_error:
                pending = self.pending_cleanup or getattr(cleanup_error, 'cleanup_pending', None)
                if pending is None and self._stack.callbacks:
                    pending = CleanupPendingError((self,), self.aclose)
                if pending is not None:
                    primary.cleanup_pending = pending
            raise

    def _report_startup_failure(self, primary):
        if self._startup_failure_reported:
            return
        self._startup_failure_reported = True
        try:
            from api_agent.runtime.listener import ListenerError
            stage = self.snapshot.activity or 'startup'
            diagnostic = primary.diagnostic() if type(primary) is ListenerError else None
            if diagnostic is not None:
                detail = diagnostic
            elif isinstance(primary, asyncio.CancelledError):
                detail = 'Startup was cancelled.'
            elif isinstance(primary, TimeoutError):
                detail = 'Startup timed out; check local prerequisites.'
            elif isinstance(primary, ValueError):
                detail = 'Startup input or configuration was rejected; check local prerequisites.'
            else:
                detail = 'Startup encountered an error; check configuration and prerequisites.'
            self._log('agent', f'Local startup failed during {stage}: {detail}\n')
            self._execution_error(primary)
        except BaseException:
            # Diagnostic delivery never owns startup failure or cleanup.
            pass

    def _safe_text(self, text, commit=None):
        from api_agent.testing.diagnostics import CredentialScrubber
        values = dict(self._config.values) if self._config is not None else {}
        boundary = self._emissions
        if boundary is None and self._session is not None:
            boundary = self._session.runtime.emissions
        for _ in range(3):
            generation, snapshot = boundary.snapshot_content() if boundary is not None else (None, None)
            safe = CredentialScrubber(values, boundary=snapshot)(text)
            safe = ''.join(character if character.isprintable() or character in '\n\t'
                           else f'\\u{ord(character):04x}' for character in safe)
            publish = lambda: commit(safe) if commit is not None else None
            if boundary is None:
                publish()
                return safe
            if boundary.commit_if_current(generation, publish):
                return safe
        # Repeated concurrent registrations make the content unsafe to publish.
        safe = '[diagnostic withheld]'
        if commit is not None:
            commit(safe)
        return safe

    def diagnostic(self, error):
        """Preserve the actionable cause without exposing owned credentials."""
        return self._safe_text(str(error))

    def _execution_error(self, error):
        """Execution failures are credential-scrubbed even without LOG_CONTENT."""
        from api_agent.testing.diagnostics import format_exception
        self._log('agent', format_exception(error, 'Local execution failed').rstrip('\n') + '\n')

    def interactive_dashboard_credentials(self):
        """Literal terminal status only; excluded from snapshots and events."""
        return (self._tracing_dashboard.interactive_credentials()
                if self._tracing_dashboard is not None else None)

    def _workflow_event(self, row):
        """Consume only the source's admitted log projection, never trace data."""
        if row.get('event') == 'configuration-required':
            from api_agent.providers.contracts import ConfigurationRequiredError
            fields = ConfigurationRequiredError(row.get('fields')).fields
            self._notify(LocalEvent('configuration-required', required_fields=fields))
            return
        if self._diagnostic_projection is None:
            return
        text = self._diagnostic_projection.format(row)
        self._sequence += 1
        self._notify(LocalEvent('diagnostic', source='agent', text=text,
                                diagnostic=row, sequence=self._sequence))

    def _log(self, source, text):
        # Commit the scrubbed projection under the same authority generation;
        # callbacks run only after releasing its lock.
        from hashlib import sha256
        fingerprint = sha256(text.encode(errors='surrogatepass')).hexdigest()
        event = []
        def commit(safe):
            safe = safe[-16384:]
            self._log_fingerprints[source] = fingerprint
            self._logs[source] = safe
            self._sequence += 1
            event.append(LocalEvent('log', source=source, text=safe, sequence=self._sequence))
        self._safe_text(text, commit)
        self._notify(event[0])

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
            except (Exception, asyncio.CancelledError):
                # settle only raises after the collector has finished. A finished
                # collector owns no live resource and cannot block later cleanup.
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
            await self.finish_cleanup()
        return status

    async def _close(self):
        self._closed = True
        publication_error = None
        try:
            if self.snapshot.phase != 'cleanup-pending':
                self._publish('stopping', components=tuple(
                    LocalComponent(name, 'cleanup scope') for name in self._planned_components))
        except BaseException as error:
            publication_error = error
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
        runtime = getattr(self._session, 'runtime', None)
        for warning in getattr(runtime, 'cleanup_warnings', ()):
            if warning not in self._cleanup_warnings_published:
                self._cleanup_warnings_published.add(warning)
                self._log('agent', warning + '\n')
        try:
            self._publish('stopped')
        except BaseException:
            if publication_error is None:
                raise
        if publication_error is not None:
            raise publication_error

    async def aclose(self):
        self.request_stop()
        if self._closing is None or self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None):
            closing = self._close()
            try:
                self._closing = asyncio.create_task(closing)
            except BaseException as error:
                closing.close()
                self.pending_cleanup = CleanupPendingError((self,), self.aclose)
                error.cleanup_pending = self.pending_cleanup
                raise
        closing = self._closing
        cancelled, pending = False, None
        try:
            with independent_timeout_scope():
                async with report_timeout('Local cleanup attempt', self.CLEANUP_ATTEMPT_SECONDS) as budget:
                    while not closing.done():
                        if budget.remaining <= 0:
                            pending = CleanupPendingError((self,), self.aclose)
                            self.pending_cleanup = pending
                            try:
                                self._publish_cleanup_pending()
                            except BaseException:
                                pass
                            raise pending
                        try:
                            await asyncio.wait({closing}, timeout=budget.remaining)
                        except asyncio.CancelledError:
                            cancelled = True
        except CleanupPendingError as error:
            # Context teardown can yield after this attempt reports pending.
            # Only the same acknowledged task can settle that exact receipt.
            if (error is not pending or closing is not self._closing or not closing.done()
                    or closing.cancelled() or self.pending_cleanup is not None):
                raise
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    def _publish_cleanup_pending(self):
        if self.snapshot.phase != 'cleanup-pending':
            self._publish('cleanup-pending', components=tuple(
                LocalComponent(name, 'cleanup scope') for name in self._planned_components))

    async def finish_cleanup(self):
        """Keep the creating loop alive until retained disposal is acknowledged.

        Each attempt is finite; the lifetime of an uncooperative owner is not.
        Retry observes the existing close task instead of invoking it again.
        """
        while True:
            try:
                await self.aclose()
                return
            except asyncio.CancelledError:
                if self.pending_cleanup is None:
                    raise
            except Exception:
                if self.pending_cleanup is None:
                    raise
            try:
                self._publish_cleanup_pending()
            except BaseException:
                # A retained cleanup receipt outlives presentation failure.
                pass
            try:
                await asyncio.sleep(self.CLEANUP_RETRY_SECONDS)
            except asyncio.CancelledError:
                # Repeated stop requests do not discard retained same-loop work.
                pass
