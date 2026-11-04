"""Pack-selected third tracing backends use captured factories and shared owners."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing_providers import (
    ExportHandles, SignalOwner, TracingProviderError, admit_binding, read_declaration,
)

BASE = Path(__file__).resolve().parents[2]


@pytest.fixture
def synthetic_provider(tmp_path):
    root = tmp_path / 'provider'
    root.mkdir()
    (root / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces", "metrics"]\n')
    (root / 'provider.py').write_text('''from api_agent.telemetry.tracing_providers import ExportHandles
class Owner:
    def force_flush(self, timeout_millis=5000): return True
    def shutdown(self): pass
class Tracer:
    def start_span(self, *args, **kwargs): pass
class Adapter:
    version=1
    def create_exporter(self, plan):
        self.plan=plan
        return ExportHandles(Owner(), Tracer())
    async def aclose(self): pass
def create_tracing_provider(config, services):
    return Adapter()
''')
    return root


def values(root, **overrides):
    return {'TRACE_PROVIDER': str(root / 'provider.py') + ':create_tracing_provider',
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example.test/v1/traces', **overrides}


def binding(root, **overrides):
    return admit_binding(values(root, **overrides), owner_root=root, boundary=EmissionBoundary())


def test_disabled_admission_is_inert(tmp_path):
    assert admit_binding({}, owner_root=tmp_path, boundary=EmissionBoundary()) is None


@pytest.mark.parametrize('field,value', [
    ('version', '2'), ('version', 'true'), ('id', '"invalid name"'), ('signals', '["logs"]'),
    ('signals', '["traces", "traces"]'), ('signals', '"traces"'), ('input_attribute', '"gen_ai.override"'),
    ('output_attribute', '"input.value"'), ('secret_fields', '1'), ('secret_fields', '["TOKEN", "TOKEN"]'),
    ('secret_fields', '["bad field"]'), ('infrastructure', '["missing-owner"]'),
    ('metrics_from_base', '"yes"'), ('extra', 'true'),
])
def test_invalid_tracing_declarations_reject_before_factory(tmp_path, field, value):
    data = {'version': '1', 'id': '"synthetic"', 'signals': '["traces"]'}
    data[field] = value
    (tmp_path / 'tracing-provider.toml').write_text('\n'.join(f'{key}={item}' for key, item in data.items()))
    with pytest.raises(TracingProviderError):
        read_declaration(tmp_path)


def test_declaration_missing_or_symlinked_fails_closed(tmp_path):
    with pytest.raises(TracingProviderError, match='cannot be admitted'):
        read_declaration(tmp_path)
    (tmp_path / 'real.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]')
    (tmp_path / 'tracing-provider.toml').symlink_to(tmp_path / 'real.toml')
    with pytest.raises(TracingProviderError, match='cannot be admitted'):
        read_declaration(tmp_path)


@pytest.mark.parametrize('dashboard', [
    '1', '{}', '{url_field="bad field"}', '{url_field="URL",login="unknown"}',
    '{url_field="URL",username_field="USER"}',
    '{url_field="URL",login="provider"}',
    '{url_field="URL",login="none",username_field="USER",password_field="PASSWORD"}',
    '{url_field="URL",login="provider",username_field="TOKEN",password_field="PASSWORD"}',
])
def test_invalid_dashboard_declarations_reject_before_factory(tmp_path, dashboard):
    (tmp_path / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\nsecret_fields=["TOKEN"]\ndashboard=' + dashboard)
    with pytest.raises(TracingProviderError):
        read_declaration(tmp_path)


def test_dashboard_only_declaration_and_mapping_are_explicit(tmp_path):
    (tmp_path / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=[]\n[dashboard]\nurl_field="DASHBOARD_URL"\n')
    declaration = read_declaration(tmp_path)
    assert not declaration.signals
    assert declaration.dashboard.login == 'none'
    assert declaration.mapping.io_attribute('input') == 'input.value'
    assert declaration.mapping.io_attribute('output') == 'output.value'
    with pytest.raises(TracingProviderError):
        declaration.mapping.io_attribute('sideways')
    (tmp_path / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=[]\n')
    with pytest.raises(TracingProviderError, match='export or dashboard'):
        read_declaration(tmp_path)


@pytest.mark.asyncio
async def test_captured_factory_remains_lazy_and_survives_source_replacement(synthetic_provider):
    selected = binding(synthetic_provider)
    (synthetic_provider / 'provider.py').write_text('raise RuntimeError("must not execute replacement")')
    provider, tracer = await selected.open({'service.name': 'synthetic-test'})
    assert provider.force_flush()
    assert callable(tracer.start_span)
    assert selected.owner.implementation.plan.resource['service.name'] == 'synthetic-test'
    assert await selected.load_adapter() is selected.owner.implementation
    with pytest.raises(TracingProviderError, match='already opened'):
        await selected.open({})
    with pytest.raises(TracingProviderError, match='disposal remains pending'):
        await selected.aclose()
    provider.shutdown()
    await selected.aclose()
    await selected.aclose()
    with pytest.raises(TracingProviderError, match='closed'):
        await selected.load_adapter()


@pytest.mark.asyncio
async def test_invalid_export_handles_are_owned_until_cleanup(synthetic_provider):
    path = synthetic_provider / 'provider.py'
    source = path.read_text().replace('return ExportHandles(Owner(), Tracer())', 'return ExportHandles(Owner(), None)')
    path.write_text(source)
    selected = binding(synthetic_provider)
    with pytest.raises(TracingProviderError, match='handles'):
        await selected.open({})
    assert selected.handles.provider is not None
    await selected.aclose()
    assert selected._signals.disposal_confirmed


@pytest.mark.asyncio
async def test_partial_export_allocation_is_retained_when_factory_raises(synthetic_provider):
    path = synthetic_provider / 'provider.py'
    source = path.read_text().replace('self.plan=plan', 'self.plan=plan\n        self.partial=self.services["own_exporter"](Owner())\n        raise RuntimeError("synthetic failure")').replace('return Adapter()', 'adapter=Adapter()\n    adapter.services=services\n    return adapter')
    path.write_text(source)
    selected = binding(synthetic_provider)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        await selected.open({})
    assert len(selected._signals.owners) == 1
    await selected.aclose()
    assert selected._signals.disposal_confirmed


def test_unsupported_signal_fails_before_factory_import(synthetic_provider):
    (synthetic_provider / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\n')
    with pytest.raises(TracingProviderError, match='does not support metrics'):
        binding(synthetic_provider, TRACE_METRICS=True)


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
def test_bundled_profiles_have_explicit_destinations_and_no_metric_base(mode):
    from api_agent.config import load_shared_schema
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.config.resolve import resolve_config
    schema = bootstrap_schema(load_shared_schema(BASE), (), {}, {}, owner_root=BASE)
    config = resolve_config(schema, environ={}, dotenv={}, overrides={'TRACE_MODE': mode}, interactive=True)
    selected = admit_binding(config, owner_root=BASE, boundary=EmissionBoundary())
    assert selected.declaration.id == 'bundled-tracing'
    assert selected.declaration.profile == mode
    assert not selected.declaration.metrics_from_base
    assert selected.values['TRACE_EXPORT_ENDPOINT'].endswith('/v1/traces')


def test_signal_owner_retries_shutdown_without_repeating_successful_disposal():
    events = []
    class Owner:
        def __init__(self, name): self.name, self.fail = name, name == 'meter'
        def force_flush(self, timeout_millis): events.append('flush-' + self.name); return True
        def shutdown(self):
            events.append('close-' + self.name)
            if self.fail:
                self.fail = False
                raise RuntimeError('retry')
    owned = SignalOwner()
    trace, meter = Owner('trace'), Owner('meter')
    owned.add(trace)
    owned.add(trace)
    owned.add(meter)
    assert owned.force_flush()
    with pytest.raises(RuntimeError): owned.shutdown()
    owned.shutdown()
    assert owned.disposal_confirmed
    assert events == ['flush-trace', 'flush-meter', 'close-trace', 'close-meter', 'close-meter']
    with pytest.raises(TracingProviderError): owned.add(object())


@pytest.mark.asyncio
@pytest.mark.parametrize('trace_content,log_content', [(False,False),(False,True),(True,False),(True,True)])
async def test_third_provider_through_shared_runtime_serializes_traces_and_metrics(synthetic_provider, trace_content, log_content):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.telemetry.tracing import TraceRuntime
    from test_genai_metrics import ledger
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = Thread(target=server.serve_forever,daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    (synthetic_provider / 'provider.py').write_text('''from api_agent.telemetry.tracing_providers import ExportHandles
class Adapter:
    version=1
    def __init__(self, services): self.own=services['own_exporter']
    def create_exporter(self, plan):
        from api_agent.telemetry.tracing import _sdk_factory
        from api_agent.telemetry.genai_metrics import sdk_meter
        provider,tracer=_sdk_factory(plan.trace_endpoint,dict(plan.headers['traces']),dict(plan.resource))
        self.own(provider)
        meter_provider,meter=sdk_meter(plan)
        self.own(meter_provider)
        return ExportHandles(provider,tracer,meter_provider,meter)
    async def aclose(self): pass
def create_tracing_provider(config,services):
    return Adapter(services)
''')
    config_values = values(synthetic_provider, TRACE_EXPORT_ENDPOINT=endpoint+'/traces',
        TRACE_METRICS=True, TRACE_METRICS_ENDPOINT=endpoint+'/metrics', TRACE_METRICS_TEMPORALITY='DELTA',
        TRACELOOP_TRACE_CONTENT=trace_content, LOG_CONTENT=log_content,
        TRACE_EXPORT_HEADERS='Authorization=synthetic-exporter-secret',
        TRACE_RESOURCE_ATTRIBUTES={'service.name':'synthetic-provider-test'})
    fields = {name:ResolvedValue(value,'cli',True) for name,value in config_values.items()}
    config = ConfigResult(config_values,fields,(),(),())
    boundary = EmissionBoundary()
    selected = admit_binding(config,owner_root=synthetic_provider,boundary=boundary)
    runtime = TraceRuntime(config,boundary=boundary,binding=selected,model_identities=('synthetic-model',))
    try:
        await runtime.astart()
        span = runtime.begin('chat synthetic-model', {'gen_ai.operation.name':'chat', 'api_agent.call_id':'call-1',
            'gen_ai.request.model':'synthetic-model'},kind='CLIENT')
        span.native_io('input',{'text':'synthetic-visible-input'})
        span.native_io('output',{'text':'synthetic-visible-output'})
        span.event({'authorization':'synthetic-exporter-secret','encrypted_reasoning':'hidden-marker'})
        span.end()
        runtime.record_call({'gen_ai.operation.name':'chat','api_agent.call_id':'call-1','gen_ai.request.model':'synthetic-model'},1,'success',attempts=ledger())
        await runtime.aclose()
    finally:
        await runtime.aclose()
        server.shutdown()
        server.server_close()
        thread.join()
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    trace = next(ExportTraceServiceRequest.FromString(body) for path,body in requests if path=='/traces')
    metric = next(ExportMetricsServiceRequest.FromString(body) for path,body in requests if path=='/metrics')
    spans = [span for resource in trace.resource_spans for scope in resource.scope_spans for span in scope.spans]
    assert len(spans) == 1
    attrs = {row.key:row.value for row in spans[0].attributes}
    assert ('input.value' in attrs) is trace_content
    assert ('output.value' in attrs) is trace_content
    assert 'synthetic-exporter-secret' not in str(trace)
    assert 'hidden-marker' not in str(trace)
    trace_resource = {row.key:row.value.string_value for row in trace.resource_spans[0].resource.attributes}
    metric_resource = {row.key:row.value.string_value for row in metric.resource_metrics[0].resource.attributes}
    assert trace_resource == metric_resource
    instruments = {row.name:row for resource in metric.resource_metrics for scope in resource.scope_metrics for row in scope.metrics}
    assert instruments['gen_ai.client.inference.usage.input_tokens'].sum.data_points[0].as_int == 12
    assert 'synthetic-visible' not in str(metric)


@pytest.mark.parametrize('adapter', [None, SimpleNamespace(version=True), SimpleNamespace(version=2),
    SimpleNamespace(version=1,aclose=lambda:None)])
def test_invalid_adapter_version_or_cleanup_is_rejected(adapter):
    from api_agent.telemetry.tracing_providers import _validate_adapter
    with pytest.raises(TracingProviderError): _validate_adapter(adapter)


@pytest.mark.parametrize('mode', ['missing','wrong_signature','async','generator'])
def test_invalid_export_method_is_rejected_before_allocation(mode):
    from api_agent.telemetry.tracing_providers import _validate_adapter
    class Adapter:
        version=1
        async def aclose(self): pass
    if mode=='wrong_signature': Adapter.create_exporter=lambda self:None
    if mode=='async':
        async def create(self,plan): pass
        Adapter.create_exporter=create
    if mode=='generator':
        def create(self,plan): yield plan
        Adapter.create_exporter=create
    with pytest.raises(TracingProviderError,match='signatures'): _validate_adapter(Adapter())
    _validate_adapter(Adapter(),require_export=False)


def test_secret_configuration_requires_strings_and_report_contains_identities_only(synthetic_provider):
    (synthetic_provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\nsecret_fields=["EXPORT_TOKEN"]\n')
    with pytest.raises(TracingProviderError,match='string configuration'):
        binding(synthetic_provider,EXPORT_TOKEN=True)
    selected=binding(synthetic_provider,EXPORT_TOKEN='synthetic-secret')
    report=selected.report()
    assert report['metrics']=='disabled'
    assert 'synthetic-secret' not in str(report)
    assert 'collector.example.test' not in str(report)


@pytest.mark.asyncio
async def test_cancelled_exporter_creation_retains_and_drains_its_partial_owner(synthetic_provider):
    from threading import Event
    started,release=Event(),Event()
    selected=binding(synthetic_provider)
    adapter=await selected.load_adapter()
    events=[]
    class Owner:
        def force_flush(self,timeout_millis=5000): return True
        def shutdown(self): events.append('shutdown')
    owner=Owner()
    def create(plan):
        selected._signals.add(owner)
        started.set()
        release.wait(3)
        return ExportHandles(owner,SimpleNamespace(start_span=lambda *a,**k:None))
    adapter.create_exporter=create
    task=asyncio.create_task(selected.open({}))
    assert await asyncio.to_thread(started.wait,2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    release.set()
    await selected.aclose()
    await selected.aclose()
    assert events==['shutdown']


@pytest.mark.asyncio
async def test_metric_initialization_failure_retains_both_sdk_owners(synthetic_provider):
    selected=binding(synthetic_provider,TRACE_METRICS=True,TRACE_METRICS_ENDPOINT='https://collector.example.test/metrics')
    adapter=await selected.load_adapter()
    events=[]
    class Owner:
        def __init__(self,name): self.name=name
        def force_flush(self,timeout_millis=5000): return True
        def shutdown(self): events.append(self.name)
    class BrokenMeter:
        def create_counter(self,*a,**k): raise RuntimeError('instrument failure')
        def create_histogram(self,*a,**k): pass
    adapter.create_exporter=lambda plan:ExportHandles(Owner('trace'),SimpleNamespace(start_span=lambda:None),Owner('meter'),BrokenMeter())
    with pytest.raises(RuntimeError,match='instrument failure'): await selected.open({})
    await selected.aclose()
    assert events==['trace','meter']


@pytest.mark.asyncio
async def test_metrics_only_provider_has_no_trace_exporter_but_retains_leaf_capture(synthetic_provider):
    from test_genai_metrics import Meter,call,ledger
    (synthetic_provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["metrics"]\n')
    selected=binding(synthetic_provider,TRACE_METRICS=True,TRACE_METRICS_ENDPOINT='https://collector.example.test/metrics')
    adapter=await selected.load_adapter()
    class Owner:
        def force_flush(self,timeout_millis=5000): return True
        def shutdown(self): pass
    meter=Meter()
    adapter.create_exporter=lambda plan:ExportHandles(None,None,Owner(),meter)
    assert selected.pipeline_identity is None
    provider,tracer=await selected.open({})
    assert tracer.start_span('non-exporting').is_recording() is False
    selected.record_call(call(),1,'success',attempts=ledger(None))
    assert selected.report()['metrics']=='incomplete'
    provider.shutdown()
    await selected.aclose()
    selected.record_call(call(),1,'success')


@pytest.mark.asyncio
async def test_dashboard_only_adapter_needs_no_export_method_or_sdk(synthetic_provider):
    (synthetic_provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=[]\n[dashboard]\nurl_field="URL"\n')
    (synthetic_provider/'provider.py').write_text('''class Adapter:
    version=1
    async def aclose(self): pass
def create_tracing_provider(config,services):
    return Adapter()
''')
    selected=binding(synthetic_provider)
    provider,tracer=await selected.open({})
    assert tracer is None and provider.disposal_confirmed
    with pytest.raises(TracingProviderError): await selected.open({})
    await selected.aclose()


def test_pipeline_identity_is_opaque_stable_and_resolves_actual_endpoint_provenance(synthetic_provider,monkeypatch):
    from api_agent.config.resolve import ConfigResult,ResolvedValue
    configured=values(synthetic_provider,TRACE_EXPORT_ENDPOINT='https://default.example.test/traces')
    result=ConfigResult(configured,{'TRACE_EXPORT_ENDPOINT':ResolvedValue(configured['TRACE_EXPORT_ENDPOINT'],'pack_default',False)},(),(),())
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT','https://environment.example.test/traces')
    selected=admit_binding(result,owner_root=synthetic_provider,boundary=EmissionBoundary())
    expected=selected.pipeline_identity
    assert len(expected)==64 and all(char in '0123456789abcdef' for char in expected)
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT','https://changed.example.test/traces')
    assert selected.pipeline_identity==expected
    changed=admit_binding(result,owner_root=synthetic_provider,boundary=EmissionBoundary())
    assert changed.pipeline_identity!=expected
    raw=admit_binding(configured,owner_root=synthetic_provider,boundary=EmissionBoundary())
    assert raw._export_plan().trace_endpoint==configured['TRACE_EXPORT_ENDPOINT']
    (synthetic_provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\npropagation=false\n')
    assert binding(synthetic_provider).pipeline_identity is None


@pytest.mark.asyncio
async def test_remote_trace_view_preserves_identity_without_metric_allocation(synthetic_provider):
    selected = binding(synthetic_provider, TRACE_METRICS=True,
                       TRACE_METRICS_ENDPOINT='http://collector:4318/v1/metrics')
    remote = selected.for_remote_trace()
    assert remote.pipeline_identity == selected.pipeline_identity
    assert remote.values['TRACE_METRICS'] is False
    owner, tracer = await remote.open({'service.name': 'api-agent'})
    assert tracer is not None and remote.metrics is None
    assert remote.handles.meter_provider is None
    owner.shutdown()
    await remote.aclose()
    assert selected.handles is None


def test_remote_trace_view_rejects_unsupported_propagation(synthetic_provider):
    path = synthetic_provider / 'tracing-provider.toml'
    path.write_text(path.read_text() + 'propagation=false\n')
    with pytest.raises(TracingProviderError, match='remote trace'):
        binding(synthetic_provider).for_remote_trace()

@pytest.mark.asyncio
@pytest.mark.parametrize('standard', [False, True])
async def test_header_secret_resource_is_rejected_before_factory(synthetic_provider, monkeypatch, standard):
    token = 'synthetic-header-resource-credential'
    values = {'TRACE_RESOURCE_ATTRIBUTES': {'service.name': token}}
    if standard:
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'X-Key=' + token)
    else:
        values['TRACE_EXPORT_HEADERS'] = 'X-Key=' + token
    from unittest.mock import Mock
    factory = Mock(side_effect=AssertionError('must not load a factory'))
    monkeypatch.setattr('api_agent.providers.loading.aload_captured_provider', factory)
    with pytest.raises(ValueError, match='credential'):
        binding(synthetic_provider, **values)
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['reader', 'provider', 'meter'])
async def test_builtin_meter_startup_failure_retains_retryable_cleanup(monkeypatch, stage):
    from opentelemetry.sdk.metrics import MeterProvider
    events = []
    pending = [True]
    class Session:
        def close(self): events.append('session-close')
    class Reader:
        def __init__(self, *args, **kwargs):
            if stage == 'reader': raise RuntimeError('construction-failed')
        def shutdown(self, **kwargs):
            events.append('reader-shutdown')
            if pending[0]: raise RuntimeError('cleanup-pending')
    def initialize(self, *args, **kwargs):
        if stage == 'provider': raise RuntimeError('construction-failed')
    def get_meter(self, *args, **kwargs): raise RuntimeError('construction-failed')
    def shutdown(self, **kwargs):
        events.append('provider-shutdown')
        if pending[0]: raise RuntimeError('cleanup-pending')
    monkeypatch.setattr('requests.Session', Session)
    monkeypatch.setattr('opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader', Reader)
    monkeypatch.setattr(MeterProvider, '__init__', initialize)
    monkeypatch.setattr(MeterProvider, 'get_meter', get_meter)
    monkeypatch.setattr(MeterProvider, 'shutdown', shutdown)
    selected = admit_binding({'TRACE_PROVIDER':str(BASE/'src/api_agent/telemetry/trace_adapters/provider.py')+':create_tracing_provider', 'TRACE_EXPORT_ENDPOINT':'http://localhost:4318/v1/traces','JAEGER_VIEWER_PORT':16686,'TRACE_MODE': 'OTEL', 'TRACE_METRICS': True,
        'TRACE_METRICS_ENDPOINT': 'http://collector/metrics'}, owner_root=BASE, boundary=EmissionBoundary())
    with pytest.raises(RuntimeError, match='construction-failed'):
        await selected.open({})
    assert selected._signals.owners
    if stage != 'reader':
        with pytest.raises(RuntimeError, match='cleanup-pending'):
            await selected.aclose()
        assert not selected._closed and not selected._signals.disposal_confirmed
    pending[0] = False
    await selected.aclose()
    assert selected._closed and selected._signals.disposal_confirmed
    assert 'session-close' in events

@pytest.mark.asyncio
async def test_failed_factory_retains_loader_cleanup_receipt(synthetic_provider, monkeypatch):
    from unittest.mock import AsyncMock
    retained = SimpleNamespace(aclose=AsyncMock())
    async def fail(*args, **kwargs):
        error = RuntimeError('factory failed with owned partial resources')
        error.retained_owners = [retained]
        raise error
    monkeypatch.setattr('api_agent.providers.loading.aload_captured_provider', fail)
    selected = binding(synthetic_provider)
    with pytest.raises(RuntimeError, match='factory failed'):
        await selected.load_adapter()
    await selected.aclose()
    retained.aclose.assert_awaited_once()
    with pytest.raises(TracingProviderError, match='closed'):
        await selected.load_adapter()


@pytest.mark.asyncio
async def test_dashboard_invalid_hook_signature_rejects(synthetic_provider):
    path = synthetic_provider/'provider.py'
    path.write_text(path.read_text().replace('    async def aclose(self): pass',
        '    async def prepare_dashboard(self): pass\n    async def dashboard_identity(self): pass\n    async def aclose(self): pass'))
    selected = binding(synthetic_provider)
    with pytest.raises(TracingProviderError, match='signature'):
        await selected.dashboard_adapter()
    await selected.aclose()


def test_descriptor_replacement_between_capture_steps_rejects(synthetic_provider, monkeypatch):
    from dataclasses import replace
    from types import MappingProxyType
    import api_agent.tooling.admission as module
    original = module.capture_tool_factory
    def changed(*args, **kwargs):
        captured = original(*args, **kwargs)
        return replace(captured, assets=MappingProxyType({**captured.assets,
            synthetic_provider/'tracing-provider.toml': b'version=2'}))
    monkeypatch.setattr(module, 'capture_tool_factory', changed)
    with pytest.raises(TracingProviderError, match='changed during'):
        binding(synthetic_provider)


@pytest.mark.asyncio
async def test_invalid_nonhandle_factory_result_is_cleanup_safe(synthetic_provider):
    path = synthetic_provider/'provider.py'
    path.write_text(path.read_text().replace('return ExportHandles(Owner(), Tracer())', 'return object()'))
    selected = binding(synthetic_provider)
    with pytest.raises(TracingProviderError, match='handles'):
        await selected.open({})
    await selected.aclose()
    assert selected._closed

@pytest.mark.asyncio
async def test_empty_header_value_and_explicit_builtin_target_are_safe(synthetic_provider):
    selected = binding(synthetic_provider, TRACE_EXPORT_HEADERS='X-Empty=')
    owner, _ = await selected.open({})
    owner.shutdown()
    await selected.aclose()
    builtin = admit_binding({'TRACE_PROVIDER':str(BASE/'src/api_agent/telemetry/trace_adapters/provider.py')+':create_tracing_provider','TRACE_MODE':'OTEL','TRACE_EXPORT_ENDPOINT':'http://chosen/traces','JAEGER_VIEWER_PORT':16686},owner_root=BASE,boundary=EmissionBoundary())
    assert builtin._export_plan().trace_endpoint == 'http://chosen/traces'
    await builtin.aclose()


@pytest.mark.asyncio
async def test_cancelled_background_task_consumer_is_safe():
    from api_agent.telemetry.tracing_providers import _consume
    task = asyncio.create_task(asyncio.sleep(100))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _consume(task)


@pytest.mark.asyncio
async def test_cancelled_cleanup_wait_retains_same_shutdown_transaction(synthetic_provider):
    from threading import Event
    selected = binding(synthetic_provider)
    entered, release = Event(), Event()
    class Owner:
        def shutdown(self): entered.set(); release.wait(5)
    selected._signals.add(Owner())
    first = asyncio.create_task(selected.aclose())
    await asyncio.to_thread(entered.wait, 5)
    transaction = selected._cleanup_task
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    retry = asyncio.create_task(selected.aclose())
    await asyncio.sleep(0)
    assert not retry.done()
    release.set()
    await retry
    assert selected._cleanup_task is transaction and selected._closed

@pytest.mark.asyncio
@pytest.mark.parametrize('metrics', [False, True])
async def test_builtin_otlp_adapter_uses_registered_signal_owners(monkeypatch, metrics):
    from api_agent.telemetry.trace_adapters.otel.provider import create_tracing_provider
    from api_agent.telemetry.genai_metrics import export_plan
    from test_genai_metrics import declaration
    owner, meter_owner = SignalOwner(), SignalOwner()
    trace = SimpleNamespace(start_span=lambda: None)
    meter = object()
    registered = []
    monkeypatch.setattr('api_agent.telemetry.tracing._sdk_factory', lambda *a, own, environ=None: (own(owner) or owner, trace))
    def make_meter(plan, *, own): own(meter_owner); return meter_owner, meter
    monkeypatch.setattr('api_agent.telemetry.genai_metrics.sdk_meter', make_meter)
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://collector/traces', 'TRACE_METRICS':metrics,
        'TRACE_METRICS_ENDPOINT':'http://collector/metrics'}, declaration(), {}, environ={})
    adapter = create_tracing_provider({}, {'own_exporter':registered.append})
    handles = adapter.create_exporter(plan)
    assert handles.provider is owner and owner in registered
    assert (handles.meter is meter) is metrics
    await adapter.aclose()


@pytest.mark.asyncio
async def test_builtin_langfuse_adapter_owns_exporter_and_registers_derived_auth(monkeypatch):
    from api_agent.telemetry.trace_adapters.langfuse.provider import create_tracing_provider
    from api_agent.telemetry.genai_metrics import export_plan
    from test_genai_metrics import declaration
    boundary = EmissionBoundary()
    registered = []
    captured = []
    owner = SignalOwner()
    monkeypatch.setattr('api_agent.telemetry.tracing._sdk_factory', lambda *a, own, environ=None: (captured.append(a) or own(owner) or owner, object()))
    services = {'emissions':boundary,'own_exporter':registered.append}
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://collector/api/public/otel/v1/traces'}, declaration(), {}, environ={})
    invalid = create_tracing_provider({}, services)
    with pytest.raises(ValueError, match='API keys'): invalid.create_exporter(plan)
    adapter = create_tracing_provider({'LANGFUSE_PUBLIC_KEY':'synthetic-public','LANGFUSE_SECRET_KEY':'synthetic-secret'},services)
    assert adapter.create_exporter(plan).provider is owner
    assert registered == [owner]
    endpoint, headers, resource = captured[0]
    assert endpoint.endswith('/api/public/otel/v1/traces')
    from api_agent.telemetry.privacy import ContentPolicy
    assert boundary.for_policy(ContentPolicy(True)).export({'value':headers['Authorization']})['value'] != headers['Authorization']
    await adapter.aclose()


def test_tracing_assets_count_toward_total_source_admission_bound(synthetic_provider, monkeypatch):
    from api_agent.tooling.admission import capture_tool_factory, ToolAdmissionError
    source=synthetic_provider/'provider.py'
    descriptor=synthetic_provider/'tracing-provider.toml'
    monkeypatch.setattr('api_agent.tooling.admission.MAX_TOOL_BYTES',len(source.read_bytes())+len(descriptor.read_bytes())-1)
    with pytest.raises(ToolAdmissionError,match='too large'):
        capture_tool_factory(source,'create_tracing_provider',synthetic_provider,interface='tracing-provider@1')


@pytest.mark.asyncio
async def test_tracing_factory_identity_mutation_rejects_before_execution(synthetic_provider):
    from dataclasses import replace
    from api_agent.tooling.admission import ToolAdmissionError
    selected=binding(synthetic_provider)
    selected.admission=replace(selected.admission,identity='f'*64)
    with pytest.raises(ToolAdmissionError,match='identity changed'):
        await selected.load_adapter()
    assert selected.owner is None
    await selected.aclose()

@pytest.mark.asyncio
async def test_real_meter_reader_failure_keeps_binding_disposal_pending(monkeypatch):
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    original = PeriodicExportingMetricReader.shutdown
    pending = [True]
    class TraceOwner:
        def force_flush(self, timeout_millis=5000): return True
        def shutdown(self): pass
    monkeypatch.setattr('api_agent.telemetry.tracing._sdk_factory',lambda *a, own, environ=None:(TraceOwner(),SimpleNamespace(start_span=lambda *a, **kw: None)))
    def reader_shutdown(reader, **kwargs):
        if pending[0]: raise RuntimeError('transient reader shutdown')
        return original(reader, **kwargs)
    monkeypatch.setattr(PeriodicExportingMetricReader,'shutdown',reader_shutdown)
    selected=admit_binding({'TRACE_PROVIDER':str(BASE/'src/api_agent/telemetry/trace_adapters/provider.py')+':create_tracing_provider','TRACE_EXPORT_ENDPOINT':'http://localhost:4318/v1/traces','JAEGER_VIEWER_PORT':16686,'TRACE_MODE':'OTEL','TRACE_METRICS':True,
        'TRACE_METRICS_ENDPOINT':'http://unused.invalid/metrics'},owner_root=BASE,boundary=EmissionBoundary())
    signals, _ = await selected.open({})
    with pytest.raises(RuntimeError,match='transient reader'): signals.shutdown()
    assert not signals.disposal_confirmed
    with pytest.raises(TracingProviderError,match='disposal remains pending'):
        await selected.aclose()
    assert not selected._closed
    pending[0] = False
    signals.shutdown()
    await selected.aclose()
    assert selected._closed and signals.disposal_confirmed
