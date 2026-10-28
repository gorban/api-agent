"""Manual, runtime-owned OTLP tracing; payloads cross privacy before SDK creation.

No global provider, automatic instrumentation, environment resource detectors or
SDK logging hooks are installed. An ambient OTEL_SDK_DISABLED=true conflicts
with enabled tracing and fails initialization without allocating SDK resources. The HTTP adapter uses the official OTLP encoder
but owns transport diagnostics so backend response bodies cannot reach logs.
"""
import asyncio
from base64 import b64encode
from contextlib import contextmanager
import json
import os
import re
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from .privacy import ContentPolicy, EmissionBoundary


class TraceError(RuntimeError):
    """Fixed diagnostics deliberately omit SDK messages and credentials."""


class TraceDeliveryError(TraceError):
    """Delivery failed, but this tracing owner's disposal was acknowledged."""
    disposal_confirmed = True


TRACE_DELIVERY_WARNING = 'Trace delivery failed; tracing resources were closed. Some traces may be missing.'


def _native_array(value):
    if type(value) is not list:
        return False
    if not value:
        return True
    kind = type(value[0])
    if kind not in (str, bool, int, float) or any(type(item) is not kind for item in value):
        return False
    if kind is str:
        try:
            for item in value:
                item.encode('utf-8')
        except UnicodeEncodeError:
            return False
    return True


def _attributes(boundary, payload, *, model_identity=None):
    clean = boundary.export(payload)
    for key in ('gen_ai.request.model', 'gen_ai.response.model'):
        clean.pop(key, None)
        if type(payload) is dict and model_identity is not None:
            model = payload.get(key)
            if model_identity(model) is True:
                clean.update(boundary.for_policy(ContentPolicy(True)).export({key: model}))
    return {key: value if type(value) in (str, bool, int, float) or _native_array(value) else json.dumps(value)
            for key, value in clean.items()}



def _model_identity(value, model_identities):
    """Admit only exact identities explicitly established by the catalog owner."""
    return (type(value) is str and 0 < len(value) <= 200 and value.isprintable()
            and value in model_identities)


def _structural(boundary, payload, model_identities=frozenset()):
    """Only this versioned, bounded manual signal contract bypasses content gating."""
    if type(payload) is not dict:
        return {}
    clean = {}
    identifiers = {'api_agent.' + key for key in ('event_id', 'invocation_id', 'turn_id',
        'scope_id', 'parent_id', 'visit_id', 'attempt_id', 'call_id', 'transport_id')}
    for key, value in payload.items():
        if key in ('gen_ai.request.model', 'gen_ai.response.model'):
            if _model_identity(value, model_identities):
                clean[key] = value
        elif key == 'api_agent.request.reasoning_effort' and value in ('off', 'low', 'medium', 'high'):
            clean[key] = value
        elif key in identifiers:
            if type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
                clean[key] = value
        elif key in ('api_agent.attempt', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens'):
            if type(value) is int and 0 <= value <= (1 << 53) - 1:
                clean[key] = value
        elif key in ('api_agent.transport_index', 'api_agent.transport_attempt_count'):
            if type(value) is int and 0 <= value <= 20:
                clean[key] = value
        elif key == 'api_agent.transport_status':
            if type(value) is int and 100 <= value <= 599:
                clean[key] = value
        elif key in ('api_agent.transport_complete', 'api_agent.transport_recovered'):
            if type(value) is bool:
                clean[key] = value
        elif key == 'api_agent.transport_outcome' and value in ('started', 'observed', 'completed', 'failed'):
            clean[key] = value
        elif key == 'gen_ai.operation.name' and value in ('chat', 'execute_tool'):
            clean[key] = value
    return _attributes(boundary.for_policy(ContentPolicy(True)), clean,
        model_identity=lambda value: _model_identity(value, model_identities))


def _span_name(boundary, name, model_identities=frozenset()):
    if type(name) is str and name.startswith('chat ') and not _model_identity(name[5:], model_identities):
        return 'chat'
    if type(name) is not str or not (
            re.fullmatch(r'(?:graph|component|node) [A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', name)
            or name in ('execute_tool', 'transport attempt')
            or (name.startswith('chat ') and _model_identity(name[5:], model_identities))):
        return 'api_agent.span'
    return boundary.content_export({'name': name}).get('name', 'api_agent.span')

def _sdk_factory(endpoint, headers, resource):
    # Honor the SDK disable policy without silent data loss or modifying globals.
    if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        raise TraceError('Trace SDK disable flag conflicts with enabled tracing.')
    # Lazy imports: disabled tracing has no optional dependency or backend I/O.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
    from requests import Session

    class OwnedHTTPExporter(OTLPSpanExporter):
        def __init__(self):
            self.session = Session()
            self.session.trust_env = False
            self.closed = False
            self.failed = Event()

        def export(self, spans):
            if self.closed:
                self.failed.set()
                return SpanExportResult.FAILURE
            try:
                data = encode_spans(spans).SerializeToString()
                response = self.session.post(endpoint, data=data,
                    headers={**headers, 'Content-Type': 'application/x-protobuf'},
                    timeout=5, allow_redirects=False, stream=True)
                try:
                    if 200 <= response.status_code < 300:
                        return SpanExportResult.SUCCESS
                    self.failed.set()
                    return SpanExportResult.FAILURE
                finally:
                    response.close()
            except Exception:
                self.failed.set()
                return SpanExportResult.FAILURE

        def force_flush(self, timeout_millis=5000):
            return True

        def shutdown(self):
            self.closed = True
            self.session.close()

    exporter = OwnedHTTPExporter()
    class OwnedTracerProvider(TracerProvider):
        def force_flush(self, timeout_millis=5000):
            # SDK flush reports queue drainage, not successful delivery. Keep
            # failed delivery sticky for this runtime without backend text.
            drained = super().force_flush(timeout_millis=timeout_millis)
            return drained and not exporter.failed.is_set()

    provider = OwnedTracerProvider(resource=Resource({'service.name': 'api-agent', **resource}),
        sampler=ALWAYS_ON, shutdown_on_exit=False)
    provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=256,
        max_export_batch_size=32, schedule_delay_millis=500, export_timeout_millis=5000))
    return provider, provider.get_tracer('api_agent')


