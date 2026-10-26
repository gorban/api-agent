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
               component_passes=None, test_id=None, source_manifest=None):
        journal = journal or WorkflowJournal(definition,
            log_content=self.log_policy.allow_content, boundary=self.boundary,
            turn_id=turn_id, component_passes=component_passes,
            test_id=test_id, source_manifest=source_manifest)
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

    @property
    def projection(self):
        return self.journal.projection

    @property
    def turn_id(self):
        return self.journal.turn_id

    @property
    def invocation_id(self):
        return self.journal.invocation_id

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
            span = self.trace.begin(operation if tool else 'chat ' + selection, structural,
                parent=self._spans.get(row.get('visit_id')), kind='CLIENT',
                start_time=int(row['started_ms'] * 1_000_000) if row.get('started_ms') is not None else None)
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

    def capture_json(self, *, visit_id, direction, values, schema, cause_id=None, capture_reason=None):
        """Project log and trace content independently before either queues it."""
        from .capture import capture_reason as classify_capture, project_values
        report = set()
        log_values = (project_values(values, schema, self.boundary, limit=65536, report=report)
                      if self.log_policy.allow_content and capture_reason != 'withheld' else None)
        trace_values = None
        if self.trace is not None and self.trace.boundary.policy.allow_content and capture_reason != 'withheld':
            trace_report = set()
            trace_values = project_values(values, schema, self.boundary, limit=65536, report=trace_report)
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
                        if cause_id is not None:
                            event['cause_id'] = cause_id
                        span.event(event)
                    except (Exception, asyncio.CancelledError):
                        pass
        return self.journal.capture_json(visit_id=visit_id, direction=direction,
            values=values, schema=schema, cause_id=cause_id, capture_reason=capture_reason,
            _observer_override=(self._journal_observer, admitted_capture))

    def close(self):
        for span in self._spans.values():
            try:
                span.attributes({'status': 'cancelled'})
                span.end()
            except (Exception, asyncio.CancelledError):
                pass
        self._spans.clear()
