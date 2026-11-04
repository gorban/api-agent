"""Versioned, lazy tracing-provider admission and shared exporter ownership."""
from dataclasses import dataclass, field, replace
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
import inspect
import asyncio
import os
import json
from hashlib import sha256
from threading import Lock
import re
import tomllib

from api_agent.runtime.infrastructure_contract import _capture


class TracingProviderError(ValueError):
    """Fixed diagnostics contain no configuration or exporter response values."""


def _field(value):
    if type(value) is not str or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', value) is None:
        raise TracingProviderError('Tracing declaration requires a configuration field name')
    return value


@dataclass(frozen=True)
class NativeMapping:
    input_key: str = 'input.value'
    output_key: str = 'output.value'
    version: int = 1

    def io_attribute(self, direction):
        if direction not in ('input', 'output'):
            raise TracingProviderError('Invalid observation direction')
        return self.input_key if direction == 'input' else self.output_key


@dataclass(frozen=True)
class DashboardDeclaration:
    url_field: str
    readiness_field: str | None = None
    username_field: str | None = None
    password_field: str | None = None
    login: str = 'none'
    readiness_path: str = ''
    config_fields: tuple[str, ...] = ()
    managed_config_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TracingDeclaration:
    id: str
    signals: frozenset[str]
    mapping: NativeMapping
    secret_fields: tuple[str, ...]
    dashboard: DashboardDeclaration | None
    infrastructure: tuple[str, ...]
    source: object
    endpoint_field: str = 'TRACE_EXPORT_ENDPOINT'
    metrics_from_base: bool = False
    otlp_environment: bool = True
    propagation: bool = True
    selector: str | None = None
    disabled_value: str = ''
    profiles: tuple = ()
    profile: str | None = None
    managed_endpoints: tuple = ()
    managed_compose_profile: str | None = None


@dataclass(frozen=True)
class ExportPlan:
    trace_endpoint: str | None = field(repr=False)
    metric_endpoint: str | None = field(repr=False)
    protocol: str
    temporality: str
    resource: object = field(repr=False)
    headers: object = field(repr=False)


@dataclass(frozen=True)
class ExportHandles:
    provider: object
    tracer: object
    meter_provider: object = None
    meter: object = None


def read_declaration(root):
    try:
        source = _capture(root, 'tracing-provider.toml', 64 * 1024)
    except (OSError, ValueError) as error:
        raise TracingProviderError('Tracing provider declaration cannot be admitted') from error
    return parse_declaration(source)