class TraceSpan:
    def __init__(self, span, boundary, model_identity=None):
        self._span = span
        self._boundary = boundary
        self._model_identity = model_identity

    def end(self, *, end_time=None):
        if self._span is not None:
            self._span.end(end_time=end_time)
            self._span = None

    def attributes(self, payload):
        clean = _attributes(self._boundary, payload, model_identity=self._model_identity)
        if self._span is not None:
            self._span.set_attributes(clean)
            if clean.get('status') in ('error', 'cancelled'):
                from opentelemetry.trace import StatusCode
                self._span.set_status(StatusCode.ERROR)

    def event(self, payload):
        clean = _attributes(self._boundary, payload, model_identity=self._model_identity)
        if self._span is not None:
            self._span.add_event('api_agent.event', attributes=clean)


class TraceRuntime:
    def __init__(self, config, *, boundary=None, factory=None, model_identities=()):
        self.bind_model_identities(model_identities)
        policy = ContentPolicy.from_config(config, field_name="TRACELOOP_TRACE_CONTENT")
        self.boundary = (boundary or EmissionBoundary()).for_policy(policy)
        self._provider = self._tracer = None
        self._closed = False
        self._factory = factory or _sdk_factory
        self._plan = None
        self._worker = None
        self._work_lock = Lock()
        self._done = Event()
        self._result = True
        self._shutdown_complete = False
        self._shutdown_flush_attempted = False
        self._delivery_failed = False
        if config.errors or config.missing:
            raise TraceError('Tracing requires valid complete configuration.')
        values = config.values
        mode = values.get('TRACE_MODE', '')
        if mode == '':
            return
        if mode not in ('OTEL', 'LangFuse'):
            raise TraceError('Unsupported trace mode.')
        endpoint = values.get('TRACE_TARGET')
        if endpoint is None:
            endpoint = 'http://localhost:3000' if mode == 'LangFuse' else 'http://localhost:4318/v1/traces'
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            parsed.port
        except (ValueError, TypeError, AttributeError):
            raise TraceError('Invalid trace endpoint.') from None
        headers = {}
        if mode == 'LangFuse':
            public, secret = values.get('LANGFUSE_PUBLIC_KEY'), values.get('LANGFUSE_SECRET_KEY')
            if any(type(key) is not str or not key for key in (public, secret)):
                raise TraceError('LangFuse tracing requires API keys.')
            auth = 'Basic ' + b64encode(f'{public}:{secret}'.encode()).decode()
            for credential in (public, secret, auth, auth[6:]):
                self.boundary.register_secret(credential)
            headers['Authorization'] = auth
            endpoint = endpoint.rstrip('/') + '/api/public/otel/v1/traces'
        self._plan = (endpoint, headers, _attributes(self.boundary, {}))

    def bind_model_identities(self, model_identities):
        """Replace the trusted catalog snapshot; span payloads cannot admit identities."""
        # Only trusted model catalog/configuration owners supply explicit identities;
        # the per-span payload never grants itself metadata admission.
        if (type(model_identities) not in (tuple, list, set, frozenset)
                or len(model_identities) > 256
                or any(type(value) is not str or not 0 < len(value) <= 200
                       or not value.isprintable() for value in model_identities)):
            raise TraceError('Trace model identities must be a bounded collection of identifiers.')
        self._model_identities = frozenset(model_identities)

    def is_model_identity(self, value):
        """Return whether the owner catalog currently admits this exact identity."""
        return _model_identity(value, self._model_identities)

    async def astart(self):
        if self._plan is None or self._provider is not None or self._closed:
            return
        try:
            self._provider, self._tracer = self._factory(*self._plan)
        except Exception:
            raise TraceError('Tracing initialization failed.') from None

    def begin(self, name, structural, *, payload=None, parent=None, kind="INTERNAL", start_time=None):
        """Begin a manually admitted operation without installing process globals."""
        clean = _attributes(self.boundary, payload or {}, model_identity=self.is_model_identity)
        # Structural is supplied only by the admitted observation adapter. Scrub
        # it unconditionally; content opt-in does not authorize resource labels.
        clean.update(_structural(self.boundary, structural, self._model_identities))
        span = None
        if self._tracer is not None and not self._closed:
            from opentelemetry.trace import SpanKind, set_span_in_context
            from opentelemetry.context import Context
            span = self._tracer.start_span(_span_name(self.boundary, name, self._model_identities), attributes=clean,
                context=set_span_in_context(parent._span, Context()) if parent is not None and parent._span is not None else Context(),
                kind=getattr(SpanKind, kind), start_time=start_time)
        return TraceSpan(span, self.boundary, self.is_model_identity)

    @contextmanager
    def span(self, payload=None):
        clean = _attributes(self.boundary, payload, model_identity=self.is_model_identity)
        span = None
        if self._tracer is not None and not self._closed:
            from opentelemetry.context import Context
            span = self._tracer.start_span('api_agent.span', attributes=clean, context=Context())
        safe = TraceSpan(span, self.boundary, self.is_model_identity)
        try:
            yield safe
        except BaseException as error:
            safe.attributes(self.boundary.exception(error))
            raise
        finally:
            if span is not None:
                span.end()

    def event(self, payload):
        with self.span() as span:
            span.event(payload)

    @staticmethod
    def _timeout(timeout):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise TraceError('Trace timeout must be between zero and thirty seconds.')

    def _bounded(self, timeout):
        self._timeout(timeout)
        with self._work_lock:
            # Check completion atomically with worker acquisition: another closer
            # may finish between shutdown's initial check and this lock.
            if self._shutdown_complete:
                return self._result
            if self._worker is None or self._done.is_set():
                self._done.clear()
                def run():
                    with self._work_lock:
                        flush = not self._closed or not self._shutdown_flush_attempted
                        if self._closed:
                            self._shutdown_flush_attempted = True
                    try:
                        if flush:
                            try:
                                delivered = bool(self._provider.force_flush(timeout_millis=int(timeout * 1000)))
                            except BaseException:
                                delivered = False
                            self._delivery_failed = self._delivery_failed or not delivered
                        self._result = not self._delivery_failed
                    finally:
                        with self._work_lock:
                            closing = self._closed
                            if not closing:
                                self._done.set()
                        if closing:
                            # A retry of failed disposal must not replay export.
                            self._shutdown_flush_attempted = True
                            try:
                                self._provider.shutdown()
                            except BaseException:
                                self._result = False
                            else:
                                self._shutdown_complete = True
                                self._result = not self._delivery_failed
                            finally:
                                self._done.set()
                self._worker = Thread(target=run, daemon=True, name='api-agent-trace-cleanup')
                self._worker.start()
        return (self._done.wait(timeout) and self._result
                and (not self._closed or self._shutdown_complete))

    def flush(self, timeout=5):
        if self._provider is None:
            return True
        with self._work_lock:
            if self._closed:
                return self._shutdown_complete and not self._delivery_failed
        return self._bounded(timeout)

    def shutdown(self, timeout=5):
        self._timeout(timeout)
        with self._work_lock:
            self._closed = True
            if self._provider is None:
                return True
            if self._shutdown_complete:
                return self._result
        return self._bounded(timeout)

    @property
    def disposal_confirmed(self):
        """True only after the provider acknowledged shutdown without an exception."""
        with self._work_lock:
            return self._shutdown_complete

    async def aclose(self, timeout=5):
        # Cancellation stops the awaiter; owned cleanup still completes in its
        # one worker and the caller receives the original cancellation.
        if not await asyncio.shield(asyncio.to_thread(self.shutdown, timeout)):
            if self.disposal_confirmed:
                if self._delivery_failed:
                    raise TraceDeliveryError(TRACE_DELIVERY_WARNING)
                return True
            raise TraceError('Tracing shutdown failed.')
        return True
