"""Backend-owned manual spans; payloads arrive only after authority projection."""
import json
import warnings

from .tracing import TraceRuntime, TraceDeliveryError, TRACE_DELIVERY_WARNING


class RemoteInferenceTrace:
    def __init__(self, config, boundary, *, factory=None, owner_root=None, environ=None, explicit_fields=None):
        binding = selected = None
        if factory is None:
            from .tracing_providers import admit_binding
            selected = admit_binding(config, owner_root=owner_root, boundary=boundary, environ=environ, explicit_fields=explicit_fields)
            if selected is not None and selected.pipeline_identity is not None:
                binding = selected.for_remote_trace()
        self.runtime = TraceRuntime(config, boundary=boundary, factory=factory,
                                    environ=environ, explicit_fields=explicit_fields,
                                    **({'binding':binding or selected} if binding is not None or selected is not None else {}))
        self.enabled = self.runtime.enabled and (selected is None or binding is not None)
        self._delivery_reported = False

    async def astart(self):
        if self.enabled:
            await self.runtime.astart()

    def begin(self, call_id, *, parent=None):
        return RemoteTraceCall(self.runtime, call_id, parent=parent) if self.enabled else None

    async def aclose(self):
        try:
            await self.runtime.aclose()
        except TraceDeliveryError:
            # Disposal was acknowledged. Report sticky loss, but never retain
            # a cleanup receipt for resources which have already been closed.
            if not self._delivery_reported:
                self._delivery_reported = True
                warnings.warn(TRACE_DELIVERY_WARNING, RuntimeWarning, stacklevel=2)


class RemoteTraceCall:
    def __init__(self, runtime, call_id, *, parent=None):
        self.runtime, self.call_id = runtime, call_id
        self.span = runtime.begin('chat unknown', {'api_agent.call_id': call_id,
                                                  'gen_ai.operation.name': 'chat'}, kind='CLIENT',
                                  **({'parent': runtime.import_parent(parent)} if parent is not None else {}))
        self.ended = False
        self._result_usage = {}
        self._lost = False

    @property
    def boundary(self):
        return self.runtime.boundary

    def provider_fields(self, cause_id, transport_id, request, result, *, provider_name=None):
        if self.ended:
            return
        from .genai_fields import provider_fields
        self.runtime.bind_model_identities((request.selected_llm,))
        self.runtime.admit_provider_model(result)
        fields = provider_fields(request, result, provider_name)
        self._result_usage = {key:value for key,value in fields.items() if key.startswith('gen_ai.usage.')}
        self.span.structural({key:value for key,value in fields.items() if not key.startswith('gen_ai.usage.')})

    def publish(self, frames, ledger):
        """Decode only verified projection frames, never provider/native objects."""
        from api_agent.providers.setup_transport import _EvidenceReceiver, _valid_evidence
        from api_agent.providers.contracts import ContractError
        receiver = _EvidenceReceiver()
        rows = []
        for _, _, raw in frames:
            frame = json.loads(raw)
            if frame.get('status') == 'evidence':
                row = {key: value for key, value in frame.items() if key not in ('version', 'status')}
            else:
                row = receiver.accept(frame)
            if row is not None:
                if not _valid_evidence(row):
                    raise ContractError('Invalid trace evidence projection')
                rows.append(row)
        if receiver.pending is not None:
            raise ContractError('Incomplete trace evidence projection')
        children, started, stopped = {}, {}, {}
        attempts = (ledger or {}).get('attempts', ())
        final_dispatch = attempts[-1]['id'] if attempts else None
        from .genai_fields import ledger_usage, transport_usage
        self.span.structural(ledger_usage(ledger) if ledger is not None else self._result_usage)
        try:
            for attempt in attempts:
                child = self.runtime.begin('transport attempt', {
                    'api_agent.call_id': self.call_id, 'api_agent.transport_id': attempt['id'],
                    'api_agent.transport_index': attempt['index'],
                    'api_agent.transport_outcome': attempt['outcome'],
                    'api_agent.transport.terminal_known': attempt.get('stopped_ms') is not None,
                    'error.type': attempt.get('error_kind'),
                    **transport_usage(attempt),
                    **({'api_agent.transport_status': attempt['status']} if attempt['status'] is not None else {}),
                }, parent=self.span, kind='CLIENT', start_time=attempt['started_ms'] * 1_000_000)
                children[attempt['id']] = child
                started[attempt['id']] = attempt['started_ms']
                stopped[attempt['id']] = attempt.get('stopped_ms')
                if attempt['outcome'] in ('completed','failed'):
                    child.attributes({'status':'cancelled' if attempt.get('error_kind') == 'cancelled' else
                                      'ok' if attempt['outcome'] == 'completed' else 'error'})
            for row in rows:
                attempt_id = row.get('transport_attempt_id')
                if attempt_id is not None and attempt_id not in children:
                    raise ContractError('Trace evidence has no transport attempt owner')
                recorded_time = ((started if row['phase'] == 'request' else stopped).get(attempt_id))
                target = children.get(attempt_id) or self.span
                from .genai_fields import diagnostic_fields
                fields = diagnostic_fields(row.get('diagnostics', {}), self.boundary)
                target.structural(fields)
                if target is not self.span and attempt_id == final_dispatch:
                    self.span.structural(fields)
                target.native_io('input' if row['phase'] == 'request' else 'output', row['content'])
                if target is not self.span:
                    self.span.native_io('input' if row['phase'] == 'request' else 'output', row['content'])
                target.event({
                    'phase': row['phase'], 'reason': row.get('reason', 'captured'),
                    'capture_source': row.get('capture_source', 'provider'),
                    'values': row['content'],
                    **({'diagnostics': row['diagnostics']} if 'diagnostics' in row else {}),
                }, structural={'api_agent.capture.phase': row['phase'],
                    'api_agent.capture.reason': row.get('reason', 'captured'),
                    'api_agent.capture.source': row.get('capture_source', 'provider')},
                    **({'timestamp': recorded_time * 1_000_000} if recorded_time is not None else {}))
        finally:
            for identifier, child in children.items():
                if stopped[identifier] is None:
                    self.loss('terminal_unknown')
                    child.capture_loss(1)
                child.end(**({'end_time': stopped[identifier] * 1_000_000}
                             if stopped[identifier] is not None else {}))

    def loss(self, reason):
        # Fixed metadata only; an exporter failure must not reflect an exception.
        if not self._lost:
            self._lost = True
            self.runtime.losses.record()
            self.span.capture_loss(1)
            self.span.event({'values': {}, 'reason': reason}, structural={'api_agent.capture.reason': reason})

    def end(self, *, failed=False, cancelled=False, end_time=None):
        if not self.ended:
            self.ended = True
            try:
                self.span.attributes({'status': 'cancelled' if cancelled else 'error' if failed else 'ok'})
            finally:
                self.span.end(**({'end_time':end_time} if end_time is not None else {}))
