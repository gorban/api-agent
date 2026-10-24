"""Bounded ownership of one local API operation and its prerequisites."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import sys
from threading import Event
from urllib.parse import urlsplit

from api_agent.providers.control import CleanupPendingError

from .tasks import settle as _settle, offload as _offload


async def retry_owned_cleanup(owner, pending):
    """Keep the caller's receipt through disposal and outer acknowledgement.

    A disposal callback can clear the owner's handle before a retained suite
    lease finishes releasing. Restore that handle if acknowledgement fails or
    is interrupted, without replacing a distinct newly assigned cleanup duty.
    Once acknowledged, retire only this receipt so the owner can be reused.
    """
    try:
        await pending.retry()
    except BaseException:
        if owner.pending_cleanup is None:
            owner.pending_cleanup = pending
        raise
    else:
        if owner.pending_cleanup is pending:
            owner.pending_cleanup = None


@dataclass(frozen=True)
class CallbackEndpoint:
    origin: str
    hostname: str
    port: int


def callback_endpoint(config):
    """Use the independently configured HTTPS listener authority."""
    raw = config.values.get('AGENT_DJANGO_URL', '')
    if not isinstance(raw, str):
        raise ValueError('invalid_callback_uri')
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ValueError('invalid_callback_uri') from error
    if (parsed.scheme != 'https' or not parsed.hostname or port == 0
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('invalid_callback_uri')
    port = port or 443
    authority = parsed.netloc
    return CallbackEndpoint(f'https://{authority}', parsed.hostname, port)


@dataclass(frozen=True)
class OperationSession:
    config: object
    tls: object
    lease: object
    runtime: object
    application: object
    listener: object
    callback: CallbackEndpoint
    infrastructure: object = None


class OperationCleanup:
    """Retain listener, runtime and prerequisite lease until confirmed disposal."""
    def __init__(self, manager, runtime, listener, lease, retain, timeout, complete=lambda: None):
        self.manager, self.runtime, self.listener = manager, runtime, listener
        self.lease, self.retain, self.timeout = lease, retain, timeout
        self.done = set()
        self.tasks = {}
        self.task = None
        self.complete = complete

    async def _run(self):
        pending = []
        # A live listener can still invoke runtime owners, so retains that subtree.
        for name, owner, method in [('listener', self.listener, 'stop'),
                                     ('runtime', self.runtime, 'aclose')]:
            if owner is None or name in self.done:
                continue
            if pending:
                pending.append(owner)
                continue
            if name not in self.tasks:
                self.tasks[name] = asyncio.create_task(getattr(owner, method)())
            task = self.tasks[name]
            finished, _ = await asyncio.wait({task}, timeout=self.timeout)
            if not finished:
                pending.append(owner)
                continue
            try:
                task.result()
            except BaseException:
                del self.tasks[name]
                pending.append(owner)
            else:
                self.done.add(name)
        if pending:
            raise CleanupPendingError(tuple(pending) + ((self.lease,) if self.lease is not None else ()), self.retry)
        if self.lease is not None and 'lease' not in self.done:
            try:
                await _offload(self.manager.release, retain=self.retain)
            except BaseException:
                raise CleanupPendingError((self.lease,), self.retry) from None
            self.done.add('lease')
        self.complete()

    async def retry(self):
        if self.task is None or self.task.done() and self.task.exception() is not None:
            self.task = asyncio.create_task(self._run())
        cancellations = asyncio.current_task().cancelling()
        try:
            await _settle(self.task)
        except CleanupPendingError as pending:
            if asyncio.current_task().cancelling() > cancellations:
                error = asyncio.CancelledError()
                error.cleanup_pending = pending
                raise error
            raise
        if asyncio.current_task().cancelling() > cancellations:
            raise asyncio.CancelledError


class OperationRunner:
    """Inject side effects; creating this coordinator performs no I/O.

    resolve and all preparation functions are synchronous. Listener implements
    async start(application, tls, endpoint), wait_ready(timeout), and stop().
    Runtime implements astart/aclose. Manager implements acquire/release.
    """

    def __init__(self, *, resolve, prepare_dependencies, prepare_https, plan,
                 manager, compose_runtime, create_application, listener,
                 timeout=120, bind_infrastructure=None, on_stage=None):
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        self.resolve = resolve
        self.prepare_dependencies = prepare_dependencies
        self.prepare_https = prepare_https
        self.plan = plan
        self.manager = manager
        self.compose_runtime = compose_runtime
        self.create_application = create_application
        self.listener = listener
        self.bind_infrastructure = bind_infrastructure
        self.on_stage = on_stage
        self.timeout = timeout
        self.pending_cleanup = None
        self._active = False

    @asynccontextmanager
    async def open(self, *, retain=False, validate=None):
        """Own one operation across preparation, execution and cleanup."""
        if self._active:
            raise RuntimeError('operation_already_active')
        if self.pending_cleanup is not None:
            raise self.pending_cleanup
        self._active = True
        try:
            async with self._open_owned(retain=retain, validate=validate) as session:
                yield session
        finally:
            self._active = False

    @asynccontextmanager
    async def _open_owned(self, *, retain, validate):
        """Start in dependency order; release a test lease after the workload."""
        def stage(name):
            if self.on_stage is not None:
                self.on_stage(name)

        stage('configuration')
        config = self.resolve()
        if config.errors or config.missing:
            raise ValueError('invalid_runtime_configuration')
        if validate is not None:
            validate(config)
        endpoint = callback_endpoint(config)
        stage('dependencies')
        await _offload(self.prepare_dependencies, config)
        stage('local HTTPS')
        tls = await _offload(self.prepare_https, config, endpoint)
        stage('operation plan')
        selected_plan = self.plan(config)
        cancelled = Event()
        owned_manager = self.manager(selected_plan, cancelled.is_set)
        lease = None
        runtime = None
        owned_listener = None
        try:
            stage('prerequisites')
            lease = await _offload(owned_manager.acquire, cancellation=cancelled)
            from .infrastructure_session import InfrastructureSession
            infrastructure = InfrastructureSession(owned_manager)
            if self.bind_infrastructure is not None:
                config = self.bind_infrastructure(config, infrastructure)
                if config.errors or config.missing or callback_endpoint(config) != endpoint:
                    raise ValueError('invalid_bound_runtime_configuration')
            stage('runtime')
            runtime = self.compose_runtime(config, endpoint.origin, tls.material)
            await runtime.astart()
            stage('listener')
            application = self.create_application(runtime, endpoint.hostname)
            owned_listener = self.listener()
            await owned_listener.start(application, tls.material, endpoint)
            await owned_listener.wait_ready(self.timeout)
            stage('listener ready')
            yield OperationSession(config, tls, lease, runtime, application, owned_listener, endpoint,
                                   infrastructure)
        finally:
            primary = sys.exc_info()[1]
            if lease is None:
                lease = getattr(owned_manager, 'lease', None)
            cleanup = OperationCleanup(owned_manager, runtime, owned_listener, lease, retain, self.timeout,
                                       complete=lambda: setattr(self, 'pending_cleanup', None))
            try:
                await cleanup.retry()
            except asyncio.CancelledError as cancellation:
                pending = getattr(cancellation, 'cleanup_pending', None)
                if pending is not None:
                    self.pending_cleanup = pending
                    if primary is not None:
                        primary.cleanup_pending = pending
                if primary is None:
                    raise
            except CleanupPendingError as pending:
                self.pending_cleanup = pending
                if primary is not None:
                    primary.cleanup_pending = pending
                else:
                    raise



def local_operation(context, *, resolve, argv, scope, ownership_directory,
                    engine_environment=None, collect=False, groups=(), timeout=120,
                    runtime_factory=None, application_wrapper=None, required_kinds=None, prepared_plan=None,
                    bind_infrastructure=None, on_stage=None):
    """Wire the local engine/TLS/API owners after an explicit operation choice.

    The caller supplies fresh resolution and handles an interpreter handoff
    before entering this context; no action occurs while building the runner.
    """
    from api_agent.runtime.dependencies import ensure_environment, active_environment
    from api_agent.runtime.https import prepare_local_https
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.prerequisites import PrerequisiteManager
    from api_agent.api.composition import compose_runtime
    from api_agent.api.application import create_application
    from api_agent.runtime.listener import UvicornListener
    from api_agent.auth.extensions import validate_operation

    def resolved_for_operation():
        config = resolve()
        if config.errors or config.missing:
            raise ValueError('invalid_runtime_configuration')
        validate_operation(context, config.values)
        return config

    def dependencies(config):
        owner = context.pack_root if getattr(context, 'external_pack', False) else context.base_root
        if not active_environment(owner / '.venv'):
            # The CLI completes its handoff before starting the event loop;
            # never accidentally launch another application from preparation.
            raise RuntimeError('interpreter_handoff_required')
        selected_groups = list(groups)
        if config.values.get('TUI_MODE') in {'integration', 'e2e'}:
            selected_groups.append('reports')
        if config.values.get('TRACE_MODE'):
            selected_groups.append('tracing')
        from api_agent.api.composition import activation_kinds
        from api_agent.providers.distribution import selected_manifests
        active = activation_kinds(config.values, required_kinds)
        manifests = selected_manifests(config.values, owner_root=getattr(context, 'owner_root', context.base_root),
                                       active_kinds=active)
        options = {'selected_provider_manifests': manifests} if manifests else {}
        status = ensure_environment(context, argv, collect=collect,
                                    groups=tuple(dict.fromkeys(selected_groups)), **options)
        if status is not None:
            raise RuntimeError('interpreter_handoff_required')

    def https(config, endpoint):
        return prepare_local_https(context.base_root / '.certs', hostname=endpoint.hostname,
                                   interactive=not config.values.get('NO_STD_IN', False))

    def plan(config):
        return prepared_plan if prepared_plan is not None else plan_compose(config.values, scope, engine_environment=engine_environment)

    def manager(selected_plan, cancelled):
        return PrerequisiteManager(selected_plan, scope, ownership_directory,
                                   engine_environment=engine_environment,
                                   cancelled=cancelled, deadline_seconds=timeout)

    def runtime(config, origin, material):
        if runtime_factory is not None:
            return runtime_factory(config, origin, material)
        return compose_runtime(context, config, callback_origin=origin, tls_material=material,
                               required_kinds=required_kinds)

    def application(runtime, hostname):
        app = create_application(runtime, allowed_hosts=(hostname, 'localhost', '127.0.0.1'))
        return application_wrapper(app) if application_wrapper is not None else app

    return OperationRunner(resolve=resolved_for_operation, prepare_dependencies=dependencies,
        prepare_https=https, plan=plan, manager=manager, compose_runtime=runtime,
        create_application=application, listener=UvicornListener, timeout=timeout,
        bind_infrastructure=bind_infrastructure, on_stage=on_stage)
