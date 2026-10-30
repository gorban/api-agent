"""Backend-owned manual spans; payloads arrive only after authority projection."""
import json
import warnings

from .tracing import TraceRuntime, TraceDeliveryError, TRACE_DELIVERY_WARNING


class RemoteInferenceTrace:
    def __init__(self, config, boundary, *, factory=None):
        self.enabled = config.values.get('TRACE_MODE', '') != ''
        self.runtime = TraceRuntime(config, boundary=boundary, factory=factory)
        self._delivery_reported = False

    async def astart(self):
        await self.runtime.astart()

    def begin(self, call_id):
        return RemoteTraceCall(self.runtime, call_id) if self.enabled else None

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
    def __init__(self, runtime, call_id):
        self.runtime, self.call_id = runtime, call_id
        self.span = runtime.begin('chat unknown', {'api_agent.call_id': call_id,
                                                  'gen_ai.operation.name': 'chat'}, kind='CLIENT')
        self.ended = False

    @property
    def boundary(self):
        return self.runtime.boundary

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
        try:
            for attempt in (ledger or {}).get('attempts', ()):
                child = self.runtime.begin('transport attempt', {
                    'api_agent.call_id': self.call_id, 'api_agent.transport_id': attempt['id'],
                    'api_agent.transport_index': attempt['index'],
                    'api_agent.transport_outcome': attempt['outcome'],
                    **({'api_agent.transport_status': attempt['status']} if attempt['status'] is not None else {}),
                }, parent=self.span, kind='CLIENT', start_time=attempt['started_ms'] * 1_000_000)
                children[attempt['id']] = child
                started[attempt['id']] = attempt['started_ms']
                stopped[attempt['id']] = attempt.get('stopped_ms')
                child.attributes({'status': 'ok' if attempt['outcome'] == 'completed' else 'error'})
            for row in rows:
                attempt_id = row.get('transport_attempt_id')
                if attempt_id is not None and attempt_id not in children:
                    raise ContractError('Trace evidence has no transport attempt owner')
                recorded_time = ((started if row['phase'] == 'request' else stopped).get(attempt_id))
                (children.get(attempt_id) or self.span).event({
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
                child.end(**({'end_time': stopped[identifier] * 1_000_000}
                             if stopped[identifier] is not None else {}))

    def loss(self, reason):
        # Fixed metadata only; an exporter failure must not reflect an exception.
        self.span.event({'values': {}, 'reason': reason}, structural={'api_agent.capture.reason': reason})

    def end(self, *, failed=False, cancelled=False):
        if not self.ended:
            self.ended = True
            try:
                self.span.attributes({'status': 'cancelled' if cancelled else 'error' if failed else 'ok'})
            finally:
                self.span.end()
