"""Manual, runtime-owned OTLP tracing; payloads cross privacy before SDK creation.

No global provider, automatic instrumentation, environment resource detectors or
SDK logging hooks are installed. An ambient OTEL_SDK_DISABLED=true conflicts
with enabled tracing and fails initialization without allocating SDK resources. The HTTP adapter uses the official OTLP encoder
but owns transport diagnostics so backend response bodies cannot reach logs.
"""
import asyncio
from contextlib import contextmanager
import json
import os
import re
from threading import Event, Lock, Thread

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
    if not boundary.policy.allow_content:
        from .inference_diagnostics import has_optional_content, omission_notice
        if has_optional_content(payload):
            clean['api_agent.content_notice'] = omission_notice('TRACELOOP_TRACE_CONTENT')
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
        elif key in ('api_agent.attempt', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens',
                     'gen_ai.usage.cache_read.input_tokens', 'gen_ai.usage.cache_write.input_tokens',
                     'gen_ai.usage.reasoning.output_tokens', 'gen_ai.request.max_tokens',
                     'api_agent.transport.usage.input_tokens', 'api_agent.transport.usage.output_tokens',
                     'api_agent.transport.usage.cache_read.input_tokens', 'api_agent.transport.usage.cache_write.input_tokens',
                     'api_agent.transport.usage.reasoning.output_tokens'):
            if type(value) is int and 0 <= value <= (1 << 53) - 1:
                clean[key] = value
        elif key in ('gen_ai.response.id', 'api_agent.response.serving_revision'):
            if type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
                if boundary.content_export({'value':value}).get('value') == value:
                    clean[key] = value
        elif key == 'api_agent.llm.outcome' and value in ('completed','length','tool_calls','refused'):
            clean[key] = value
        elif key == 'api_agent.response.status' and value in ('completed','incomplete','failed','cancelled','in_progress','queued'):
            clean[key] = value
        elif key == 'api_agent.response.service_tier' and value in ('auto','default','flex','priority','scale','standard','reserved'):
            clean[key] = value
        elif key == 'gen_ai.request.reasoning.level' and value in ('none','off','minimal','low','medium','high','xhigh','max','auto'):
            clean[key] = value
        elif key == 'gen_ai.provider.name':
            if (type(value) is str and re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', value)
                    and re.fullmatch(r'(?:(?:sk|pk|ghp|gho|ghs)[_-]|bearer[.])[a-z0-9_.-]{16,}', value) is None):
                safe = boundary.content_export({'value': value}).get('value')
                if safe == value:
                    clean[key] = value
        elif key == 'gen_ai.request.temperature':
            if type(value) in (int, float) and __import__('math').isfinite(value) and 0 <= value <= (1 << 53) - 1:
                clean[key] = float(value)
        elif key == 'gen_ai.response.finish_reasons':
            if type(value) is list and len(value) <= 8 and all(type(item) is str and item in
                    ('stop', 'length', 'tool_calls', 'content_filter', 'end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'guardrail_intervened', 'content_filtered', 'other') for item in value):
                clean[key] = value
        elif key == 'api_agent.failure.cause':
            from .outcomes import OutcomeCause
            if type(value) is str and value in frozenset(item.value for item in OutcomeCause):
                clean[key] = value
        elif key == 'api_agent.failure.code':
            from .outcomes import FAILURE_CODES
            if type(value) is str and value in FAILURE_CODES:
                clean[key] = value
        elif key == 'error.type' and value in ('unknown', 'deadline', 'unavailable', 'network', 'rate_limit', 'authentication', 'authorization', 'invalid_request', 'server'):
            clean[key] = value
        elif key in ('api_agent.transport_index', 'api_agent.transport_attempt_count'):
            if type(value) is int and 0 <= value <= 20:
                clean[key] = value
        elif key == 'api_agent.transport_status':
            if type(value) is int and 100 <= value <= 599:
                clean[key] = value
        elif key in ('api_agent.transport_complete', 'api_agent.transport_recovered', 'api_agent.usage.complete', 'api_agent.transport.terminal_known'):
            if type(value) is bool:
                clean[key] = value
        elif key == 'api_agent.transport_outcome' and value in ('started', 'observed', 'completed', 'failed'):
            clean[key] = value
        elif key == 'langfuse.observation.type' and value in ('generation', 'tool', 'chain', 'span'):
            clean[key] = value
        elif key in ('gen_ai.tool.name', 'gen_ai.tool.call.id', 'api_agent.tool.instance'):
            if type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
                clean[key] = value
        elif key == 'http.request.method':
            from .http_tracing import METHODS
            if type(value) is str and value in METHODS:
                clean[key] = value
        elif key == 'http.route':
            from .http_tracing import ROUTES
            if type(value) is str and value in ROUTES:
                clean[key] = value
        elif key == 'http.response.status_code':
            if type(value) is int and 100 <= value <= 599:
                clean[key] = value
        elif key == 'gen_ai.operation.name' and value in ('chat', 'execute_tool'):
            clean[key] = value
        elif key == 'api_agent.capture.reason' and value in ('captured', 'partial', 'truncated', 'unavailable', 'withheld', 'terminal_unknown'):
            clean[key] = value
        elif key == 'api_agent.capture.phase' and value in ('request', 'response'):
            clean[key] = value
        elif key == 'api_agent.capture.source' and value in ('provider', 'fallback'):
            clean[key] = value
    return _attributes(boundary.for_policy(ContentPolicy(True)), clean,
        model_identity=lambda value: _model_identity(value, model_identities))


def _span_name(boundary, name, model_identities=frozenset()):
    from .http_tracing import safe_name
    if type(name) is str and name.startswith('chat ') and not _model_identity(name[5:], model_identities):
        return 'chat'
    if type(name) is not str or not (
            re.fullmatch(r'(?:graph|component|node) [A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', name)
            or name in ('execute_tool', 'transport attempt', 'request') or safe_name(name)
            or (name.startswith('chat ') and _model_identity(name[5:], model_identities))):
        return 'api_agent.span'
    return boundary.content_export({'name': name}).get('name', 'api_agent.span')

class _TraceSDKOwner:
    """Retain successful allocations and acknowledge each disposal phase."""
    def __init__(self):
        from .operation import TraceLoss
        self.trace_loss_owner = TraceLoss()
        self.session = self.exporter = self.processor = self.provider = None
        self.acknowledged = set()
        self.disposed = False
        self._dispose_lock = Lock()

    def force_flush(self, timeout_millis=5000):
        return self.provider.force_flush(timeout_millis=timeout_millis) if self.provider is not None else True

    def shutdown(self):
        with self._dispose_lock:
            if self.disposed:
                return
            # Drain active HTTP use before stopping the worker. Explicit lower
            # phases remain retryable even when an SDK shutdown is one-shot.
            for name in ('exporter', 'processor', 'provider', 'session'):
                if name in self.acknowledged:
                    continue
                component = getattr(self, name)
                if component is not None:
                    if name == 'session':
                        component.close()
                    else:
                        component.shutdown()
                self.acknowledged.add(name)
            self.disposed = True


def _sdk_factory(endpoint, headers, resource, *, own=None, environ=None):
    """Retain partial startup before any later SDK construction can fail."""
    # Honor the SDK disable policy without silent data loss or modifying globals.
    environment = os.environ if environ is None else environ
    if environment.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        raise TraceError('Trace SDK disable flag conflicts with enabled tracing.')
    if environ is not None and os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        raise TraceError('Ambient SDK disable policy conflicts with captured tracing.')
    owner = _TraceSDKOwner()
    try:
        if own is not None:
            own(owner)
        return _build_sdk(endpoint, headers, resource, owner)
    except BaseException as error:
        error.cleanup_owner = owner
        if own is None:
            try:
                owner.shutdown()
            except BaseException:
                pass
        raise


def _build_sdk(endpoint, headers, resource, owner):
    # Lazy imports: disabled tracing has no optional dependency or backend I/O.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
    from requests import Session
    from threading import Condition

    pending = [0]
    pending_lock = Lock()
    queue_capacity = 1024

    owner.session = Session()
    owner.session.trust_env = False

    class OwnedHTTPExporter(OTLPSpanExporter):
        def __init__(self):
            self.session = owner.session
            self.closed = False
            self.failed = Event()
            self._condition = Condition()
            self._active = 0

        def export(self, spans):
            with self._condition:
                if self.closed:
                    self.failed.set()
                    return SpanExportResult.FAILURE
                self._active += 1
            try:
                return self._export(spans)
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
                with pending_lock:
                    pending[0] = max(0, pending[0] - len(spans))

        def _export(self, spans):
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
            with self._condition:
                self.closed = True
                if not self._condition.wait_for(lambda: self._active == 0, timeout=5):
                    raise TraceError('Trace export disposal remains pending.')

    exporter = OwnedHTTPExporter()
    owner.exporter = exporter
    class OwnedTracerProvider(TracerProvider):
        def force_flush(self, timeout_millis=5000):
            # SDK flush reports queue drainage, not successful delivery. Keep
            # failed delivery sticky for this runtime without backend text.
            drained = super().force_flush(timeout_millis=timeout_millis)
            return drained and not exporter.failed.is_set()

    class CountedBatchSpanProcessor(BatchSpanProcessor):
        # The SDK's bounded queue reports overflow only through SDK diagnostics.
        # Admit outstanding records before its public on_end hook instead, so
        # queue rejection has the same explicit application loss ownership.
        def on_end(self, span):
            if not (span.context and span.context.trace_flags.sampled):
                return
            with pending_lock:
                rejected = pending[0] >= queue_capacity
                if not rejected:
                    pending[0] += 1
                    super().on_end(span)
            if rejected:
                owner.trace_loss_owner.record()

    provider = OwnedTracerProvider(resource=Resource({'service.name': 'api-agent', **resource}),
        sampler=ALWAYS_ON, shutdown_on_exit=False)
    owner.provider = provider
    processor = CountedBatchSpanProcessor(exporter, max_queue_size=queue_capacity,
        max_export_batch_size=32, schedule_delay_millis=500, export_timeout_millis=5000)
    owner.processor = processor
    provider.add_span_processor(processor)
    return owner, provider.get_tracer('api_agent')



class TraceSpan:
    def __init__(self, span, boundary, model_identity=None, mapping=None, operation=None, losses=None):
        from .operation import TraceLoss
        self._losses = losses or TraceLoss()
        self._span = span
        self._boundary = boundary
        self._model_identity = model_identity
        self._operation = operation
        self._outcome = None
        self._failure_description = None
        self._failure_signature = None
        from .trace_mapping import TraceMapping, admit_mapping
        self._mapping = admit_mapping(mapping or TraceMapping())

    def export_context(self):
        """Export only this explicit SDK span's bounded identity, never ambient state."""
        if self._span is None:
            return None
        context = self._span.get_span_context()
        if not context.is_valid:
            return None
        return {'version': 1, 'trace_id': format(context.trace_id, '032x'),
                'span_id': format(context.span_id, '016x'),
                'trace_flags': int(context.trace_flags) & 1}

    def end(self, *, end_time=None):
        if self._span is not None:
            span, self._span = self._span, None
            self._losses.run(lambda: span.end(end_time=end_time))

    def attributes(self, payload):
        clean = _attributes(self._boundary, payload, model_identity=self._model_identity)
        if self._span is not None:
            self._losses.run(lambda: self._span.set_attributes(clean))
            if clean.get('status') == 'error':
                self._error_status()

    def _error_status(self):
        from opentelemetry.trace import Status, StatusCode
        lease = self._boundary.begin_evidence_lease()
        try:
            message = self._failure_description or 'Operation did not complete successfully.'
            # Scope closure is another publication; refresh cached descriptions
            # against credentials registered since the original failure.
            self._failure_description = self._boundary.for_policy(ContentPolicy(True)).export({
                'message': message}).get('message')
            self._losses.run(lambda: self._span.set_status(
                Status(StatusCode.ERROR, self._failure_description)))
        finally:
            lease.release()

    def failure(self, error, *, deadline_expired=False):
        """Describe an explicit failure through this destination's live boundary."""
        from .failure_content import failure_content
        from .outcomes import failure_diagnostic
        diagnostic = failure_diagnostic(error, deadline_expired=deadline_expired)
        if self._span is None or diagnostic['cause'] == 'cancelled':
            return
        lease = self._boundary.begin_evidence_lease()
        try:
            fixed = self._boundary.for_policy(ContentPolicy(True)).export({
                'message': 'Operation failed (' + diagnostic['code'] + ').'})
            admitted = self._boundary.export({'failure': {
                **failure_content(error, include_candidate=False), **diagnostic}})
            details = admitted.get('failure', {})
            self._failure_description = details.get('provider', {}).get('message') or fixed.get('message')
            encoded = json.dumps(details, ensure_ascii=False, allow_nan=False) if details else None
            signature = (self._failure_description, encoded)
            self._error_status()
            if signature == self._failure_signature:
                return
            self._failure_signature = signature
            attributes = _structural(self._boundary, {
                'api_agent.failure.cause': diagnostic['cause'],
                'api_agent.failure.code': diagnostic['code']}, ())
            if self._failure_description is not None:
                attributes['message'] = self._failure_description
            if encoded is not None:
                attributes['api_agent.failure.details'] = encoded
            self._losses.run(lambda: self._span.add_event('api_agent.failure', attributes=attributes))
        finally:
            lease.release()

    def structural(self, payload):
        models = frozenset(value for key, value in payload.items() if key in
            ('gen_ai.request.model', 'gen_ai.response.model') and self._model_identity is not None
            and self._model_identity(value))
        clean = _structural(self._boundary, payload, models)
        if self._span is not None:
            self._losses.run(lambda: self._span.set_attributes(clean))

    def outcome(self, outcome):
        from .outcomes import ExecutionOutcome, OutcomeState
        if type(outcome) is not ExecutionOutcome:
            raise TypeError('Expected a versioned execution outcome')
        if self._outcome is not None and self._outcome.state in (OutcomeState.FAILURE, OutcomeState.PARTIAL):
            if outcome.state != OutcomeState.FAILURE or self._outcome.state == OutcomeState.FAILURE:
                return
        self._outcome = outcome
        if self._span is not None:
            clean = self._boundary.for_policy(ContentPolicy(True)).export({
                'api_agent.outcome.version': outcome.version,
                'api_agent.outcome.state': outcome.state.value,
                **({'api_agent.outcome.cause_id': outcome.cause_id} if outcome.cause_id else {}),
                **({'error.type': outcome.cause.value} if outcome.state in
                   (OutcomeState.FAILURE, OutcomeState.PARTIAL) else {})})
            self._losses.run(lambda: self._span.set_attributes(clean))
            if outcome.state in (OutcomeState.FAILURE, OutcomeState.PARTIAL):
                self._error_status()

    def capture_loss(self, count):
        """Structural incomplete-delivery marker independent of content permission."""
        if type(count) is not int or not 0 < count <= (1 << 53) - 1:
            raise ValueError('Invalid trace loss count')
        if self._span is not None:
            clean = self._boundary.for_policy(ContentPolicy(True)).export({
                'api_agent.trace.loss_count': count, 'api_agent.trace.complete': False})
            self._losses.run(lambda: self._span.set_attributes(clean))

    def native_io(self, direction, values):
        """Native observation panels use the same mandatory pre-queue boundary."""
        if direction not in ('input', 'output'):
            raise ValueError('Invalid observation direction')
        # Project structure before JSON encoding: hidden carriers cannot hide in
        # a serialized attribute and content opt-in never grants secret access.
        clean = self._boundary.export({'values': values})
        if 'values' in clean and self._span is not None:
            self._losses.run(lambda: self._span.set_attribute(self._mapping.io_attribute(direction),
                json.dumps(clean['values'], ensure_ascii=False, allow_nan=False)))
            from .trace_mapping import genai_messages
            messages = genai_messages(direction, clean['values']) if self._operation == 'chat' else None
            if messages is not None:
                self._losses.run(lambda: self._span.set_attribute('gen_ai.' + direction + '.messages',
                    json.dumps(messages, ensure_ascii=False, allow_nan=False)))

    def event(self, payload, *, structural=None, timestamp=None):
        clean = _attributes(self._boundary, payload, model_identity=self._model_identity)
        clean.update(_structural(self._boundary, structural, ()))
        if self._span is not None:
            self._losses.run(lambda: self._span.add_event('api_agent.event', attributes=clean,
                                 **({'timestamp': timestamp} if timestamp is not None else {})))


class TraceRuntime:
    def __init__(self, config, *, boundary=None, factory=None, model_identities=(), mapping=None, binding=None, environ=None, explicit_fields=None):
        from .trace_mapping import TraceMapping, admit_mapping
        self._binding = binding
        self._binding_close_task = None
        self._binding_closed = False
        self.mapping = admit_mapping(binding.mapping if binding is not None else mapping or TraceMapping())
        from .operation import TraceLoss
        self.losses = TraceLoss()
        self._catalog_model_identities = frozenset()
        self._response_model_identities = frozenset()
        self.bind_model_identities(model_identities)
        policy = ContentPolicy.from_config(config, field_name="TRACELOOP_TRACE_CONTENT")
        self.boundary = (boundary or EmissionBoundary()).for_policy(policy)
        self._provider = self._tracer = None
        self._lifecycle_lock = asyncio.Lock()
        self._startup_failed = False
        self._closed = False
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
        from .http_tracing import diagnostics_level
        self.http_diagnostics_level = diagnostics_level(values)
        if binding is not None:
            return
        from .tracing_providers import admit_binding
        try:
            self._binding = admit_binding(config, owner_root=None, boundary=self.boundary, environ=environ, explicit_fields=explicit_fields)
        except ValueError as error:
            raise TraceError(str(error)) from None
        if self._binding is not None:
            self._binding.exporter_factory = factory
            self.mapping = admit_mapping(self._binding.mapping)

    @property
    def enabled(self):
        """Whether this owner has an explicitly selected export plan."""
        return self._binding is not None

    @property
    def active(self):
        """Admit observations only after startup and before disposal."""
        return self._tracer is not None and not self._closed

    def bind_model_identities(self, model_identities):
        """Replace the trusted catalog snapshot; span payloads cannot admit identities."""
        # Only trusted model catalog/configuration owners supply explicit identities;
        # the per-span payload never grants itself metadata admission.
        if (type(model_identities) not in (tuple, list, set, frozenset)
                or len(model_identities) > 256
                or any(type(value) is not str or not 0 < len(value) <= 200
                       or not value.isprintable() for value in model_identities)):
            raise TraceError('Trace model identities must be a bounded collection of identifiers.')
        self._catalog_model_identities = frozenset(model_identities)
        self._model_identities = self._catalog_model_identities | self._response_model_identities

    def admit_provider_model(self, result):
        """Admit a served alias from the validated shared provider result only."""
        from api_agent.providers.llm import LLMResult
        if not isinstance(result, LLMResult) or result.actual_model is None or not self.active:
            return
        if result.actual_model not in self._response_model_identities and len(self._response_model_identities) >= 256:
            self.losses.record()
            return
        self._response_model_identities = self._response_model_identities | {result.actual_model}
        self._model_identities = self._catalog_model_identities | self._response_model_identities

    def is_model_identity(self, value):
        """Return whether the owner catalog currently admits this exact identity."""
        return _model_identity(value, self._model_identities)

    async def astart(self):
        async with self._lifecycle_lock:
            await self._start()

    async def _start(self):
        if not self.enabled or self._closed:
            return
        if self._startup_failed:
            raise TraceError('Tracing initialization failed.')
        if self._provider is not None:
            return
        try:
            resource = self.boundary.content_export({'service.name': 'api-agent'})
            self._provider, self._tracer = await self._binding.open(resource)
            from .operation import TraceLoss
            trace_provider = self._binding.handles.provider if self._binding.handles is not None else self._provider
            if type(getattr(trace_provider, 'trace_loss_owner', None)) is TraceLoss:
                trace_provider.trace_loss_owner = self.losses
        except BaseException as error:
            self._startup_failed = True
            if self._provider is None:
                # The binding has already retained any partial startup owner.
                # Its ledger must acknowledge disposal once for both lifetimes.
                self._provider = self._binding.signal_owner
                if getattr(error, 'cleanup_owner', None) is not None:
                    self._delivery_failed = True
            if not isinstance(error, Exception):
                raise
            raise TraceError('Tracing initialization failed.') from None

    @property
    def pipeline_id(self):
        """Opaque account/pipeline compatibility proof for authenticated handoff."""
        if not self.enabled:
            return None
        identity = getattr(self._binding, 'pipeline_identity', None)
        return identity if type(identity) is str and re.fullmatch(r'[0-9a-f]{64}', identity) else None

    def import_parent(self, parent):
        from .trace_ownership import validate_parent
        parent = validate_parent(parent)
        if not self.active:
            return TraceSpan(None, self.boundary, self.is_model_identity, self.mapping, losses=self.losses)
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
        context = SpanContext(int(parent['trace_id'], 16), int(parent['span_id'], 16),
                              True, TraceFlags(parent['trace_flags']))
        return TraceSpan(NonRecordingSpan(context), self.boundary, self.is_model_identity,
                         self.mapping, losses=self.losses)

    def record_call(self, structural, elapsed, outcome, *, attempts=None):
        """One terminal logical-call trigger; physical usage stays in its ledger."""
        if not self.active or self._binding is None:
            return
        import math
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = None
        if outcome not in ('completed', 'failed', 'failure', 'partial', 'cancelled',
                           'success', 'unknown', 'length', 'tool_calls', 'refused'):
            outcome = 'unknown'
        if attempts is not None:
            from api_agent.providers.generation_attempts import validate_transport_attempts
            attempts = validate_transport_attempts(attempts)
        clean = _structural(self.boundary, structural, self._model_identities)
        self.losses.run(lambda: self._binding.record_call(clean, elapsed, outcome, attempts=attempts))

    def begin(self, name, structural, *, payload=None, parent=None, kind="INTERNAL", start_time=None):
        """Begin a manually admitted operation without installing process globals."""
        clean = _attributes(self.boundary, payload or {}, model_identity=self.is_model_identity)
        # Structural is supplied only by the admitted observation adapter. Scrub
        # it unconditionally; content opt-in does not authorize resource labels.
        clean.update(_structural(self.boundary, structural, self._model_identities))
        if type(structural) is dict and structural.get('gen_ai.operation.name') in ('chat', 'execute_tool'):
            clean.update(_structural(self.boundary, {'langfuse.observation.type':
                'generation' if structural['gen_ai.operation.name'] == 'chat' else 'tool'}))
        span = None
        if self._tracer is not None and not self._closed:
            from opentelemetry.trace import SpanKind, set_span_in_context
            from opentelemetry.context import Context
            from .operation import safe_trace
            span = self.losses.run(lambda: self._tracer.start_span(_span_name(self.boundary, name, self._model_identities), attributes=clean,
                context=set_span_in_context(parent._span, Context()) if parent is not None and parent._span is not None else Context(),
                kind=getattr(SpanKind, kind), start_time=start_time))
        return TraceSpan(span, self.boundary, self.is_model_identity, self.mapping, clean.get('gen_ai.operation.name'), self.losses)

    @contextmanager
    def span(self, payload=None):
        clean = _attributes(self.boundary, payload, model_identity=self.is_model_identity)
        span = None
        if self._tracer is not None and not self._closed:
            from opentelemetry.context import Context
            from .operation import safe_trace
            span = self.losses.run(lambda: self._tracer.start_span('api_agent.span', attributes=clean, context=Context()))
        safe = TraceSpan(span, self.boundary, self.is_model_identity)
        try:
            yield safe
        except BaseException as error:
            safe.attributes(self.boundary.exception(error))
            raise
        finally:
            safe.end()

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
        async with self._lifecycle_lock:
            return await self._close(timeout)

    async def _close(self, timeout):
        # SDK disposal precedes factory/meter ownership release. Retrying an
        # unacknowledged binding close never replays export or reopens the SDK.
        completed = await asyncio.shield(asyncio.to_thread(self.shutdown, timeout))
        if not completed and not self.disposal_confirmed and self._provider is not None:
            raise TraceError('Tracing shutdown failed.')
        if self._binding is not None and not self._binding_closed:
            if (self._binding_close_task is None or self._binding_close_task.done()
                    and (self._binding_close_task.cancelled() or self._binding_close_task.exception() is not None)):
                self._binding_close_task = asyncio.create_task(self._binding.aclose())
            await asyncio.shield(self._binding_close_task)
            self._binding_closed = True
        if self._delivery_failed:
            raise TraceDeliveryError(TRACE_DELIVERY_WARNING)
        return True
