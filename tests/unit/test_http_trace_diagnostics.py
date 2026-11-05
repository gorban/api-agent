"""HTTP facts are finite, owned, scrubbed and independent of payload capture."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.telemetry.http_tracing import (HTTPMetadata, request_metadata,
    suppressed, bind_http, current_http, diagnostics_level, safe_name)
from api_agent.telemetry.operation import operation
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from test_workflow_observations import collector, config
from test_operation_traces import spans_from, attrs


@pytest.fixture(autouse=True)
def configured_application():
    from api_agent.api.application import Runtime, create_application
    create_application(Runtime('demo'))


def scope(path='/app/agent/internals/health', method='GET', **extra):
    return dict(type='http', path=path, method=method, headers=[], **extra)


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_http_facts_survive_capture_flags_and_scrub_credentials(mode, log, trace):
    with collector() as (endpoint, received):
        runtime = TraceRuntime(config(log, trace, mode,
            TRACE_EXPORT_ENDPOINT=endpoint + ('/api/public/otel/v1/traces' if mode == 'LangFuse' else ''),
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret'),
            boundary=EmissionBoundary(ContentPolicy(log), secrets=['GET']))
        async def run():
            await runtime.astart()
            with operation(runtime, http=HTTPMetadata('GET', '/app/agent/models')) as owner:
                owner.span.structural({'http.response.status_code': 403,
                    'http.route': '/private/credential', 'http.request.method': 'private-method'})
            await runtime.aclose()
        asyncio.run(run())
    span, = spans_from(received)
    assert attrs(span)['http.response.status_code'] == {'intValue': '403'}
    assert attrs(span)['http.route'] == {'stringValue': '/app/agent/models'}
    assert 'GET' not in span.name
    assert attrs(span)['http.request.method'] != {'stringValue': 'GET'}
    assert 'private-method' not in repr(received)
    assert '/private/credential' not in repr(received)
    assert span.status.code == 0


@pytest.mark.parametrize('method,path,diagnostic', [
    ('GET', '/app/agent/internals/health', True),
    ('GET', '/app/agent/internals/health/startup', True),
    ('GET', '/app/agent/internals/health/ready', True),
    ('HEAD', '/app/agent/internals/health', False),
    ('POST', '/app/agent/chat/heartbeat', True),
    ('GET', '/app/agent/chat/heartbeat', False),
    ('GET', '/app/agent/internals/health/private', False),
    ('GET', '/private/sensitive?credential=value', False),
])
def test_only_resolved_builtin_probes_are_diagnostic(method, path, diagnostic):
    metadata = request_metadata(scope(path, method), owned=True)
    assert metadata.diagnostic is diagnostic
    assert suppressed('off', metadata) is diagnostic
    assert not suppressed('info', metadata)
    unowned = request_metadata(scope(path, method))
    assert unowned.route is None and not unowned.diagnostic
    assert unowned.name == method


def test_preflights_require_both_headers_and_builtin_route():
    request = scope('/app/agent/chat', 'OPTIONS')
    assert not request_metadata(request, owned=True).diagnostic
    request['headers'] = [(b'host', b'localhost'), (b'origin', b'https://example.test'), (b'access-control-request-method', b'POST')]
    assert request_metadata(request, owned=True).diagnostic
    assert not request_metadata(request).diagnostic
    request['headers'].append((b'origin', b'duplicate'))
    assert not request_metadata(request, owned=True).diagnostic


def test_mount_dynamic_id_and_unknown_method_never_enter_name():
    metadata = request_metadata(scope('/secret-mount/app/agent/result-sets/private-id',
        root_path='/secret-mount'), owned=True)
    assert metadata.name == 'GET /app/agent/result-sets/{result_id}'
    assert request_metadata(scope(method='SECRET')).name == 'HTTP'
    assert safe_name('HTTP') and not safe_name('GET /private')
    assert not safe_name('GET ') and not safe_name(None)


def test_custom_resolver_and_failed_classification_are_not_diagnostics():
    from django.urls import path, re_path
    from api_agent.api import views
    custom = (path('app/agent/internals/health', lambda request: None),)
    assert request_metadata(scope(), owned=True, urlconf=custom).route is None
    positional = (re_path(r'^(app/agent/internals/health)$', views.health),)
    assert request_metadata(scope(), owned=True, urlconf=positional).route is None
    malformed = (path('app/agent/internals/health', views.health, {'probe': 'private'}),)
    assert request_metadata(scope(), owned=True, urlconf=malformed).route is None
    relocated = (path('other/<str:result_id>', views.result_set),)
    assert request_metadata(scope('/other/private'), owned=True, urlconf=relocated).route is None
    assert request_metadata(scope(), owned=True, urlconf='missing_private_module').name == 'GET'


def test_request_binding_requires_exact_owner_and_scope_and_resets():
    owner, other = object(), object()
    request = scope()
    metadata = HTTPMetadata('GET', diagnostic=True)
    with pytest.raises(asyncio.CancelledError):
        with bind_http(owner, request, metadata):
            assert current_http(owner, request) is metadata
            assert current_http(other, request) is None
            assert current_http(owner, dict(request)) is None
            raise asyncio.CancelledError
    assert current_http(owner, request) is None


@pytest.mark.parametrize('value', ['', 'INFO', False, None, 'private'])
def test_invalid_diagnostics_level_rejected(value):
    with pytest.raises(ValueError, match='Invalid HTTP tracing diagnostics level'):
        diagnostics_level({'TRACE_HTTP_DIAGNOSTICS_LEVEL': value})


def test_builtin_probe_preserves_response_without_entering_request_owner():
    from api_agent.api import Runtime, create_application
    from test_api_foundation import http
    class UninitializedOwner:
        http_diagnostics_level = 'off'
        def request(self, request):
            pytest.fail('diagnostic must not initialize its request owner')
    app = create_application(Runtime('demo', tracing=UninitializedOwner()))
    assert http(app, '/app/agent/internals/health')[0] == 503
    assert http(app, '/app/agent/internals/health/ready')[0] == 503


@pytest.mark.parametrize('custom_backend', [False, True])
def test_injected_handler_or_backend_cannot_claim_probe_ownership(custom_backend):
    from contextlib import asynccontextmanager
    from api_agent.api import Runtime, create_application
    from api_agent.api.application import Application
    from test_api_foundation import http
    entered = []
    class Owner:
        http_diagnostics_level = 'off'
        @asynccontextmanager
        async def request(self, request):
            entered.append(request['path'])
            yield None
    async def handler(request, receive, send):
        await send({'type': 'http.response.start', 'status': 202, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'custom'})
    class Backend:
        async def handle_asgi(self, request, receive, send, fallback):
            await handler(request, receive, send)
            return True
    runtime = Runtime('demo', tracing=Owner(), services={'studio_backend': Backend()} if custom_backend else {})
    app = create_application(runtime) if custom_backend else Application(runtime, handler)
    assert http(app, '/app/agent/internals/health')[0] == 202
    assert entered == ['/app/agent/internals/health']


def test_direct_studio_probe_bridge_never_opens_provider(monkeypatch):
    from api_agent.api.studio import StudioBackend
    from api_agent.testing.studio_tracing import StudioTraceOwner
    owner = StudioTraceOwner(config().values, {}, object())
    owner.bind_http('http://localhost', 'nonce', backend=object.__new__(StudioBackend))
    monkeypatch.setattr(owner, '_authorized', lambda request: pytest.fail('provider admission reached'))
    async def run():
        async with owner.request(scope('/app/agent/chat/heartbeat', 'POST')) as selected:
            assert selected is None
    asyncio.run(run())
    assert owner._sessions == {}


@pytest.mark.parametrize('level,method,count', [('off', 'GET', 0), ('info', 'GET', 1), ('off', 'HEAD', 1)])
@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
def test_application_probe_policy_preserves_failed_response_and_serialized_status(level, method, count, mode):
    from api_agent.api import Runtime, create_application
    from test_api_foundation import http
    with collector() as (endpoint, received):
        runtime = TraceRuntime(config(TRACE_MODE=mode, TRACE_HTTP_DIAGNOSTICS_LEVEL=level,
            TRACE_EXPORT_ENDPOINT=endpoint + ('/api/public/otel/v1/traces' if mode == 'LangFuse' else ''),
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret'))
        app = create_application(Runtime('demo', tracing=runtime))
        asyncio.run(runtime.astart())
        try:
            status, _, _ = http(app, '/app/agent/internals/health', method)
            assert status == (405 if method == 'HEAD' else 503)
        finally:
            asyncio.run(runtime.aclose())
    spans = spans_from(received)
    assert len(spans) == count
    if count:
        span, = spans
        assert span.name == method + ' /app/agent/internals/health'
        assert attrs(span)['http.request.method'] == {'stringValue': method}
        assert attrs(span)['http.response.status_code'] == {'intValue': str(status)}
        assert span.status.code == 0


def test_failed_header_send_does_not_record_status_and_cancellation_resets_binding():
    from api_agent.api.application import Application
    from contextlib import asynccontextmanager
    active = []
    class Span:
        def structural(self, values):
            active.append(values)
        def end(self):
            pass
        def outcome(self, value):
            pass
    class Trace:
        boundary = EmissionBoundary(ContentPolicy(False))
        def begin(self, *args, **kwargs):
            return Span()
    trace = Trace()
    request = scope('/private/path')
    async def handler(request, receive, send):
        await send({'type': 'http.response.start', 'status': 200})
    async def send(message):
        raise asyncio.CancelledError
    app = Application(SimpleNamespace(tracing=trace, services={}), handler)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await app(request, None, send)
        assert current_http(trace, request) is None
    asyncio.run(run())
    assert not any('http.response.status_code' in values for values in active)


@pytest.mark.parametrize('level', ['off', 'info', 'invalid'])
def test_shared_config_validates_diagnostics_level(level):
    from api_agent.config import load_shared_schema, resolve_config
    result = resolve_config(load_shared_schema(), environ={}, dotenv={'TRACE_HTTP_DIAGNOSTICS_LEVEL': level})
    assert bool(result.errors) is (level == 'invalid')
    if level != 'invalid':
        assert result.values['TRACE_HTTP_DIAGNOSTICS_LEVEL'] == level


def test_invalid_metadata_values_cannot_export_arbitrary_names():
    metadata = HTTPMetadata([], [])
    assert metadata.name == 'HTTP'
    assert metadata.attributes() == {'http.request.method': '_OTHER'}
    assert request_metadata({'method': None}) is None
    request = scope('/app/agent/chat', 'OPTIONS')
    request['headers'] = [(b'origin', b' '), (b'access-control-request-method', b'POST')]
    assert not request_metadata(request, owned=True).diagnostic
    request['headers'] = [('origin', 'value')]
    assert not request_metadata(request, owned=True).diagnostic


def test_runtime_binding_still_validates_http_policy():
    from api_agent.telemetry.trace_mapping import TraceMapping
    with pytest.raises(ValueError, match='Invalid HTTP tracing diagnostics level'):
        TraceRuntime(config(TRACE_HTTP_DIAGNOSTICS_LEVEL='invalid'), binding=SimpleNamespace(mapping=TraceMapping()))


@pytest.mark.parametrize('path', ['/app/agent/internals/health', '/app/agent/result-sets/private'])
def test_direct_bridge_does_not_claim_delegated_dispatch(monkeypatch, path):
    from api_agent.api.studio import StudioBackend
    from api_agent.testing.studio_tracing import StudioTraceOwner
    owner = StudioTraceOwner(config().values, {}, object())
    owner.bind_http('http://localhost', 'nonce', backend=object.__new__(StudioBackend))
    admitted = []
    monkeypatch.setattr(owner, '_authorized', lambda request: admitted.append(request) or False)
    request = scope(path)
    async def run():
        async with owner.request(request) as selected:
            assert selected is None
    asyncio.run(run())
    assert admitted == [request]


@pytest.mark.parametrize('method,route,status', [('private', '/private', 99), (None, None, 600), ([], [], True)])
def test_structural_http_rejects_unbounded_values(method, route, status):
    from api_agent.telemetry.tracing import _structural
    assert _structural(EmissionBoundary(ContentPolicy(True)), {
        'http.request.method': method, 'http.route': route,
        'http.response.status_code': status}) == {}
