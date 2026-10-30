"""Launch-owned workflow capture provenance and credential inventory admission."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

_FLAGS = ('LOG_CONTENT', 'TRACELOOP_TRACE_CONTENT')
_CURRENT = ContextVar('integration_workflow_capture', default=None)


def capture_sources(values, fields):
    """Retain exact resolved provenance, never infer permission from a mapping."""
    result = {}
    for name in _FLAGS:
        field = (fields or {}).get(name)
        if isinstance(field, ResolvedValue) and type(field.value) is bool:
            if field.value != values.get(name):
                raise ContractError('Workflow capture configuration conflicts with its provenance')
            if type(field.source) is not str or type(field.explicit) is not bool:
                raise ContractError('Invalid workflow capture configuration provenance')
            result[name] = dict(value=field.value, source=field.source, explicit=field.explicit)
    return result


def capture_config(values, sources):
    """Read only the parent's protected, exact content-setting records."""
    if type(sources) is not dict or set(sources) - set(_FLAGS):
        raise ContractError('Invalid workflow capture configuration provenance')
    fields = {}
    for name, row in sources.items():
        if (type(row) is not dict or set(row) != {'value', 'source', 'explicit'}
                or type(row['value']) is not bool or type(row['source']) is not str
                or type(row['explicit']) is not bool or type(values.get(name)) is not bool
                or row['value'] != values[name]):
            raise ContractError('Invalid workflow capture configuration provenance')
        fields[name] = ResolvedValue(**row)
    return ConfigResult(dict(values), fields, (), (), ())


class _WorkerCapture:
    def __init__(self, config, scope):
        from .inference_content import content_boundary
        self.config, self.scope = config, scope
        self.policy = ContentPolicy.from_config(config)
        self.boundary = content_boundary(config.values)
        scope.join('workflow', self.boundary)

    def services(self, provider=None):
        from api_agent.workflows.observations import WorkflowObservationFactory
        return {'emissions': self.boundary, 'content_policy': self.policy,
                'observation_factory': WorkflowObservationFactory(self.config, boundary=self.boundary),
                'capture_authority': lambda: self.authority(provider)}

    @asynccontextmanager
    async def authority(self, provider):
        # Prepare the remote registration before freezing caller registrations:
        # setup may already be waiting for a caller credential producer.
        client = getattr(provider, 'client', None) if provider is not None else None
        if client is not None:
            await client.prepare_graph_capture()
        lease = self.scope.freeze(credential_only=True)
        try:
            if client is None:
                yield _union_boundary(lease.secrets)
            else:
                async with client.graph_capture_authority() as remote:
                    if type(remote) is not EmissionBoundary:
                        raise ContractError('Workflow credential authority is unavailable')
                    with remote._authority.lock:
                        values = tuple(remote._secrets)
                    yield _union_boundary((*lease.secrets, *values))
        finally:
            lease.release()


def _union_boundary(values):
    values = set(values)
    if (any(type(value) is not str or not value for value in values)
            or len(values) > 64 or sum(len(value.encode('utf-8')) for value in values) > 16384):
        raise ContractError('Workflow credential authority exceeds limits')
    return EmissionBoundary(ContentPolicy(True), secrets=values, preserve_json_text=True)


@contextmanager
def worker_capture(snapshot, scope, manifest):
    """Bind only a declared workflow producer and real parent provenance."""
    from api_agent.workflows.capture_authority import bind_capture_services
    if manifest is None or 'workflow' not in manifest:
        token = _CURRENT.set(None)
        try:
            with bind_capture_services({}):
                yield
        finally:
            _CURRENT.reset(token)
        return
    config = capture_config(snapshot['config'], snapshot.get('capture_sources', {}))
    owner = _WorkerCapture(config, scope)
    token = _CURRENT.set(owner)
    try:
        with bind_capture_services(owner.services()):
            yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def provider_capture(provider):
    """Extend the current invocation capability for the fixture's lifetime."""
    owner = _CURRENT.get()
    if owner is None:
        yield
        return
    from api_agent.workflows.capture_authority import bind_capture_services
    boundary = getattr(provider, 'emissions', None)
    if boundary is not None:
        owner.scope.join('workflow', boundary)
    with bind_capture_services(owner.services(provider)):
        yield


def visit_authorities(visit):
    """Enroll the original live credential owners of a wrapped graph journal."""
    if visit is None:
        return ()
    journal = visit[0]
    getter = getattr(journal, 'credential_authorities', None)
    if getter is not None:
        boundaries = getter()
    else:
        boundary = getattr(journal, 'boundary', None) or getattr(journal, '_boundary', None)
        boundaries = () if boundary is None else (boundary,)
    if type(boundaries) is not tuple or any(type(item) is not EmissionBoundary for item in boundaries):
        raise ContractError('Workflow credential authority is unavailable')
    return tuple({id(item._authority): item for item in boundaries}.values())
