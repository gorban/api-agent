"""Pinned GenAI metrics and explicit OTLP signal configuration; no SDK on import."""
from dataclasses import dataclass
import math
import os
import re
from threading import Lock, Condition
from types import MappingProxyType
from urllib.parse import urlsplit, unquote

SEMCONV_REVISION = '95aff9cdf605bbc7d7e8f7c4745a693bd0c4eea8'
DURATION_BUCKETS = tuple(0.01 * 2 ** index for index in range(14))
TOKEN_BUCKETS = tuple(4 ** index for index in range(14))


class MetricConfigurationError(ValueError):
    pass


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        if (type(value) is not str or len(value) > 2048 or parsed.scheme not in {'http', 'https'}
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError()
        parsed.port
    except (ValueError, TypeError, AttributeError):
        raise MetricConfigurationError('OTLP requires a valid HTTP(S) endpoint without embedded credentials') from None
    return value


def _headers(value):
    if isinstance(value, dict):
        rows = value
    elif type(value) is str:
        try:
            rows = dict(part.split('=', 1) for part in value.split(',') if part)
            rows = {unquote(key): unquote(item) for key, item in rows.items()}
        except ValueError:
            raise MetricConfigurationError('Invalid OTLP header configuration') from None
    else:
        raise MetricConfigurationError('Invalid OTLP header configuration')
    if (len(rows) > 32 or any(type(key) is not str or type(item) is not str
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", key) is None or len(item) > 4096
            or item.startswith((' ', '\t'))
            or any(ord(char) < 32 or ord(char) > 126 for char in key + item)
            for key, item in rows.items())):
        raise MetricConfigurationError('Invalid OTLP header configuration')
    return MappingProxyType(dict(rows))


def signal_endpoint(values, declaration, signal, *, environ=None, explicit_fields=None, required=True):
    """One signal URL precedence rule; optional viewing never validates export policy."""
    environ = os.environ if environ is None else environ
    environ = environ if declaration.otlp_environment else {}
    field = declaration.endpoint_field if signal == 'traces' else 'TRACE_METRICS_ENDPOINT'
    explicit = explicit_fields is None or field in explicit_fields
    try:
        if field in (values if explicit_fields is None else explicit_fields) and not values.get(field):
            raise MetricConfigurationError('An active tracing signal requires a nonempty explicit endpoint')
        prefix = 'OTEL_EXPORTER_OTLP_' + signal.upper()
        endpoint = (values.get(field) if explicit else None) or environ.get(prefix + '_ENDPOINT')
        if not endpoint and environ.get('OTEL_EXPORTER_OTLP_ENDPOINT') and (signal == 'traces' or declaration.metrics_from_base):
            endpoint = environ['OTEL_EXPORTER_OTLP_ENDPOINT'].rstrip('/') + '/v1/' + signal
        endpoint = endpoint or values.get(field)
        if not endpoint:
            raise MetricConfigurationError('Configure an explicit supported OTLP signal endpoint')
        return _endpoint(endpoint)
    except MetricConfigurationError:
        if required:
            raise
        return None


def export_plan(values, declaration, resource, *, environ=None, explicit_fields=None):
    from .tracing_providers import ExportPlan
    environ = os.environ if environ is None else environ
    if environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true' and declaration.signals:
        raise MetricConfigurationError('OTEL SDK disable policy conflicts with selected export signals')
    environ = environ if declaration.otlp_environment else {}
    metrics = bool(values.get('TRACE_METRICS'))
    if metrics and 'metrics' not in declaration.signals:
        raise MetricConfigurationError('Selected tracing provider does not admit metric export')
    def explicit(field):
        return values.get(field) if explicit_fields is None or field in explicit_fields else None
    endpoints, headers = {}, {}
    for signal in ('traces', 'metrics'):
        if signal not in declaration.signals or signal == 'metrics' and not metrics:
            endpoints[signal] = None
            continue
        prefix = 'OTEL_EXPORTER_OTLP_' + signal.upper()
        endpoints[signal] = signal_endpoint(values, declaration, signal, environ=environ, explicit_fields=explicit_fields)
        protocol = explicit('TRACE_EXPORT_PROTOCOL') or environ.get(prefix + '_PROTOCOL') or environ.get('OTEL_EXPORTER_OTLP_PROTOCOL') or values.get('TRACE_EXPORT_PROTOCOL') or 'http/protobuf'
        if protocol != 'http/protobuf':
            raise MetricConfigurationError('This tracing binding supports only OTLP HTTP/protobuf')
        header_explicit = 'TRACE_EXPORT_HEADERS' in values and (explicit_fields is None or 'TRACE_EXPORT_HEADERS' in explicit_fields)
        selected_headers = values['TRACE_EXPORT_HEADERS'] if header_explicit else environ.get(prefix + '_HEADERS') or environ.get('OTEL_EXPORTER_OTLP_HEADERS') or values.get('TRACE_EXPORT_HEADERS') or ''
        headers[signal] = _headers(selected_headers)
    temporality = (explicit('TRACE_METRICS_TEMPORALITY') or environ.get('OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE') or values.get('TRACE_METRICS_TEMPORALITY') or 'CUMULATIVE').upper() if metrics else 'CUMULATIVE'
    if temporality not in {'DELTA', 'CUMULATIVE'}:
        raise MetricConfigurationError('Metric temporality must be DELTA or CUMULATIVE')
    return ExportPlan(endpoints.get('traces'), endpoints.get('metrics'), 'http/protobuf', temporality,
                      MappingProxyType(dict(resource)), MappingProxyType(headers))


class GenAIMetrics:
    """Terminal logical duration and known physical-attempt usage, never parent sums."""
    def __init__(self, meter, *, profile='current'):
        if profile not in {'current', 'legacy'}:
            raise MetricConfigurationError('Unsupported pinned GenAI metric profile')
        self.profile, self._seen, self._lock = profile, set(), Lock()
        self._physical_seen = set()
        self._label_sets = set()
        self.incomplete_calls = 0
        self.incomplete = meter.create_counter('api_agent.gen_ai.usage.incomplete', unit='{call}')
        name = 'gen_ai.client.inference.duration' if profile == 'current' else 'gen_ai.client.operation.duration'
        self.duration = meter.create_histogram(name, unit='s', explicit_bucket_boundaries_advisory=DURATION_BUCKETS)
        self.tokens = {}
        self.extra_tokens = {}
        if profile == 'current':
            for direction in ('input', 'output'):
                self.tokens[direction] = (
                    meter.create_counter('gen_ai.client.inference.usage.' + direction + '_tokens', unit='{token}'),
                    meter.create_histogram('gen_ai.client.inference.operation.' + direction + '_tokens', unit='{token}', explicit_bucket_boundaries_advisory=TOKEN_BUCKETS))
            for source, suffix in (('cached_input_tokens', 'cache_read.input_tokens'),
                                   ('cache_write_tokens', 'cache_write.input_tokens'),
                                   ('reasoning_tokens', 'reasoning.output_tokens')):
                self.extra_tokens[source] = meter.create_counter('gen_ai.client.inference.usage.' + suffix, unit='{token}')
        else:
            self.legacy_tokens = meter.create_histogram('gen_ai.client.token.usage', unit='{token}', explicit_bucket_boundaries_advisory=TOKEN_BUCKETS)

    def record_call(self, structural, elapsed, outcome, *, attempts=None):
        if (type(structural) is not dict or structural.get('gen_ai.operation.name') not in {'chat', 'generate_content', 'text_completion'}
                or elapsed is not None and (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0)
                or outcome not in {'success', 'failure', 'cancelled', 'partial', 'unknown', 'completed', 'failed', 'length', 'refused', 'tool_calls'}):
            raise MetricConfigurationError('Metric records require an admitted terminal inference operation')
        call_id = structural.get('api_agent.call_id')
        if type(call_id) is not str or not 1 <= len(call_id) <= 128:
            raise MetricConfigurationError('Metric records require a bounded authoritative call identity')
        if attempts is not None:
            from api_agent.providers.generation_attempts import validate_transport_attempts
            ledger = validate_transport_attempts(attempts)
            measurements = [(('physical', row['id']), row['usage']) for row in ledger['attempts'] if row['execution'] != 'not_started']
            complete = ledger['complete'] and all(usage is not None for _, usage in measurements)
        else:
            measurements = [(('aggregate', call_id), {direction + '_tokens': structural.get('gen_ai.usage.' + direction + '_tokens') for direction in ('input', 'output')})]
            complete = True
        # SDK labels never contain arbitrary payloads, user content, call IDs or errors.
        labels = {key: structural[key] for key in ('gen_ai.operation.name', 'gen_ai.provider.name',
                  'gen_ai.request.model', 'gen_ai.response.model') if type(structural.get(key)) is str}
        labels['api_agent.outcome'] = outcome
        for _, usage in measurements:
            if usage is None:
                continue
            for direction in ('input', 'output'):
                value = usage.get(direction + '_tokens')
                if value is not None and (type(value) is not int or not 0 <= value <= 10 ** 15):
                    raise MetricConfigurationError('Metric token usage must be a known bounded nonnegative integer')
                complete = complete and value is not None
        with self._lock:
            if call_id in self._seen:
                return
            if len(self._seen) >= 100000 or len(self._physical_seen | {identity for identity, _ in measurements}) > 100000:
                raise MetricConfigurationError('Metric identity capacity exhausted; start a new operation owner')
            label_identity = tuple(sorted(labels.items()))
            if label_identity not in self._label_sets and len(self._label_sets) >= 256:
                raise MetricConfigurationError('Metric label capacity exhausted; start a new operation owner')
            self._label_sets.add(label_identity)
            # Reserve before SDK calls: a failed exporter cannot cause retry double counting.
            self._seen.add(call_id)
            if not complete:
                self.incomplete_calls += 1
                self.incomplete.add(1, attributes=labels)
            if elapsed is not None:
                self.duration.record(elapsed, attributes=labels)
            for identity, usage in measurements:
                if identity in self._physical_seen:
                    continue
                self._physical_seen.add(identity)
                for direction in ('input', 'output'):
                    value = (usage or {}).get(direction + '_tokens')
                    if value is None:
                        continue
                    if self.profile == 'current':
                        counter, histogram = self.tokens[direction]
                        counter.add(value, attributes=labels)
                        histogram.record(value, attributes=labels)
                    else:
                        self.legacy_tokens.record(value, attributes={**labels, 'gen_ai.token.type': direction})
                for source, instrument in self.extra_tokens.items():
                    value = (usage or {}).get(source)
                    if value is not None:
                        instrument.add(value, attributes=labels)


def sdk_meter(plan, *, own=None):
    """Retain startup resources even when SDK construction or rollback fails."""
    if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        raise MetricConfigurationError('Ambient SDK disable policy conflicts with metric export')
    retained = []
    def retain(owner):
        retained.append(owner)
        if own is not None:
            own(owner)
    try:
        return _sdk_meter(plan, own=retain)
    except BaseException as error:
        if retained:
            error.cleanup_owner = retained[0]
            if own is None:
                try:
                    retained[0].shutdown()
                except BaseException:
                    pass
        raise


def _sdk_meter(plan, *, own):
    """One non-global meter with explicit signal destination and delivery evidence."""
    from threading import Event
    from requests import Session
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import encode_metrics
    from opentelemetry.sdk.metrics import MeterProvider, Counter, Histogram
    from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExporter, MetricExportResult, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    class MeterOwner:
        def __init__(self):
            self.provider = self.reader = self.exporter = self.session = None
            self.acknowledged = set()
            self.disposed = False
        def force_flush(self, timeout_millis=5000):
            return self.provider.force_flush(timeout_millis=timeout_millis) if self.provider is not None else True
        def shutdown(self):
            if self.disposed:
                return
            # Lower resources acknowledge independently before one-shot SDK owners.
            # A provider/reader no-op retry cannot stand in for exporter/session drain.
            for name in ('exporter', 'reader', 'provider', 'session'):
                if name in self.acknowledged:
                    continue
                component = getattr(self, name)
                if component is not None:
                    if name == 'session':
                        component.close()
                    else:
                        component.shutdown(timeout_millis=5000)
                self.acknowledged.add(name)
            self.disposed = True
    owner = MeterOwner()
    own(owner)
    session = Session()
    owner.session = session
    session.trust_env = False
    failed = Event()
    temporality = getattr(AggregationTemporality, plan.temporality)

    class OwnedExporter(MetricExporter):
        def __init__(self):
            super().__init__(preferred_temporality={Counter: temporality, Histogram: temporality})
            self.closed = False
            self._active = 0
            self._condition = Condition()

        def export(self, metrics_data, timeout_millis=10000, **kwargs):
            with self._condition:
                if self.closed:
                    failed.set()
                    return MetricExportResult.FAILURE
                self._active += 1
            try:
                body = encode_metrics(metrics_data).SerializeToString()
                response = session.post(plan.metric_endpoint, data=body,
                    headers={**dict(plan.headers.get('metrics', {})), 'Content-Type': 'application/x-protobuf'},
                    timeout=min(5, timeout_millis / 1000), allow_redirects=False, stream=True)
                try:
                    if 200 <= response.status_code < 300:
                        return MetricExportResult.SUCCESS
                finally:
                    response.close()
            except Exception:
                pass
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
            failed.set()
            return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis=5000):
            return not failed.is_set()

        def shutdown(self, timeout_millis=5000, **kwargs):
            with self._condition:
                self.closed = True
                if not self._condition.wait_for(lambda: self._active == 0, timeout=max(0, timeout_millis) / 1000):
                    raise RuntimeError('Metric export disposal remains pending')

    exporter = OwnedExporter()
    owner.exporter = exporter
    # Public SDK no-ticker mode: existing runtime/session flush owns delivery.
    # No background reader thread or second application scheduler is created.
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf, export_timeout_millis=5000)
    owner.reader = reader

    class OwnedMeterProvider(MeterProvider):
        def force_flush(self, timeout_millis=5000):
            drained = super().force_flush(timeout_millis=timeout_millis)
            return drained and not failed.is_set()

    provider = OwnedMeterProvider(metric_readers=[reader], resource=Resource(dict(plan.resource)), shutdown_on_exit=False)
    owner.provider = provider
    meter = provider.get_meter('api_agent.genai', version=SEMCONV_REVISION)
    return owner, meter


