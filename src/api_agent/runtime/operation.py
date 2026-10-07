"""Bounded ownership of one local API operation and its prerequisites."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import sys
from threading import Event
from urllib.parse import urlsplit

from .tasks import settle as _settle, offload as _offload


@dataclass(frozen=True)
class CallbackEndpoint:
    origin: str
    hostname: str
    port: int


def callback_endpoint(config):
    """Use the independently configured HTTPS listener authority."""
    raw = config.values.get('API_LISTENER_URL', '')
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


class OperationRunner:
    """Inject side effects; creating this coordinator performs no I/O.

    resolve and all preparation functions are synchronous. Listener implements
    async start(application, tls, endpoint), wait_ready(timeout), and stop().
    Runtime implements astart/aclose. Manager implements acquire/release.
    """

    def __init__(self, *, resolve, prepare_dependencies, prepare_https, plan,
                 manager, compose_runtime, create_application, listener,
                 timeout=120):
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
        self.timeout = timeout

    @asynccontextmanager
    async def open(self, *, retain=False, validate=None):
        """Start in dependency order; release a test lease after the workload."""
        config = self.resolve()
        if config.errors or config.missing:
            raise ValueError('invalid_runtime_configuration')
        if validate is not None:
            validate(config)
        endpoint = callback_endpoint(config)
        await _offload(self.prepare_dependencies, config)
        tls = await _offload(self.prepare_https, config, endpoint)
        selected_plan = self.plan(config)
        cancelled = Event()
        owned_manager = self.manager(selected_plan, cancelled.is_set)
        lease = None
        runtime = None
        owned_listener = None
        try:
            lease = await _offload(owned_manager.acquire, cancellation=cancelled)
            runtime = self.compose_runtime(config, endpoint.origin, tls.material)
            await runtime.astart()
            application = self.create_application(runtime, endpoint.hostname)
            owned_listener = self.listener()
            await owned_listener.start(application, tls.material, endpoint)
            await owned_listener.wait_ready(self.timeout)
            yield OperationSession(config, tls, lease, runtime, application, owned_listener, endpoint)
        finally:
            cleanup_cancellations = asyncio.current_task().cancelling()
            # A start failure may have bound a socket before raising. Stop even
            # then, and attempt every later cleanup when an earlier one fails.
            failed = None
            if owned_listener is not None:
                try:
                    await _settle(asyncio.create_task(owned_listener.stop()))
                except BaseException as error:
                    failed = error
            if runtime is not None:
                try:
                    await _settle(asyncio.create_task(runtime.aclose()))
                except BaseException as error:
                    failed = failed or error
            if lease is None:
                lease = getattr(owned_manager, 'lease', None)
            if lease is not None:
                try:
                    await _offload(owned_manager.release, retain=retain)
                except BaseException as error:
                    failed = failed or error
            if failed is not None and sys.exc_info()[0] is None:
                raise failed
            if (sys.exc_info()[0] is None
                    and asyncio.current_task().cancelling() > cleanup_cancellations):
                raise asyncio.CancelledError


def local_operation(context, *, resolve, argv, scope, ownership_directory,
                    engine_environment=None, collect=False, groups=(), timeout=120):
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
        status = ensure_environment(context, argv, collect=collect,
                                    groups=tuple(dict.fromkeys(selected_groups)))
        if status is not None:
            raise RuntimeError('interpreter_handoff_required')

    def https(config, endpoint):
        return prepare_local_https(context.base_root / '.certs', hostname=endpoint.hostname,
                                   interactive=not config.values.get('NO_STD_IN', False))

    def plan(config):
        return plan_compose(config.values, scope, engine_environment=engine_environment)

    def manager(selected_plan, cancelled):
        return PrerequisiteManager(selected_plan, scope, ownership_directory,
                                   engine_environment=engine_environment,
                                   cancelled=cancelled, deadline_seconds=timeout)

    def runtime(config, origin, material):
        return compose_runtime(context, config, callback_origin=origin, tls_material=material)

    def application(runtime, hostname):
        return create_application(runtime, allowed_hosts=(hostname, 'localhost', '127.0.0.1'))

    return OperationRunner(resolve=resolved_for_operation, prepare_dependencies=dependencies,
        prepare_https=https, plan=plan, manager=manager, compose_runtime=runtime,
        create_application=application, listener=UvicornListener, timeout=timeout)