def parse_declaration(source):
    """Reuse the versioned descriptor parser for admitted immutable bytes."""
    try:
        data = tomllib.loads(source.body.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise TracingProviderError('Tracing provider declaration cannot be admitted') from error
    return _parse_data(data, source)


def _parse_data(data, source):
    if data.get('version') == 2:
        return _parse_profiles(data, source)
    allowed = {'version', 'id', 'signals', 'input_attribute', 'output_attribute',
               'secret_fields', 'dashboard', 'infrastructure', 'endpoint_field', 'metrics_from_base', 'otlp_environment', 'propagation'}
    if (type(data.get('version')) is not int or data['version'] != 1 or set(data) - allowed
            or not {'id', 'signals'} <= data.keys()
            or type(data['id']) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', data['id']) is None):
        raise TracingProviderError('Unsupported tracing provider declaration')
    signals = data['signals']
    if (type(signals) is not list or any(type(item) is not str or item not in {'traces', 'metrics'} for item in signals)
            or len(set(signals)) != len(signals)):
        raise TracingProviderError('Tracing signals must be unique supported signal names')
    keys = [data.get('input_attribute', 'input.value'), data.get('output_attribute', 'output.value')]
    for key, direction in zip(keys, ('input', 'output')):
        if key not in ('langfuse.observation.' + direction, direction + '.value'):
            raise TracingProviderError('Unsupported native observation mapping')
    secrets = data.get('secret_fields', [])
    if type(secrets) is not list or len(secrets) > 32 or len(set(map(str, secrets))) != len(secrets):
        raise TracingProviderError('Tracing secret fields must be bounded unique names')
    secrets = tuple(_field(value) for value in secrets)
    dashboard = data.get('dashboard')
    if dashboard is not None:
        if (type(dashboard) is not dict or set(dashboard) - {'url_field', 'readiness_field', 'username_field', 'password_field', 'login', 'readiness_path', 'config_fields', 'managed_config_fields'}
                or 'url_field' not in dashboard or dashboard.get('login', 'none') not in {'none', 'provider'}
                or ('username_field' in dashboard) != ('password_field' in dashboard)):
            raise TracingProviderError('Invalid optional tracing dashboard capability')
        for key, value in dashboard.items():
            if key not in {'login', 'readiness_path', 'config_fields', 'managed_config_fields'}:
                _field(value)
        for member in ('config_fields', 'managed_config_fields'):
            config_fields = dashboard.get(member, [])
            if (type(config_fields) is not list or len(config_fields) > 32
                    or len(set(map(str, config_fields))) != len(config_fields)):
                raise TracingProviderError('Tracing dashboard configuration fields must be bounded unique names')
            dashboard = {**dashboard, member: tuple(_field(value) for value in config_fields)}
        path = dashboard.get('readiness_path', '')
        if (type(path) is not str or len(path) > 512 or path and (not path.startswith('/') or path.startswith('//')
                or any(value in path for value in ('?', '#', '..', '\\')) or any(ord(value) < 33 or ord(value) > 126 for value in path))
                or path and 'readiness_field' in dashboard):
            raise TracingProviderError('Invalid tracing dashboard readiness path')
        if set(secrets) & {dashboard.get('username_field'), dashboard.get('password_field')}:
            raise TracingProviderError('Exporter secrets cannot be dashboard login display values')
        if (dashboard.get('login', 'none') == 'provider') != ('username_field' in dashboard):
            raise TracingProviderError('Provider dashboard login requires explicit human login fields')
        dashboard = DashboardDeclaration(**dashboard)
    if not signals and dashboard is None:
        raise TracingProviderError('Tracing provider must declare an export or dashboard capability')
    infrastructure = data.get('infrastructure', [])
    if (type(infrastructure) is not list or len(infrastructure) > 16
            or any(type(value) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}/[a-z][a-z0-9_-]{0,63}', value) is None for value in infrastructure)
            or len(set(infrastructure)) != len(infrastructure)):
        raise TracingProviderError('Tracing infrastructure requires stable owner/service identities')
    if type(data.get('metrics_from_base', False)) is not bool or type(data.get('otlp_environment', True)) is not bool or type(data.get('propagation', True)) is not bool:
        raise TracingProviderError('Metric base-endpoint capability must be boolean')
    return TracingDeclaration(data['id'], frozenset(signals), NativeMapping(*keys), secrets,
                              dashboard, tuple(infrastructure), source,
                              _field(data.get('endpoint_field', 'TRACE_EXPORT_ENDPOINT')), data.get('metrics_from_base', False), data.get('otlp_environment', True), data.get('propagation', True))


def _validate_adapter(adapter, *, require_export=True):
    if (type(getattr(adapter, 'version', None)) is not int or adapter.version != 1
            or not inspect.iscoroutinefunction(getattr(adapter, 'aclose', None))):
        raise TracingProviderError('Tracing factory must return a version-1 owned adapter')
    try:
        inspect.signature(adapter.aclose).bind()
        if require_export:
            create = getattr(adapter, 'create_exporter', None)
            if (not callable(create) or inspect.iscoroutinefunction(create)
                    or inspect.isgeneratorfunction(create)):
                raise TypeError()
            inspect.signature(create).bind(object())
    except (TypeError, ValueError):
        raise TracingProviderError('Invalid tracing provider lifecycle signatures') from None



class TraceProviderBinding:
    """One captured factory and its exporter/metric lifetime; no global registration."""
    def __init__(self, declaration, admission, values, boundary, owner_root, explicit_fields=None, *, require_export=True):
        self.declaration, self.admission = declaration, admission
        self.values = MappingProxyType(deepcopy(dict(values)))
        self.boundary, self.owner_root = boundary, owner_root
        self.explicit_fields = explicit_fields
        self.require_export = require_export
        self._capture_identity = admission.identity
        self._environment = MappingProxyType({key: value for key, value in os.environ.items() if key.startswith('OTEL_')})
        self.mapping, self.signals = declaration.mapping, declaration.signals
        self.owner = self.handles = self.metrics = None
        self.exporter_factory = None
        self._closed = False
        self._opening = False
        self._load_lock = asyncio.Lock()
        self._retained = []
        self._signals = SignalOwner()
        self._creation_task = self._cleanup_task = None
        self._transferred = False
        for field in declaration.secret_fields:
            value = self.values.get(field)
            if require_export and value is not None and type(value) is not str:
                raise TracingProviderError('Tracing exporter credentials require string configuration values')
            if type(value) is str and value:
                self.boundary.register_secret(value)
        if require_export and self.values.get('TRACE_METRICS') and 'metrics' not in self.signals:
            raise TracingProviderError('Selected tracing provider does not support metrics')

    @property
    def signal_owner(self):
        """One acknowledged signal lifetime, including partial initialization."""
        return self._signals

    async def open(self, resource):
        if not self.require_export:
            raise TracingProviderError('Dashboard-only binding cannot initialize exporters')
        if self._closed or self._opening or self.handles is not None or self._transferred:
            raise TracingProviderError('Tracing provider binding is already opened or closed')
        from .genai_metrics import GenAIMetrics
        # Resolve credentials without resource attributes first; register them before
        # freezing any resource that could contain an identical credential value.
        from .genai_metrics import export_plan
        headers_plan = export_plan(self.values, self.declaration, {}, environ=self._environment,
                                   explicit_fields=self.explicit_fields)
        self._opening = True
        from .privacy import register_secret_async
        for headers in headers_plan.headers.values():
            for value in headers.values():
                if value:
                    await register_secret_async(self.boundary, value)
        plan = self._export_plan(resource)
        await self.load_adapter()
        if not self.signals:
            self._transferred = True
            self._opening = False
            return self._signals, None
        from api_agent.providers.progress import component_timeout
        self._creation_task = asyncio.create_task(asyncio.to_thread(self._create_exporter, plan))
        self._creation_task.add_done_callback(_consume)
        async with component_timeout('Tracing exporter initialization', 30):
            handles = await asyncio.shield(self._creation_task)
        self._capture_handles(handles)
        if isinstance(handles, BaseException):
            raise handles
        if (not isinstance(handles, ExportHandles)
                or ('traces' in self.signals and
                    (any(not callable(getattr(handles.provider, method, None)) for method in ('force_flush', 'shutdown'))
                     or not callable(getattr(handles.tracer, 'start_span', None))))
                or ('traces' not in self.signals and (handles.provider is not None or handles.tracer is not None))
                or (handles.meter_provider is not None and (not self.values.get('TRACE_METRICS') or 'metrics' not in self.signals))
                or (self.values.get('TRACE_METRICS') and
                    (any(not callable(getattr(handles.meter_provider, method, None)) for method in ('force_flush', 'shutdown'))
                     or any(not callable(getattr(handles.meter, method, None)) for method in ('create_counter', 'create_histogram'))))):
            raise TracingProviderError('Tracing exporter handles do not match admitted capabilities')
        self.handles = handles
        if self.values.get('TRACE_METRICS'):
            self.metrics = GenAIMetrics(handles.meter, profile=self.values.get('TRACE_METRIC_PROFILE', 'current'))
        self._transferred = True
        self._opening = False
        tracer = handles.tracer
        if 'traces' not in self.signals:
            from opentelemetry.trace import NoOpTracer
            tracer = NoOpTracer()
        return self._signals, tracer

    def _export_plan(self, resource=None):
        from .genai_metrics import export_plan, resource_plan
        resource = resource_plan(self.values, self.boundary, resource, environ=self._environment)
        return export_plan(self.values, self.declaration, resource, environ=self._environment,
                           explicit_fields=self.explicit_fields)

    def for_remote_trace(self):
        """Independent trace-only lifetime retaining the admitted pipeline identity."""
        identity = self.pipeline_identity
        if identity is None:
            raise TracingProviderError('Selected provider does not support remote trace propagation')
        values = dict(self.values)
        values['TRACE_METRICS'] = False
        binding = TraceProviderBinding(self.declaration, self.admission, values,
                                       self.boundary, self.owner_root, self.explicit_fields)
        binding._environment = self._environment
        binding._remote_pipeline_identity = identity
        return binding

    @property
    def pipeline_identity(self):
        if hasattr(self, '_remote_pipeline_identity'):
            return self._remote_pipeline_identity
        if not self.require_export or not self.declaration.propagation or 'traces' not in self.signals:
            return None
        plan = self._export_plan()
        proof = {'version': 1, 'capture': self._capture_identity,
                 'traces': plan.trace_endpoint, 'metrics': plan.metric_endpoint,
                 'protocol': plan.protocol, 'temporality': plan.temporality,
                 'resource': dict(plan.resource),
                 'headers': {signal: dict(values) for signal, values in plan.headers.items()},
                 'credentials': {field: self.values.get(field) for field in self.declaration.secret_fields},
                 'mapping': [self.mapping.input_key, self.mapping.output_key],
                 'profile': self.declaration.profile,
                 'metric_profile': self.values.get('TRACE_METRIC_PROFILE', 'current')}
        return sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def report(self):
        return {'version': 1, 'id': self.declaration.id, 'signals': sorted(self.signals),
                'owner': str(self.owner_root), 'identity': self.admission.identity,
                'descriptor_sha256': self.declaration.source.sha256, 'profile': self.declaration.profile,
                'dashboard': self.declaration.dashboard is not None,
                'metrics': ('disabled' if not self.values.get('TRACE_METRICS') else
                            'pending' if self.metrics is None else
                            'incomplete' if self.metrics.incomplete_calls else 'ready')}

    async def load_adapter(self):
        from api_agent.tooling.admission import prepare_tool_admission
        from api_agent.providers.loading import aload_captured_provider
        async with self._load_lock:
            if self._closed:
                raise TracingProviderError('Tracing provider binding is closed')
            if self.owner is None:
                self.admission = prepare_tool_admission(self.admission)
                try:
                    self.owner = await aload_captured_provider(self.admission, self.values,
                        MappingProxyType({'owner_root': self.owner_root, 'emissions': self.boundary,
                                          'own_exporter': self._signals.add, 'sdk_factory': self.exporter_factory, 'export_environment': self._environment}),
                        validator=lambda adapter: _validate_adapter(adapter, require_export=self.require_export and bool(self.signals)))
                except BaseException as error:
                    self._retained.extend(getattr(error, 'retained_owners', ()))
                    raise
            return self.owner.implementation

    async def dashboard_adapter(self):
        adapter = await self.load_adapter()
        for name, kwargs in (('prepare_dashboard', dict(url=object(), username=object(), password=object())),
                             ('dashboard_identity', dict(url=object()))):
            method = getattr(adapter, name, None)
            if not inspect.iscoroutinefunction(method):
                raise TracingProviderError('Tracing provider lacks its declared dashboard login capability')
            try:
                inspect.signature(method).bind(object(), **kwargs)
            except (TypeError, ValueError):
                raise TracingProviderError('Invalid tracing dashboard login signature') from None
        return adapter

    def _create_exporter(self, plan):
        # Keep cancellation raised inside the worker distinct from cancellation
        # of the operation awaiting it, so its partial owner remains recoverable.
        try:
            return self.owner.implementation.create_exporter(plan)
        except BaseException as error:
            return error

    def _capture_handles(self, handles):
        if isinstance(handles, BaseException):
            owner = getattr(handles, 'cleanup_owner', None)
            if owner is not None:
                self._signals.add(owner)
        if isinstance(handles, ExportHandles):
            self.handles = handles
            for owner in (handles.provider, handles.meter_provider):
                if owner is not None:
                    self._signals.add(owner)

    def record_call(self, structural, elapsed, outcome, *, attempts=None):
        if self.metrics is not None and not self._closed:
            from .privacy import ContentPolicy
            allowed = {'gen_ai.operation.name', 'gen_ai.provider.name', 'gen_ai.request.model',
                       'gen_ai.response.model', 'api_agent.call_id', 'gen_ai.usage.input_tokens',
                       'gen_ai.usage.output_tokens'}
            projected = {key: value for key, value in structural.items() if key in allowed}
            clean = self.boundary.for_policy(ContentPolicy(True)).export(projected)
            self.metrics.record_call(clean, elapsed, outcome, attempts=attempts)

    async def aclose(self):
        if self._closed:
            return
        from api_agent.providers.progress import component_timeout
        async with component_timeout('Tracing provider cleanup', 30):
            if self._creation_task is not None:
                try:
                    handles = await asyncio.shield(self._creation_task)
                except Exception:
                    pass
                else:
                    self._capture_handles(handles)
            if self._transferred:
                if not self._signals.disposal_confirmed:
                    raise TracingProviderError('Shared tracing signal disposal remains pending')
            elif not self._signals.disposal_confirmed:
                if self._cleanup_task is None:
                    self._cleanup_task = asyncio.create_task(asyncio.to_thread(self._signals.shutdown))
                    self._cleanup_task.add_done_callback(_consume)
                try:
                    await asyncio.shield(self._cleanup_task)
                except Exception:
                    self._cleanup_task = None
                    raise
            while self._retained:
                await self._retained[0].aclose()
                self._retained.pop(0)
            if self.owner is not None:
                await self.owner.aclose()
            self._opening = False
            self._closed = True


def admit_binding(config, *, owner_root, boundary, environ=None, explicit_fields=None, require_export=True):
    """Capture selected declarations/source without imports or authentication."""
    resolved = hasattr(config, 'fields') and hasattr(config, 'values')
    values = config.values if resolved else config
    explicit = frozenset(name for name, value in config.fields.items() if value.explicit) if resolved else frozenset(values)
    selection = effective_selection(config, owner_root=owner_root, environ=environ, explicit_fields=explicit_fields,
                                    require_export=require_export)
    if selection is None:
        return None
    values, explicit = selection.values, selection.explicit_fields
    path = selection.path
    declaration = selection.declaration
    from api_agent.tooling.admission import capture_tool_factory
    from api_agent.providers.loading import provider_location
    _, factory = provider_location(selection.location, owner_root=owner_root)
    admission = capture_tool_factory(path, factory, path.parent, interface='tracing-provider@1')
    if admission.assets.get(path.parent / 'tracing-provider.toml') != declaration.source.body:
        raise TracingProviderError('Tracing declaration changed during executable admission')
    from .privacy import ContentPolicy
    trace_boundary = boundary.for_policy(ContentPolicy.from_config(config, field_name='TRACELOOP_TRACE_CONTENT'))
    binding = TraceProviderBinding(declaration, admission, values, trace_boundary, path.parent, explicit, require_export=require_export)
    binding._environment = selection.environment
    return binding


def _consume(task):
    if not task.cancelled():
        task.exception()


class SignalOwner:
    """Compose signal shutdown under the existing bounded runtime cleanup worker."""
    def __init__(self):
        self.owners = []
        self.closed = set()
        self._lock = Lock()
        self._dispose_lock = Lock()

    def add(self, owner):
        if not callable(getattr(owner, 'shutdown', None)):
            raise TracingProviderError('Export resources require an owned shutdown method')
        with self._lock:
            if not any(item is owner for item in self.owners):
                self.owners.append(owner)
        return owner

    def force_flush(self, timeout_millis=5000):
        with self._dispose_lock:
            with self._lock:
                owners = tuple(self.owners)
            results = [owner.force_flush(timeout_millis=timeout_millis) for owner in owners]
        return all(results)

    def shutdown(self):
        with self._dispose_lock:
            with self._lock:
                pending = tuple((index, owner) for index, owner in enumerate(self.owners) if index not in self.closed)
            for index, owner in pending:
                owner.shutdown()
                with self._lock:
                    self.closed.add(index)

    @property
    def disposal_confirmed(self):
        with self._lock:
            return len(self.closed) == len(self.owners)


def _parse_profiles(data, source):
    allowed = {'version', 'id', 'selector', 'disabled_value', 'profiles'}
    rows = data.get('profiles')
    if (set(data) != allowed or type(data['version']) is not int
            or type(data['disabled_value']) is not str or len(data['disabled_value']) > 64
            or type(rows) is not list or not 1 <= len(rows) <= 16):
        raise TracingProviderError('Invalid bounded tracing profiles')
    selector = _field(data['selector'])
    profiles, seen = [], {data['disabled_value']}
    for row in rows:
        if type(row) is not dict or type(row.get('value')) is not str or {'id', 'version'} & row.keys() or not row['value'] or len(row['value']) > 64 or row['value'] in seen:
            raise TracingProviderError('Tracing profiles require unique exact values')
        seen.add(row['value'])
        managed = row.get('managed_endpoints', {})
        if (type(managed) is not dict or managed and (not {'trace_endpoint', 'dashboard_url'} <= managed.keys() or set(managed) - {'trace_endpoint', 'dashboard_url', 'dashboard_port_field', 'trace_endpoint_aliases'}
                or any(type(value) is not str or not value for key, value in managed.items() if key != 'trace_endpoint_aliases'))):
            raise TracingProviderError('Invalid managed tracing destinations')
        aliases = managed.get('trace_endpoint_aliases', [])
        if (type(aliases) is not list or len(aliases) > 8 or any(type(value) is not str or not value for value in aliases)
                or len(set(aliases)) != len(aliases)):
            raise TracingProviderError('Invalid managed tracing endpoint aliases')
        if aliases:
            managed = {**managed, 'trace_endpoint_aliases': tuple(aliases)}
        if managed:
            from .genai_metrics import _endpoint
            try:
                for endpoint in (managed['trace_endpoint'], managed['dashboard_url'], *aliases):
                    _endpoint(endpoint)
            except ValueError:
                raise TracingProviderError('Invalid managed tracing destination URL') from None
        if 'dashboard_port_field' in managed:
            _field(managed['dashboard_port_field'])
        compose = row.get('managed_compose_profile')
        if compose is not None and (type(compose) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', compose) is None):
            raise TracingProviderError('Invalid managed tracing Compose profile')
        payload = {key: value for key, value in row.items() if key not in {'value', 'managed_endpoints', 'managed_compose_profile'}}
        child = _parse_data({**payload, 'version': 1, 'id': data['id']}, source)
        if managed and (not (child.infrastructure or compose) or child.dashboard is None):
            raise TracingProviderError('Managed tracing destinations require infrastructure and dashboard')
        if compose and not managed:
            raise TracingProviderError('Managed tracing Compose profile requires destination bounds')
        profiles.append(replace(child, profile=row['value'], managed_endpoints=tuple(sorted(managed.items())), managed_compose_profile=compose))
    return replace(profiles[0], selector=selector, disabled_value=data['disabled_value'], profiles=tuple(profiles), profile=None)


def select_declaration(declaration, values):
    """Select capabilities from admitted bytes without imports or allocation."""
    if declaration.selector is None:
        return declaration
    if declaration.selector not in values:
        raise TracingProviderError(
            f'Tracing selector {declaration.selector} is unavailable; declare an active configuration field')
    value = values[declaration.selector]
    if value == declaration.disabled_value:
        return None
    for profile in declaration.profiles:
        if value == profile.profile:
            return profile
    raise TracingProviderError('Unsupported tracing provider profile')


def effective_selection(config, *, owner_root=None, environ=None, declaration=None, require_export=True, explicit_fields=None):
    from .tracing_selection import effective_selection as select
    return select(config, owner_root=owner_root, environ=environ, declaration=declaration, require_export=require_export, explicit_fields=explicit_fields)