def resource_plan(values, boundary, resource=None, *, environ=None):
    """Finite supported resource identities; no detector or process-global state."""
    from .privacy import ContentPolicy
    environ = os.environ if environ is None else environ
    supported = {'service.name', 'service.namespace', 'service.version', 'deployment.environment.name'}
    try:
        attributes = dict(part.split('=', 1) for part in environ.get('OTEL_RESOURCE_ATTRIBUTES', '').split(',') if part)
        attributes = {unquote(key): unquote(value) for key, value in attributes.items()}
    except ValueError:
        raise MetricConfigurationError('Invalid resource attribute configuration') from None
    selected = dict(attributes)
    if environ.get('OTEL_SERVICE_NAME'):
        selected['service.name'] = environ['OTEL_SERVICE_NAME']
    configured = values.get('TRACE_RESOURCE_ATTRIBUTES', {})
    if not isinstance(configured, dict):
        raise MetricConfigurationError('Resource attributes require a typed object')
    selected.update(configured)
    if (set(selected) - supported or len(selected) > 4
            or any(type(value) is not str or not 0 < len(value) <= 200
                   or not value.isprintable() for value in selected.values())):
        raise MetricConfigurationError('Only bounded service and deployment resource identities are supported')
    safe = boundary.for_policy(ContentPolicy(True)).export(selected)
    if safe != selected:
        raise MetricConfigurationError('Resource identities may not contain credential values')
    return {'service.name': 'api-agent', **dict(resource or {}), **safe}
