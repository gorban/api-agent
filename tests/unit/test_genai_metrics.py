"""Pinned metrics distinguish logical duration from all known physical usage."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from api_agent.telemetry.genai_metrics import GenAIMetrics, MetricConfigurationError, export_plan, resource_plan
from api_agent.telemetry.tracing_providers import TracingDeclaration, NativeMapping
from api_agent.telemetry.privacy import EmissionBoundary


class Instrument:
    def __init__(self): self.rows = []
    def record(self, value, *, attributes): self.rows.append((value, attributes))
    def add(self, value, *, attributes): self.rows.append((value, attributes))


class Meter:
    def __init__(self): self.instruments, self.units = {}, {}
    def create_histogram(self, name, unit, **kwargs):
        self.units[name] = unit
        self.instruments[name] = Instrument()
        return self.instruments[name]
    def create_counter(self, name, unit): return self.create_histogram(name, unit)


def declaration(signals=('traces', 'metrics'), **kwargs):
    return TracingDeclaration('synthetic', frozenset(signals), NativeMapping(), (), None, (), None, **kwargs)


def call(**kwargs):
    return {'gen_ai.operation.name': 'chat', 'api_agent.call_id': 'call-1',
            'gen_ai.usage.input_tokens': 7, 'gen_ai.usage.output_tokens': 2, **kwargs}


def ledger(first_usage=5):
    from api_agent.providers.generation_attempts import TransportAttemptLedger
    from api_agent.providers.contracts import Usage
    attempts = TransportAttemptLedger(2)
    first = attempts.begin(1)
    error = SimpleNamespace(kind='server', execution='possibly_executed', http_status=503,
                            usage=Usage(first_usage, 0) if first_usage is not None else None)
    attempts.finish(first, error=error, retry_delay_seconds=0.2)
    final = attempts.begin(2)
    attempts.finish(final, result=SimpleNamespace(usage=Usage(7, 2)))
    return attempts.snapshot()


@pytest.mark.parametrize('profile', ['current', 'legacy'])
def test_all_known_failed_and_successful_attempts_count_once(profile):
    meter = Meter()
    metrics = GenAIMetrics(meter, profile=profile)
    metrics.record_call(call(), 1.5, 'success', attempts=ledger())
    metrics.record_call(call(), 1.5, 'success', attempts=ledger())
    duration = 'gen_ai.client.inference.duration' if profile == 'current' else 'gen_ai.client.operation.duration'
    assert len(meter.instruments[duration].rows) == 1
    if profile == 'current':
        assert sum(value for value, labels in meter.instruments['gen_ai.client.inference.usage.input_tokens'].rows) == 12
        assert [value for value, labels in meter.instruments['gen_ai.client.inference.operation.input_tokens'].rows] == [5, 7]
    else:
        assert sum(value for value, labels in meter.instruments['gen_ai.client.token.usage'].rows if labels['gen_ai.token.type'] == 'input') == 12
    assert metrics.incomplete_calls == 0
    assert meter.units[duration] == 's'


def test_unknown_attempt_usage_remains_incomplete_without_zero_fabrication():
    meter = Meter()
    metrics = GenAIMetrics(meter)
    metrics.record_call(call(), 1, 'success', attempts=ledger(None))
    assert metrics.incomplete_calls == 1
    assert [value for value, labels in meter.instruments['gen_ai.client.inference.usage.input_tokens'].rows] == [7]


def test_no_ledger_uses_known_leaf_usage_without_content_labels():
    meter = Meter()
    metrics = GenAIMetrics(meter)
    metrics.record_call(call(prompt='not-a-label', **{'gen_ai.request.model': 'synthetic-model'}), 0, 'failure')
    labels = meter.instruments['gen_ai.client.inference.duration'].rows[0][1]
    assert labels == {'gen_ai.operation.name': 'chat', 'gen_ai.request.model': 'synthetic-model', 'api_agent.outcome': 'failure'}
    assert all('call-1' not in str(row[1]) for instrument in meter.instruments.values() for row in instrument.rows)
    metrics.record_call(call(**{'api_agent.call_id':'unknown', 'gen_ai.usage.input_tokens':None}), 1, 'unknown')
    assert metrics.incomplete_calls == 1


@pytest.mark.parametrize('changes', [
    {'gen_ai.operation.name':'execute_tool'}, {'api_agent.call_id':''},
    {'gen_ai.usage.input_tokens':True}, {'gen_ai.usage.output_tokens':-1},
])
def test_invalid_metric_records_reject_before_instrument_queue(changes):
    meter = Meter()
    metrics = GenAIMetrics(meter)
    with pytest.raises(MetricConfigurationError): metrics.record_call(call(**changes), 1, 'success')
    assert all(not instrument.rows for instrument in meter.instruments.values())


@pytest.mark.parametrize('elapsed', [True, -1, float('nan'), float('inf')])
def test_invalid_elapsed_rejects(elapsed):
    with pytest.raises(MetricConfigurationError): GenAIMetrics(Meter()).record_call(call(), elapsed, 'success')


def test_metric_profile_and_identity_capacity_are_explicit():
    with pytest.raises(MetricConfigurationError): GenAIMetrics(Meter(), profile='automatic')
    metrics = GenAIMetrics(Meter())
    metrics._seen = set(str(i) for i in range(100000))
    with pytest.raises(MetricConfigurationError, match='capacity'): metrics.record_call(call(), 1, 'success')


def test_signal_specific_configuration_and_explicit_application_precedence():
    env = {'OTEL_EXPORTER_OTLP_ENDPOINT':'https://base.test/root',
           'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT':'https://traces.test/custom',
           'OTEL_EXPORTER_OTLP_METRICS_ENDPOINT':'https://metrics.test/custom',
           'OTEL_EXPORTER_OTLP_HEADERS':'Authorization=shared',
           'OTEL_EXPORTER_OTLP_TRACES_HEADERS':'Authorization=trace-only',
           'OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE':'delta'}
    plan = export_plan({'TRACE_METRICS':True}, declaration(), {'service.name':'same'}, environ=env)
    assert plan.trace_endpoint == 'https://traces.test/custom'
    assert plan.metric_endpoint == 'https://metrics.test/custom'
    assert plan.headers['traces']['Authorization'] == 'trace-only'
    assert plan.headers['metrics']['Authorization'] == 'shared'
    assert plan.temporality == 'DELTA'
    explicit = export_plan({'TRACE_METRICS':True, 'TRACE_EXPORT_ENDPOINT':'https://app.test/traces',
                            'TRACE_METRICS_ENDPOINT':'https://app.test/metrics', 'TRACE_METRICS_TEMPORALITY':'CUMULATIVE'}, declaration(), {}, environ=env)
    assert explicit.trace_endpoint == 'https://app.test/traces'
    assert explicit.metric_endpoint == 'https://app.test/metrics'
    assert explicit.temporality == 'CUMULATIVE'


def test_metric_signal_url_is_never_inferred_from_trace_only_destination():
    env = {'OTEL_EXPORTER_OTLP_ENDPOINT':'https://base.test'}
    with pytest.raises(MetricConfigurationError, match='explicit'):
        export_plan({'TRACE_METRICS':True}, declaration(), {}, environ=env)
    plan = export_plan({'TRACE_METRICS':True}, declaration(metrics_from_base=True), {}, environ=env)
    assert plan.trace_endpoint == 'https://base.test/v1/traces'
    assert plan.metric_endpoint == 'https://base.test/v1/metrics'
    with pytest.raises(MetricConfigurationError, match='does not admit'):
        export_plan({'TRACE_METRICS':True}, declaration(signals=('traces',)), {}, environ=env)


@pytest.mark.parametrize('endpoint', ['https://user:secret@host.test/path', 'http://host.test/?token=x', 'ftp://host.test', 'http://host.test:bad/path', 'bad', 1])
def test_invalid_endpoint_rejects_without_echoing_secrets(endpoint):
    with pytest.raises(MetricConfigurationError) as error:
        export_plan({'TRACE_EXPORT_ENDPOINT':endpoint}, declaration(signals=('traces',)), {}, environ={})
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('env', [
    {'OTEL_EXPORTER_OTLP_PROTOCOL':'grpc'},
    {'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL':'http/json'},
    {'OTEL_EXPORTER_OTLP_HEADERS':'broken'},
    {'OTEL_EXPORTER_OTLP_HEADERS':'Authorization=bad%0Aheader'},
])
def test_unsupported_otlp_configuration_rejects_before_allocation(env):
    with pytest.raises(MetricConfigurationError):
        export_plan({'TRACE_EXPORT_ENDPOINT':'https://collector.test/v1/traces'}, declaration(signals=('traces',)), {}, environ=env)


def test_resource_precedence_is_shared_and_finite():
    boundary = EmissionBoundary()
    env = {'OTEL_RESOURCE_ATTRIBUTES':'service.name=attributes,service.namespace=synthetic', 'OTEL_SERVICE_NAME':'service-env'}
    plan = resource_plan({}, boundary, environ=env)
    assert plan == {'service.name':'service-env', 'service.namespace':'synthetic'}
    assert resource_plan({'TRACE_RESOURCE_ATTRIBUTES':{'service.name':'explicit'}}, boundary, environ=env)['service.name'] == 'explicit'
    with pytest.raises(MetricConfigurationError): resource_plan({}, boundary, environ={'OTEL_RESOURCE_ATTRIBUTES':'custom.payload=hidden'})
    boundary.register_secret('registered-secret')
    with pytest.raises(MetricConfigurationError, match='credential'):
        resource_plan({'TRACE_RESOURCE_ATTRIBUTES':{'service.name':'registered-secret'}}, boundary, environ={})


def test_inactive_metric_settings_do_not_change_trace_only_export():
    env = {'OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE':'LOWMEMORY'}
    values = {'TRACE_EXPORT_ENDPOINT':'https://collector.test/traces'}
    assert export_plan(values, declaration(signals=('traces',)), {}, environ=env).temporality == 'CUMULATIVE'
    with pytest.raises(MetricConfigurationError, match='temporality'):
        export_plan({**values, 'TRACE_METRICS':True, 'TRACE_METRICS_ENDPOINT':'https://collector.test/metrics'}, declaration(), {}, environ=env)


def test_endpoint_precedence_uses_provenance_not_equality_to_default():
    env = {'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT':'https://environment.test/traces'}
    values = {'TRACE_EXPORT_ENDPOINT':'http://localhost:4318/v1/traces'}
    inherited = export_plan(values, declaration(signals=('traces',)), {}, environ=env, explicit_fields=frozenset())
    assert inherited.trace_endpoint == 'https://environment.test/traces'
    explicit = export_plan(values, declaration(signals=('traces',)), {}, environ=env, explicit_fields={'TRACE_EXPORT_ENDPOINT'})
    assert explicit.trace_endpoint == values['TRACE_EXPORT_ENDPOINT']
    assert export_plan(values, declaration(signals=('traces',)), {}, environ=env).trace_endpoint == values['TRACE_EXPORT_ENDPOINT']


def test_backend_compatibility_cannot_be_silently_retargeted_by_standard_otel_environment():
    declared = declaration(signals=('traces',), otlp_environment=False)
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'https://backend.test'}, declared, {},
                       environ={'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT':'https://other.test', 'OTEL_EXPORTER_OTLP_HEADERS':'Authorization=unrelated'})
    assert plan.trace_endpoint == 'https://backend.test'
    assert not plan.headers['traces']
    with pytest.raises(MetricConfigurationError, match='disable'):
        export_plan({'TRACE_EXPORT_ENDPOINT':'https://backend.test'}, declared, {}, environ={'OTEL_SDK_DISABLED':'true'})


def test_export_plan_repr_cannot_disclose_endpoints_headers_or_resources():
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'https://sensitive-host.test/private-path',
                        'TRACE_EXPORT_HEADERS':'Authorization=exporter-secret'}, declaration(signals=('traces',)),
                       {'service.name':'private-service'}, environ={})
    assert 'exporter-secret' not in repr(plan)
    assert 'sensitive-host' not in repr(plan)
    assert 'private-service' not in repr(plan)


def test_replayed_physical_attempt_is_not_counted_under_another_logical_callback():
    meter = Meter()
    metrics = GenAIMetrics(meter)
    attempts = ledger()
    metrics.record_call(call(), 1, 'success', attempts=attempts)
    metrics.record_call(call(**{'api_agent.call_id':'another-call'}), 1, 'success', attempts=attempts)
    assert sum(value for value, _ in meter.instruments['gen_ai.client.inference.usage.input_tokens'].rows) == 12
    assert len(meter.instruments['gen_ai.client.inference.duration'].rows) == 2


@pytest.mark.parametrize('temporality', ['DELTA', 'CUMULATIVE'])
def test_real_metric_payload_preserves_resources_units_buckets_and_known_attempts(temporality):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from api_agent.telemetry.genai_metrics import sdk_meter, DURATION_BUCKETS, TOKEN_BUCKETS
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, ExportMetricsServiceRequest.FromString(body)))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':endpoint + '/traces', 'TRACE_METRICS':True,
                        'TRACE_METRICS_ENDPOINT':endpoint + '/explicit-metrics', 'TRACE_METRICS_TEMPORALITY':temporality},
                       declaration(), {'service.name':'synthetic-meter', 'service.version':'1'}, environ={})
    provider, meter = sdk_meter(plan)
    try:
        metrics = GenAIMetrics(meter)
        metrics.record_call(call(), 1.5, 'success', attempts=ledger())
        assert provider.force_flush()
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()
        worker.join()
    assert requests[0][0] == '/explicit-metrics'
    payload = requests[0][1]
    resource = payload.resource_metrics[0]
    assert {row.key:row.value.string_value for row in resource.resource.attributes} == dict(plan.resource)
    instruments = {row.name:row for scope in resource.scope_metrics for row in scope.metrics}
    counter = instruments['gen_ai.client.inference.usage.input_tokens']
    histogram = instruments['gen_ai.client.inference.operation.input_tokens']
    duration = instruments['gen_ai.client.inference.duration']
    expected_temporality = 1 if temporality == 'DELTA' else 2
    assert counter.sum.aggregation_temporality == expected_temporality
    assert counter.sum.data_points[0].as_int == 12
    assert histogram.histogram.data_points[0].sum == 12
    assert histogram.histogram.data_points[0].count == 2
    assert tuple(histogram.histogram.data_points[0].explicit_bounds) == TOKEN_BUCKETS
    assert tuple(duration.histogram.data_points[0].explicit_bounds) == DURATION_BUCKETS
    assert duration.unit == 's'
    assert counter.unit == '{token}'


def test_unknown_duration_does_not_discard_known_physical_usage():
    meter = Meter()
    metrics = GenAIMetrics(meter)
    metrics.record_call(call(), None, 'success', attempts=ledger())
    assert not meter.instruments['gen_ai.client.inference.duration'].rows
    assert sum(value for value, _ in meter.instruments['gen_ai.client.inference.usage.input_tokens'].rows) == 12


def test_metric_label_cardinality_is_bounded_before_sdk_queue():
    meter=Meter()
    metrics=GenAIMetrics(meter)
    metrics._label_sets={((f'key-{i}','synthetic'),) for i in range(256)}
    with pytest.raises(MetricConfigurationError,match='label capacity'):
        metrics.record_call(call(),1,'success')
    assert all(not instrument.rows for instrument in meter.instruments.values())

@pytest.mark.parametrize('source', ['OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS'])
@pytest.mark.parametrize('explicit', [None, frozenset({'TRACE_EXPORT_ENDPOINT', 'TRACE_EXPORT_HEADERS'})])
def test_explicit_empty_headers_clear_inherited_authentication(source, explicit):
    values = {'TRACE_EXPORT_ENDPOINT': 'http://collector/traces', 'TRACE_EXPORT_HEADERS': ''}
    plan = export_plan(values, declaration(), {}, environ={source: 'Authorization=synthetic-inherited'}, explicit_fields=explicit)
    assert dict(plan.headers['traces']) == {}
    inherited = export_plan(values, declaration(), {}, environ={source: 'Authorization=synthetic-inherited'}, explicit_fields=frozenset({'TRACE_EXPORT_ENDPOINT'}))
    assert inherited.headers['traces']['Authorization'] == 'synthetic-inherited'

@pytest.mark.parametrize('value', [3, {'bad header': 'value'}, {'X-Key': '\nvalue'}])
def test_headers_reject_untyped_or_unsafe_values(value):
    with pytest.raises(MetricConfigurationError, match='header'):
        export_plan({'TRACE_EXPORT_ENDPOINT':'http://collector/traces', 'TRACE_EXPORT_HEADERS':value}, declaration(), {}, environ={})

@pytest.mark.parametrize('values,environment', [({}, {'OTEL_RESOURCE_ATTRIBUTES':'broken'}), ({'TRACE_RESOURCE_ATTRIBUTES':[]}, {})])
def test_resource_configuration_rejects_malformed_typed_inputs(values, environment):
    with pytest.raises(MetricConfigurationError):
        resource_plan(values, EmissionBoundary(), environ=environment)


def test_extra_known_token_counters_are_reported_once():
    meter = Meter()
    metrics = GenAIMetrics(meter)
    attempts = ledger()
    attempts['attempts'][0]['usage']['cached_input_tokens'] = 3
    attempts['attempts'][1]['usage']['reasoning_tokens'] = 1
    metrics.record_call(call(), 1, 'success', attempts=attempts)
    assert meter.instruments['gen_ai.client.inference.usage.cache_read.input_tokens'].rows[0][0] == 3
    assert meter.instruments['gen_ai.client.inference.usage.reasoning.output_tokens'].rows[0][0] == 1


def test_metric_http_delivery_failures_and_disposal_are_owned(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricExportResult
    from api_agent.telemetry.genai_metrics import sdk_meter
    captured = {}
    class Session:
        def post(self, *args, **kwargs):
            if captured.get('raise'): raise RuntimeError('synthetic transport failure')
            return SimpleNamespace(status_code=captured.get('status', 200), close=lambda: captured.setdefault('response_closed', True))
        def close(self): captured['session_closed'] = True
    class Reader:
        def __init__(self, exporter, **kwargs): captured['exporter'] = exporter
        def shutdown(self, **kwargs): captured['exporter'].shutdown()
    monkeypatch.setattr('requests.Session', Session)
    monkeypatch.setattr('opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader', Reader)
    monkeypatch.setattr('opentelemetry.exporter.otlp.proto.http.metric_exporter.encode_metrics', lambda value: SimpleNamespace(SerializeToString=lambda: b'bounded'))
    monkeypatch.setattr(MeterProvider, '__init__', lambda self, **kwargs: None)
    monkeypatch.setattr(MeterProvider, 'get_meter', lambda *args, **kwargs: object())
    monkeypatch.setattr(MeterProvider, 'force_flush', lambda *args, **kwargs: True)
    monkeypatch.setattr(MeterProvider, 'shutdown', lambda self, **kwargs: captured['exporter'].shutdown())
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://collector/traces','TRACE_METRICS':True,
                       'TRACE_METRICS_ENDPOINT':'http://collector/metrics'}, declaration(), {}, environ={})
    owner, _ = sdk_meter(plan)
    exporter = captured['exporter']
    assert owner.force_flush() and exporter.force_flush()
    assert exporter.export(None) == MetricExportResult.SUCCESS
    captured['status'] = 503
    assert exporter.export(None) == MetricExportResult.FAILURE
    captured['raise'] = True
    assert exporter.export(None) == MetricExportResult.FAILURE
    assert not owner.force_flush() and not exporter.force_flush()
    owner.shutdown()
    owner.shutdown()
    assert exporter.export(None) == MetricExportResult.FAILURE
    assert captured['session_closed'] and captured['response_closed']


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_standalone_meter_startup_failure_exposes_cleanup_receipt(monkeypatch, cleanup_fails):
    from api_agent.telemetry.genai_metrics import sdk_meter
    class Session:
        def close(self):
            if cleanup_fails: raise RuntimeError('cleanup pending')
    monkeypatch.setattr('requests.Session', Session)
    def fail(*args, **kwargs): raise RuntimeError('reader construction')
    monkeypatch.setattr('opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader', fail)
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://collector/traces','TRACE_METRICS':True,
                       'TRACE_METRICS_ENDPOINT':'http://collector/metrics'}, declaration(), {}, environ={})
    with pytest.raises(RuntimeError, match='reader construction') as caught:
        sdk_meter(plan)
    owner = caught.value.cleanup_owner
    assert owner.force_flush()
    assert owner.disposed is not cleanup_fails


def test_meter_import_or_registration_failure_preserves_original_error(monkeypatch):
    import api_agent.telemetry.genai_metrics as module
    def fail(*args, **kwargs): raise ImportError('SDK unavailable')
    monkeypatch.setattr(module, '_sdk_meter', fail)
    with pytest.raises(ImportError, match='SDK unavailable'):
        module.sdk_meter(None)


def test_sdk_owner_registration_failure_has_empty_retryable_receipt():
    from api_agent.telemetry.genai_metrics import sdk_meter
    def reject(owner): raise RuntimeError('owner admission failed')
    with pytest.raises(RuntimeError, match='owner admission') as caught:
        sdk_meter(None, own=reject)
    receipt = caught.value.cleanup_owner
    receipt.shutdown()
    assert receipt.disposed

@pytest.mark.parametrize('phase', ['exporter', 'reader', 'provider', 'session'])
@pytest.mark.parametrize('timing', ['before', 'after'])
def test_real_sdk_cleanup_retains_each_lower_phase_until_retry(monkeypatch, phase, timing):
    import threading
    from api_agent.telemetry.genai_metrics import sdk_meter
    before = {thread.ident for thread in threading.enumerate()}
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://unused.invalid/traces', 'TRACE_METRICS':True,
        'TRACE_METRICS_ENDPOINT':'http://unused.invalid/metrics'}, declaration(), {}, environ={})
    owner, _ = sdk_meter(plan)
    # The public infinite-interval SDK mode creates no periodic worker.
    assert {thread.ident for thread in threading.enumerate()} == before
    component = getattr(owner, phase)
    method = 'close' if phase == 'session' else 'shutdown'
    original = getattr(component, method)
    first = True
    def transient(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            if timing == 'after': original(*args, **kwargs)
            raise RuntimeError('transient owned cleanup failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(component, method, transient)
    with pytest.raises(RuntimeError, match='transient owned cleanup'):
        owner.shutdown()
    assert not owner.disposed and phase not in owner.acknowledged
    owner.shutdown()
    assert owner.disposed
    assert owner.acknowledged == {'exporter', 'reader', 'provider', 'session'}
    assert {thread.ident for thread in threading.enumerate()} == before


def test_real_sdk_reader_failure_is_retried_before_provider_one_shot(monkeypatch):
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from api_agent.telemetry.genai_metrics import sdk_meter
    original = PeriodicExportingMetricReader.shutdown
    calls = []
    def fail_once(reader, **kwargs):
        calls.append(reader)
        if len(calls) == 1: raise RuntimeError('transient reader failure')
        return original(reader, **kwargs)
    monkeypatch.setattr(PeriodicExportingMetricReader, 'shutdown', fail_once)
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://unused.invalid/traces','TRACE_METRICS':True,
        'TRACE_METRICS_ENDPOINT':'http://unused.invalid/metrics'}, declaration(), {}, environ={})
    owner, _ = sdk_meter(plan)
    with pytest.raises(RuntimeError, match='transient reader failure'): owner.shutdown()
    assert not owner.disposed and 'provider' not in owner.acknowledged
    owner.shutdown()
    assert owner.disposed and len(calls) >= 2


def test_real_sdk_active_export_drains_before_session_disposal(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from api_agent.telemetry.genai_metrics import sdk_meter
    entered, release, closing = Event(), Event(), Event()
    session_closed = []
    class Session:
        def post(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return SimpleNamespace(status_code=200, close=lambda: None)
        def close(self): session_closed.append(True)
    monkeypatch.setattr('requests.Session', Session)
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'http://unused.invalid/traces','TRACE_METRICS':True,
        'TRACE_METRICS_ENDPOINT':'http://unused.invalid/metrics'}, declaration(), {}, environ={})
    owner, meter = sdk_meter(plan)
    GenAIMetrics(meter).record_call(call(), 1, 'success', attempts=ledger())
    with ThreadPoolExecutor(max_workers=2) as workers:
        flush = workers.submit(owner.force_flush)
        assert entered.wait(2)
        # Direct exporter shutdown has the same bounded drain used by owner close.
        with pytest.raises(RuntimeError, match='disposal remains pending'):
            owner.exporter.shutdown(timeout_millis=1)
        assert not session_closed and not owner.disposed
        def shutdown():
            closing.set()
            owner.shutdown()
        close = workers.submit(shutdown)
        assert closing.wait(2) and not close.done()
        assert not session_closed
        release.set()
        assert flush.result(timeout=3)
        close.result(timeout=3)
    assert owner.disposed and session_closed
