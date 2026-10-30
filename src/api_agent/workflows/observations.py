"""Synchronous destination fan-out over one bounded journal identity issuer.

Raw capture values exist only on the caller stack. The journal retains its
existing log-gated previews; live diagnostics and traces get separate scrubbed
projections before either consumer can queue an event.
"""
import asyncio
from collections.abc import Mapping
from types import MappingProxyType

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .journal import WorkflowJournal, _plain_values, CaptureUnavailable


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


class WorkflowObservationFactory:
    def __init__(self, config, *, boundary, sink=None, trace=None):
        self.log_policy = ContentPolicy.from_config(config)
        self.boundary = boundary.for_policy(self.log_policy)
        self.sink, self.trace = sink, trace

    def create(self, definition, *, journal=None, turn_id=None,
               component_passes=None, test_id=None, source_manifest=None,
               semantic_identity=None):
        if journal is not None and semantic_identity is not None:
            from .identity import validate_identity
            if validate_identity(semantic_identity) != getattr(journal, 'semantic_identity', None):
                raise ValueError("Existing journal semantic identity conflicts")
        journal = journal or WorkflowJournal(definition,
            log_content=self.log_policy.allow_content, boundary=self.boundary,
            turn_id=turn_id, component_passes=component_passes,
            test_id=test_id, source_manifest=source_manifest,
            semantic_identity=semantic_identity)
        return WorkflowObservations(journal, boundary=self.boundary,
            log_policy=self.log_policy, sink=self.sink, trace=self.trace)


