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
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from .privacy import ContentPolicy, EmissionBoundary


class TraceError(RuntimeError):
    """Fixed diagnostics deliberately omit SDK messages and credentials."""


def _attributes(boundary, payload):
    clean = boundary.export(payload)
    return {key: value if type(value) in (str, bool, int, float) else json.dumps(value)
            for key, value in clean.items()}


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
    def __init__(self, span, boundary):
        self._span = span
        self._boundary = boundary

    def attributes(self, payload):
        clean = _attributes(self._boundary, payload)
        if self._span is not None:
            self._span.set_attributes(clean)

    def event(self, payload):
        clean = _attributes(self._boundary, payload)
        if self._span is not None:
            self._span.add_event('api_agent.event', attributes=clean)


class TraceRuntime:
    def __init__(self, config, *, boundary=None, factory=None):
        self.boundary = boundary or EmissionBoundary(ContentPolicy.from_config(config))
        self._provider = self._tracer = None
        self._closed = False
        self._factory = factory or _sdk_factory
        self._plan = None
        self._worker = None
        self._work_lock = Lock()
        self._done = Event()
        self._result = True
        self._shutdown_complete = False
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

    async def astart(self):
        if self._plan is None or self._provider is not None or self._closed:
            return
        try:
            self._provider, self._tracer = self._factory(*self._plan)
        except Exception:
            raise TraceError('Tracing initialization failed.') from None

    @contextmanager
    def span(self, payload=None):
        clean = _attributes(self.boundary, payload)
        span = None
        if self._tracer is not None and not self._closed:
            span = self._tracer.start_span('api_agent.span', attributes=clean)
        safe = TraceSpan(span, self.boundary)
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
            if self._worker is None or self._done.is_set():
                self._done.clear()
                def run():
                    try:
                        self._result = bool(self._provider.force_flush(timeout_millis=int(timeout * 1000)))
                    except Exception:
                        self._result = False
                    finally:
                        with self._work_lock:
                            closing = self._closed
                            if not closing:
                                self._done.set()
                        if closing:
                            try:
                                self._provider.shutdown()
                            except Exception:
                                self._result = False
                            with self._work_lock:
                                self._shutdown_complete = True
                                self._done.set()
                self._worker = Thread(target=run, daemon=True, name='api-agent-trace-cleanup')
                self._worker.start()
        return self._done.wait(timeout) and self._result

    def flush(self, timeout=5):
        if self._provider is None or self._closed:
            return True
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

    async def aclose(self, timeout=5):
        # Cancellation stops the awaiter; owned cleanup still completes in its
        # one worker and the caller receives the original cancellation.
        if not await asyncio.shield(asyncio.to_thread(self.shutdown, timeout)):
            raise TraceError('Tracing shutdown failed.')
        return True
