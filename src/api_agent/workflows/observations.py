"""Synchronous destination fan-out over one bounded journal identity issuer.

Raw capture values exist only on the caller stack. The journal retains its
existing log-gated previews; live diagnostics and traces get separate scrubbed
projections before either consumer can queue an event.
"""
import asyncio
import math
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


def _plain_capture_unavailable(values, schema, plain_fields):
    if not plain_fields:
        return None
    # Validate declarations even when content or its authority is unavailable.
    _plain_values({}, schema, plain_fields, None)
    properties = schema['properties']
    for field in plain_fields:
        if not isinstance(properties[field], Mapping):
            raise ValueError('Invalid plain workflow capture schema')
    if type(values) is not dict:
        return 'unsupported'
    unavailable = None
    for field in plain_fields:
        kind = properties[field].get('type')
        if kind not in ('string', 'boolean', 'integer', 'number', 'null'):
            unavailable = 'unsupported'
        if field not in values:
            continue
        value = values[field]
        if type(value) is str and len(value) > 65536:
            unavailable = 'payload_limit'
        elif kind in ('string', 'boolean', 'integer', 'number', 'null'):
            admitted = (kind == 'string' and type(value) is str
                or kind == 'boolean' and type(value) is bool
                or kind in ('integer', 'number') and type(value) is int and abs(value) <= (1 << 53) - 1
                or kind == 'number' and type(value) is float and math.isfinite(value)
                or kind == 'null' and value is None)
            if not admitted:
                unavailable = 'unsupported'
    return unavailable


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
        from api_agent.telemetry.operation import TraceLoss
        self.trace_losses = getattr(trace, 'losses', None) or TraceLoss()
        self._spans = {}
        self._visit_call_kinds = {}
        self._call_payloads = {}
        self._tool_spans = {}
        self._tool_visits = set()
        self._remote_pending = 0
        from threading import Lock
        self._remote_lock = Lock()
        self._call_fields = {}
        self._closed = False
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

    def provider_fields(self, cause_id, transport_id, request, result, *, provider_name=None):
        """The shared source supplies validated typed facts, never raw SDK bodies."""
        from api_agent.providers.llm import LLMRequest, LLMResult
        if not self._trace_active() or not isinstance(request, LLMRequest) or not isinstance(result, LLMResult):
            return
        from .attribution import current_workflow_visit
        visit = current_workflow_visit()
        if visit is None or visit[0] is not self or visit[1] not in self._spans:
            return
        key = (cause_id, transport_id)
        if key not in self._call_fields and len(self._call_fields) >= 256:
            self.trace_losses.record()
            return
        # Catalog request identity and validated actual response identity have
        # separate provenance; neither is established from arbitrary span data.
        identities = tuple(value for value in (request.selected_llm, result.actual_model) if value is not None)
        self.trace.admit_provider_model(result)
        from api_agent.telemetry.genai_fields import provider_fields
        fields = provider_fields(request, result, provider_name)
        from api_agent.telemetry.tracing import _structural
        fields = _structural(self.trace.boundary, fields, frozenset(identities))
        current = self._call_fields.setdefault(key, {})
        current.update(fields)

    def provider_diagnostics(self, cause_id, transport_id, diagnostics):
        if not self._trace_active():
            return
        key = (cause_id, transport_id)
        if key not in self._call_fields and len(self._call_fields) >= 256:
            self.trace_losses.record()
            return
        from api_agent.telemetry.genai_fields import diagnostic_fields
        fields = diagnostic_fields(diagnostics, self.trace.boundary)
        self._call_fields.setdefault(key, {}).update(fields)

    def remote_offer(self, call_id, visit_id):
        """Keep ownership on the invocation stack, including rejected admission."""
        if not self._trace_active():
            return None
        from api_agent.telemetry.worker_trace import WorkerTraceOffer
        span = self._spans.get(visit_id)
        pipeline = self.trace.pipeline_id
        if pipeline is None:
            return WorkerTraceOffer(self, call_id, {}, local=True)
        parent = span.export_context() if span is not None else None
        if parent is None:
            return WorkerTraceOffer(self, call_id, {})
        with self._remote_lock:
            if self._remote_pending >= 256:
                return WorkerTraceOffer(self, call_id, {})
            self._remote_pending += 1
        def release():
            with self._remote_lock:
                self._remote_pending -= 1
        return WorkerTraceOffer(self, call_id,
            dict(observation_parent=parent, observation_pipeline=pipeline), release)

    def finalize(self):
        return self.journal.finalize()

    def report_outcome(self, scope_id, outcome):
        """Explicit producer outcome while its originating scope is still open."""
        from api_agent.telemetry.outcomes import ExecutionOutcome
        if type(outcome) is not ExecutionOutcome:
            raise TypeError('Expected a versioned execution outcome')
        span = self._spans.get(scope_id)
        if span is not None:
            from api_agent.telemetry.operation import safe_trace
            safe_trace(lambda: span.outcome(outcome))

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
            self.trace_losses.record()

    def _trace_active(self):
        # Compatibility adapters without lifecycle state retain their established
        # manual contract. Runtime-owned SDK exporters expose explicit admission.
        return not self._closed and self.trace is not None and getattr(self.trace, 'active', True) is not False

    def _trace_row(self, row, *, _finalizing=False, _parent=None, _terminal_ns=None):
        if _finalizing and self._closed:
            self.trace_losses.record()
            return
        if not self._trace_active():
            return
        event = row['event']
        if event == 'call' and not _finalizing:
            from api_agent.telemetry.worker_trace import defer
            parent = self._spans.get(row.get('visit_id'))
            import time
            terminal_ns = int(row['stopped_ms'] * 1_000_000) if row.get('stopped_ms') is not None else time.time_ns()
            if defer(self, row['call_id'], lambda: self.trace_losses.run(
                    lambda: self._trace_row(row, _finalizing=True, _parent=parent, _terminal_ns=terminal_ns))):
                return
        if event == 'scope_start':
            if len(self._spans) >= 256:
                self.trace_losses.record()
                return
            kind = row.get('kind', 'node')
            owner = row.get('owner', 'workflow')
            if kind == 'node':
                self._visit_call_kinds[row['scope_id']] = self.journal._node_call_kinds.get(owner)
            structural = {'api_agent.' + key: row[key] for key in (
                'event_id', 'invocation_id', 'turn_id', 'scope_id', 'parent_id',
                'visit_id', 'attempt_id', 'attempt') if row.get(key) is not None}
            from api_agent.telemetry.outcomes import current_operation
            operation = current_operation(self.trace)
            parent = self._spans.get(row.get('parent_id'))
            if kind == 'graph' and operation is not None:
                parent = operation.span
            self._spans[row['scope_id']] = self.trace.begin(
                kind + ' ' + owner, structural, parent=parent,
                start_time=int(row['started_ms'] * 1_000_000) if row.get('started_ms') is not None else None)
        elif event == 'scope_end':
            span = self._spans.pop(row['scope_id'], None)
            self._visit_call_kinds.pop(row['scope_id'], None)
            self._tool_visits.discard(row['scope_id'])
            if span is not None:
                span.attributes({'status': 'error' if row.get('outcome') == 'failed' else
                                 'cancelled' if row.get('outcome') == 'cancelled' else 'ok',
                                 'duration_seconds': row.get('elapsed')})
                from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeState, OutcomeCause
                state = {'failed': OutcomeState.FAILURE, 'cancelled': OutcomeState.CANCELLED,
                         'passed': OutcomeState.SUCCESS}.get(row.get('outcome'), OutcomeState.UNKNOWN)
                if row.get('outcome') != 'passed':
                    span.outcome(ExecutionOutcome(state,
                        OutcomeCause.EXECUTION if state == OutcomeState.FAILURE else None))
                span.end(end_time=int(row['stopped_ms'] * 1_000_000) if row.get('stopped_ms') is not None else None)
        elif event == 'tool_start':
            self._tool_visits.add(row.get('visit_id'))
            if len(self._tool_spans) < 256:
                self._tool_spans[row['tool_invocation_id']] = self.trace.begin('execute_tool', {
                    'gen_ai.operation.name': 'execute_tool',
                    'gen_ai.tool.name': row.get('tool_name'),
                    'gen_ai.tool.call.id': row['tool_invocation_id'],
                    'api_agent.tool.instance': row.get('tool_instance'),
                }, parent=self._spans.get(row.get('visit_id')),
                    start_time=int(row['started_ms'] * 1_000_000))
            else:
                self.trace_losses.record()
        elif event == 'tool_end':
            span = self._tool_spans.pop(row['tool_invocation_id'], None)
            if span is not None:
                from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeState, OutcomeCause
                state = OutcomeState(row['outcome'])
                span.outcome(ExecutionOutcome(state, OutcomeCause.TOOL if state in
                    (OutcomeState.FAILURE, OutcomeState.PARTIAL) else None, row['tool_invocation_id']))
                parent = self._spans.get(row.get('visit_id'))
                if parent is not None and state in (OutcomeState.FAILURE, OutcomeState.PARTIAL):
                    parent.outcome(ExecutionOutcome(state, OutcomeCause.TOOL, row['tool_invocation_id']))
                span.end(end_time=int(row['stopped_ms'] * 1_000_000))
        elif event == 'call':
            # Central accounting supplies an id once; journal dedup runs before
            # this callback. Do not estimate or count the same usage at parents.
            tool = (self._visit_call_kinds.get(row.get('visit_id')) == 'deterministic'
                    and row.get('selection') is None and row.get('usage') is None)
            if tool and row.get('visit_id') in self._tool_visits:
                return
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
                                   ('output_tokens', 'output_tokens'),
                                   ('cached_input_tokens', 'cache_read.input_tokens'),
                                   ('cache_write_tokens', 'cache_write.input_tokens'),
                                   ('reasoning_tokens', 'reasoning.output_tokens')):
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
            physical_ids = {item['id'] for item in ledger['attempts']} if ledger is not None else set()
            field_keys = [key for key in self._call_fields if key[0] == row['call_id'] or key[1] in physical_ids]
            call_fields = {key:self._call_fields.pop(key) for key in field_keys}
            last_dispatch = ledger['attempts'][-1]['id'] if ledger is not None and ledger['attempts'] else None
            for key, fields in call_fields.items():
                if key[1] == last_dispatch:
                    structural.update(fields)
            if ledger is not None:
                from api_agent.telemetry.genai_fields import ledger_usage, transport_usage
                structural = {key:value for key,value in structural.items() if not key.startswith('gen_ai.usage.')}
                structural.update(ledger_usage(ledger))
            if not tool and callable(getattr(self.trace, 'record_call', None)):
                self.trace.record_call(structural, row.get('elapsed'), row.get('outcome', 'unknown'), attempts=ledger)
            from api_agent.telemetry.worker_trace import ownership, _MISSING
            remote_state = ownership(self, row['call_id'])
            if remote_state is not _MISSING and remote_state != 'inactive':
                physical_ids = {item['id'] for item in ledger['attempts']} if ledger is not None else set()
                for key in tuple(self._call_payloads):
                    if key[0] == row['call_id'] or key[1] in physical_ids:
                        self._call_payloads.pop(key)
                return
            span = self.trace.begin(operation if tool else 'chat ' + selection, structural,
                parent=_parent if _finalizing else self._spans.get(row.get('visit_id')), kind='CLIENT',
                start_time=int(row['started_ms'] * 1_000_000) if row.get('started_ms') is not None else
                    ledger['attempts'][0]['started_ms'] * 1_000_000 if _finalizing and ledger is not None and ledger['attempts'] else _terminal_ns)
            if ledger is not None:
                for item in ledger['attempts']:
                    child = self.trace.begin('transport attempt', {
                        'api_agent.transport_id': item['id'],
                        'api_agent.transport_index': item['index'],
                        'api_agent.transport_outcome': item['outcome'],
                        'api_agent.transport.terminal_known': item['stopped_ms'] is not None,
                        'api_agent.transport_status': item['status'],
                        'error.type': item['error_kind'],
                        **transport_usage(item),
                    }, parent=span, kind='CLIENT',
                        start_time=item['started_ms'] * 1_000_000)
                    for key, fields in call_fields.items():
                        if key[1] == item['id']:
                            child.structural({key:value for key,value in fields.items() if not key.startswith('gen_ai.usage.')})
                    if item['outcome'] not in ('started', 'observed'):
                        child.attributes({'status': 'cancelled' if item.get('error_kind') == 'cancelled' else
                                          'error' if item['outcome'] == 'failed' else 'ok'})
                    for (cause, attempt), phases in self._call_payloads.items():
                        if attempt == item['id']:
                            for direction, values in phases.items():
                                child.native_io(direction, values)
                    if item['stopped_ms'] is None:
                        self.trace_losses.record()
                        child.capture_loss(1)
                    child.end(end_time=item['stopped_ms'] * 1_000_000
                              if item['stopped_ms'] is not None else None)
            payload_keys = [(cause, attempt) for cause, attempt in self._call_payloads
                if cause == row['call_id'] or ledger is not None and attempt in
                   {item['id'] for item in ledger['attempts']}]
            for key in payload_keys:
                for direction, values in self._call_payloads.pop(key).items():
                    span.native_io(direction, values)
            span.attributes({'duration_seconds': row.get('elapsed'),
                             'status': 'error' if row.get('outcome') in ('failed', 'failure', 'partial') else
                                       'cancelled' if row.get('outcome') == 'cancelled' else 'ok'})
            span.end(end_time=_terminal_ns if _finalizing else int(row['stopped_ms'] * 1_000_000)
                if row.get('stopped_ms') is not None else None)

    def capture(self, *, visit_id, direction, values, schema, plain_fields=None):
        # Each destination is projected while raw values are on this stack,
        # before the journal or any callback can retain a row.
        if direction not in ('input', 'output'):
            raise ValueError('Invalid workflow capture direction')
        if type(visit_id) is not str or not visit_id:
            raise ValueError('Workflow capture requires a node visit')
        unavailable = _plain_capture_unavailable(values, schema, plain_fields)
        if unavailable is not None:
            return self.journal.observe(dict(event='capture', visit_id=visit_id,
                direction=direction, reason=unavailable))
        log_values = trace_values = None
        try:
            if plain_fields and self.log_policy.allow_content:
                log_values, _ = _plain_values(values, schema, plain_fields,
                                              self.boundary, limit=65536)
            if plain_fields and self._trace_active() and self.trace.boundary.policy.allow_content:
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
                        self.trace_losses.record()
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
        if self._trace_active() and self.trace.boundary.policy.allow_content and capture_reason != 'withheld':
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
                        if tool_invocation_id is not None:
                            native = self._tool_spans.get(tool_invocation_id)
                            if native is not None:
                                native.native_io(direction, trace_values)
                        elif cause_id is not None:
                            # Trace-only bounded projection, rechecked against the
                            # live credential authority again at SDK publication.
                            key = (cause_id, transport_attempt_id)
                            if key in self._call_payloads or len(self._call_payloads) < 256:
                                self._call_payloads.setdefault(key, {})[direction] = trace_values
                            else:
                                self.trace_losses.record()
                        else:
                            span.native_io(direction, trace_values)
                    except (Exception, asyncio.CancelledError):
                        self.trace_losses.record()
            elif self._trace_active() and not self.trace.boundary.policy.allow_content:
                span = self._spans.get(visit_id)
                if span is not None:
                    # Supply no content to the trace adapter. Its own policy
                    # adds destination-specific omission guidance.
                    try:
                        span.event({'values': {}})
                    except (Exception, asyncio.CancelledError):
                        self.trace_losses.record()
        return self.journal.capture_json(visit_id=visit_id, direction=direction,
            values=values, schema=schema, cause_id=cause_id, capture_reason=capture_reason, transport_attempt_id=transport_attempt_id, capture_source=capture_source,
            tool_invocation_id=tool_invocation_id, attempt_id=attempt_id,
            _observer_override=(self._journal_observer, admitted_capture))

    def close(self):
        self._closed = True
        # Abnormal/early close still respects child-before-parent lifetime.
        for span in reversed(tuple(self._tool_spans.values())):
            try:
                span.end()
            except (Exception, asyncio.CancelledError):
                self.trace_losses.record()
        self._tool_spans.clear()
        for span in reversed(tuple(self._spans.values())):
            try:
                span.attributes({'status': 'cancelled'})
                span.end()
            except (Exception, asyncio.CancelledError):
                self.trace_losses.record()
        self._spans.clear()
        self._tool_visits.clear()
        self._call_payloads.clear()
        self._call_fields.clear()