class WorkflowObservations:
    """Journal-compatible per-request adapter with no payload store."""
    def __init__(self, journal, *, boundary, log_policy, sink=None, trace=None):
        log_policy, boundary = journal.admit_policy(log_policy, boundary,
            model_identity=trace.is_model_identity if trace is not None else None)
        if trace is not None:
            boundary.include(trace.boundary)
        self.journal, self.boundary = journal, boundary
        self.log_policy, self.sink, self.trace = log_policy, sink, trace
        self._spans = {}
        self._visit_call_kinds = {}
        self._journal_observer = self._admitted
        journal.subscribe(self._journal_observer)

    def credential_authorities(self):
        """Keep wrapper admission attached to the journal's original live owners."""
        return self.journal.credential_authorities()

    @property
    def semantic_identity(self):
        return self.journal.semantic_identity

    @property
    def projection(self):
        return self.journal.projection

    @property
    def turn_id(self):
        return self.journal.turn_id

    @property
    def invocation_id(self):
        return self.journal.invocation_id

    def admit_semantic_identity(self, identity):
        return self.journal.admit_semantic_identity(identity)

    def admit_execution_context(self, value):
        return self.journal.admit_execution_context(value)

    def observe(self, row):
        return self.journal.observe(row)

    def record_call(self, **kwargs):
        return self.journal.record_call(**kwargs)

    def finalize(self):
        return self.journal.finalize()

    def _emit(self, row):
        if self.sink is not None:
            try:
                if row.get('reason') == 'withheld' and not self.log_policy.allow_content:
                    from api_agent.telemetry.inference_diagnostics import omission_notice
                    row = dict(row, content_notice=omission_notice('LOG_CONTENT'))
                self.sink(_freeze(self.boundary.content_export(row)))
            except (Exception, asyncio.CancelledError):
                pass

    def _admitted(self, row):
        self._emit(row)
        try:
            self._trace_row(row)
        except (Exception, asyncio.CancelledError):
            pass

    def _trace_row(self, row):
        if self.trace is None:
            return
        event = row['event']
        if event == 'scope_start':
            if len(self._spans) >= 256:
                return
            kind = row.get('kind', 'node')
            owner = row.get('owner', 'workflow')
            if kind == 'node':
                self._visit_call_kinds[row['scope_id']] = self.journal._node_call_kinds.get(owner)
            structural = {'api_agent.' + key: row[key] for key in (
                'event_id', 'invocation_id', 'turn_id', 'scope_id', 'parent_id',
                'visit_id', 'attempt_id', 'attempt') if row.get(key) is not None}
            self._spans[row['scope_id']] = self.trace.begin(
                kind + ' ' + owner, structural,
                parent=self._spans.get(row.get('parent_id')))
        elif event == 'scope_end':
            span = self._spans.pop(row['scope_id'], None)
            self._visit_call_kinds.pop(row['scope_id'], None)
            if span is not None:
                span.attributes({'status': 'error' if row.get('outcome') == 'failed' else
                                 'cancelled' if row.get('outcome') == 'cancelled' else 'ok',
                                 'duration_seconds': row.get('elapsed')})
                span.end()
        elif event == 'call':
            # Central accounting supplies an id once; journal dedup runs before
            # this callback. Do not estimate or count the same usage at parents.
            tool = (self._visit_call_kinds.get(row.get('visit_id')) == 'deterministic'
                    and row.get('selection') is None and row.get('usage') is None)
            selection = row.get('selection') or 'unknown'
            operation = 'execute_tool' if tool else 'chat'
            structural = {'gen_ai.operation.name': operation,
                          'api_agent.call_id': row['call_id'],
                          'api_agent.event_id': row['event_id']}
            if not tool:
                structural['gen_ai.request.model'] = selection
                structural['gen_ai.response.model'] = row.get('actual_model')
                structural['api_agent.request.reasoning_effort'] = row.get('reasoning_effort')
            for source, target in (('input_tokens', 'input_tokens'),
                                   ('output_tokens', 'output_tokens')):
                value = (row.get('usage') or {}).get(source)
                if type(value) is int and value >= 0:
                    structural['gen_ai.usage.' + target] = value
            ledger = row.get('transport_attempts')
            if ledger is not None:
                from api_agent.providers.generation_attempts import validate_transport_attempts
                ledger = validate_transport_attempts(ledger)
                structural.update({'api_agent.transport_attempt_count': len(ledger['attempts']),
                                   'api_agent.transport_complete': ledger['complete'],
                                   'api_agent.transport_recovered': any(
                                       item['retry_delay_seconds'] is not None for item in ledger['attempts'])})
            span = self.trace.begin(operation if tool else 'chat ' + selection, structural,
                parent=self._spans.get(row.get('visit_id')), kind='CLIENT',
                start_time=int(row['started_ms'] * 1_000_000) if row.get('started_ms') is not None else None)
            if ledger is not None:
                for item in ledger['attempts']:
                    child = self.trace.begin('transport attempt', {
                        'api_agent.transport_id': item['id'],
                        'api_agent.transport_index': item['index'],
                        'api_agent.transport_outcome': item['outcome'],
                        'api_agent.transport_status': item['status'],
                    }, parent=span, kind='CLIENT',
                        start_time=item['started_ms'] * 1_000_000)
                    if item['outcome'] not in ('started', 'observed'):
                        child.attributes({'status': 'error' if item['outcome'] == 'failed' else 'ok'})
                    child.end(end_time=item['stopped_ms'] * 1_000_000
                              if item['stopped_ms'] is not None else None)
            span.attributes({'duration_seconds': row.get('elapsed'),
                             'status': 'error' if row.get('outcome') in ('failed', 'failure', 'partial') else
                                       'cancelled' if row.get('outcome') == 'cancelled' else 'ok'})
            span.end(end_time=int(row['stopped_ms'] * 1_000_000)
                if row.get('stopped_ms') is not None else None)

    def capture(self, *, visit_id, direction, values, schema, plain_fields=None):
        # Each destination is projected while raw values are on this stack,
        # before the journal or any callback can retain a row.
        if direction not in ('input', 'output'):
            raise ValueError('Invalid workflow capture direction')
        if type(visit_id) is not str or not visit_id:
            raise ValueError('Workflow capture requires a node visit')
        if plain_fields:
            # Validate declaration shape/field classification before handling an
            # unavailable payload, so withholding cannot mask a bad contract.
            _plain_values({}, schema, plain_fields, self.boundary)
        unavailable = None
        if type(plain_fields) in (tuple, list) and type(values) is dict and isinstance(schema, Mapping):
            properties = schema.get('properties')
            if isinstance(properties, Mapping):
                for field in plain_fields:
                    rule = properties.get(field) if type(field) is str else None
                    if isinstance(rule, Mapping) and rule.get('type') not in (
                            'string', 'boolean', 'integer', 'number', 'null'):
                        unavailable = 'unsupported'
                    if type(field) is str and type(values.get(field)) is str and len(values[field]) > 65536:
                        unavailable = 'payload_limit'
        if unavailable is not None:
            return self.journal.observe(dict(event='capture', visit_id=visit_id,
                direction=direction, reason=unavailable))
        log_values = trace_values = None
        try:
            if plain_fields and self.log_policy.allow_content:
                log_values, _ = _plain_values(values, schema, plain_fields,
                                              self.boundary, limit=65536)
            if plain_fields and self.trace is not None and self.trace.boundary.policy.allow_content:
                trace_values, _ = _plain_values(values, schema, plain_fields,
                                                self.boundary, limit=65536)
                # Credential authorities are shared across destinations; this
                # content-only redaction does not import the log permission.
                trace_values = self.boundary.content_export({'values': trace_values}).get('values', {})
        except CaptureUnavailable:
            return self.journal.observe(dict(event='capture', visit_id=visit_id,
                direction=direction, reason='privacy_withheld'))
        def admitted_capture(row):
            # Replace only this invocation's registered subscriber. Other
            # producers and subscribers still emit synchronously at admission.
            live = dict(row)
            if log_values is not None:
                live.update(values=log_values, reason='captured')
                live.pop('truncated_fields', None)
            elif not self.log_policy.allow_content:
                live.pop('values', None)
                live['reason'] = 'withheld'
            self._emit(live)
            if trace_values is not None:
                span = self._spans.get(visit_id)
                if span is not None:
                    try:
                        span.event({'event_id': row['event_id'], 'direction': direction, 'values': trace_values})
                    except (Exception, asyncio.CancelledError):
                        pass
        return self.journal.capture(visit_id=visit_id, direction=direction,
            values=values if self.log_policy.allow_content else {}, schema=schema,
            plain_fields=plain_fields if self.log_policy.allow_content else None,
            _observer_override=(self._journal_observer, admitted_capture))

    def capture_json(self, *, visit_id, direction, values, schema, cause_id=None, capture_reason=None, transport_attempt_id=None, capture_source=None, tool_invocation_id=None, attempt_id=None):
        """Project log and trace content independently before either queues it."""
        from .capture import capture_reason as classify_capture, project_values
        report = set()
        provider_content = cause_id is not None and capture_source == 'provider'
        log_values = (project_values(values, schema, self.boundary, limit=65536, report=report,
                                     _provider_content=provider_content)
                      if self.log_policy.allow_content and capture_reason != 'withheld' else None)
        trace_values = None
        if self.trace is not None and self.trace.boundary.policy.allow_content and capture_reason != 'withheld':
            trace_report = set()
            trace_values = project_values(values, schema, self.boundary, limit=65536, report=trace_report,
                                          _provider_content=provider_content)
        def admitted_capture(row):
            live = dict(row)
            if log_values is not None:
                live.update(values=log_values, reason=classify_capture(log_values, report, capture_reason))
            else:
                live.pop('values', None)
                live['reason'] = 'withheld'
            self._emit(live)
            if trace_values is not None:
                span = self._spans.get(visit_id)
                if span is not None:
                    try:
                        event = dict(event_id=row['event_id'], direction=direction, values=trace_values,
                                     reason=classify_capture(trace_values, trace_report, capture_reason),
                                     capture_class='schema_user_io')
                        if tool_invocation_id is not None:
                            event['tool_invocation_id'] = tool_invocation_id
                        if cause_id is not None:
                            event['cause_id'] = cause_id
                        if transport_attempt_id is not None:
                            event['transport_attempt_id'] = transport_attempt_id
                        if capture_source is not None:
                            event['capture_source'] = capture_source
                        span.event(event)
                    except (Exception, asyncio.CancelledError):
                        pass
            elif self.trace is not None and not self.trace.boundary.policy.allow_content:
                span = self._spans.get(visit_id)
                if span is not None:
                    # Supply no content to the trace adapter. Its own policy
                    # adds destination-specific omission guidance.
                    try:
                        span.event({'values': {}})
                    except (Exception, asyncio.CancelledError):
                        pass
        return self.journal.capture_json(visit_id=visit_id, direction=direction,
            values=values, schema=schema, cause_id=cause_id, capture_reason=capture_reason, transport_attempt_id=transport_attempt_id, capture_source=capture_source,
            tool_invocation_id=tool_invocation_id, attempt_id=attempt_id,
            _observer_override=(self._journal_observer, admitted_capture))

    def close(self):
        for span in self._spans.values():
            try:
                span.attributes({'status': 'cancelled'})
                span.end()
            except (Exception, asyncio.CancelledError):
                pass
        self._spans.clear()
