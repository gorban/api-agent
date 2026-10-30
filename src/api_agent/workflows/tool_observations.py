"""Typed tool lifecycle identities, separate from LLM accounting observations."""
import asyncio
import math
from uuid import UUID


TOOL_FIELDS = ('tool_invocation_id', 'tool_name', 'tool_instance')


def tool_occurrence(packet, event):
    """Bind a tool payload to its exact recorded graph, event and node visit."""
    return dict(kind='tool', invocation_id=packet['invocation_id'],
        event_id=event['event_id'], visit_id=event['visit_id'],
        attempt_id=event.get('attempt_id'), tool_invocation_id=event['tool_invocation_id'],
        phase='request' if event['direction'] == 'input' else 'response')


def validate_tool_events(events):
    """Reject conflicting identities; return false for unfinished or lost evidence."""
    visits, tools, node_times = {}, {}, {}
    for event in events:
        if type(event) is not dict:
            raise ValueError('Invalid tool observation event')
        kind = event.get('event')
        if kind == 'scope_start' and event.get('kind') == 'node':
            visits[event['scope_id']] = (event.get('attempt_id'), True)
            node_times[event['scope_id']] = event.get('started_ms')
        if kind == 'scope_end' and event.get('kind') == 'node':
            visits[event['scope_id']] = (event.get('attempt_id'), False)
            for row in tools.values():
                if row['owner'][0] == event['scope_id'] and row['end'] is not None and row['end'] > event.get('stopped_ms', -1):
                    raise ValueError('Tool interval exceeds its node visit')
        identifier = event.get('tool_invocation_id')
        if identifier is None:
            if kind in ('tool_start', 'tool_end') or any(key in event for key in TOOL_FIELDS):
                raise ValueError('Tool lifecycle lacks its identity')
            continue
        try:
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError()
        except ValueError:
            raise ValueError('Invalid tool invocation identity') from None
        if kind not in ('tool_start', 'tool_end', 'capture') or any(key in event for key in
                ('call_id', 'cause_id', 'usage', 'selection', 'actual_model',
                 'transport_attempts', 'transport_attempt_id', 'capture_source')):
            raise ValueError('Tool evidence contains LLM call fields')
        common = {'event', 'event_id', 'sequence', 'invocation_id', 'turn_id',
                  'visit_id', 'attempt_id', 'tool_invocation_id'}
        fields = ({'tool_name', 'tool_instance', 'started_ms'} if kind == 'tool_start' else
                  {'tool_name', 'tool_instance', 'stopped_ms', 'outcome'} if kind == 'tool_end' else
                  {'direction', 'values', 'reason', 'capture_class'})
        if set(event) - common - fields:
            raise ValueError('Unexpected tool observation field')
        owner = (event.get('visit_id'), event.get('attempt_id'))
        if visits.get(owner[0]) != (owner[1], True):
            raise ValueError('Tool evidence lacks its open node visit')
        import re
        if any(type(event[key]) is not str or re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:/-]{0,127}', event[key]) is None
               for key in ('tool_name', 'tool_instance') if key in event):
            raise ValueError('Unsafe tool observation label')
        if kind == 'tool_start':
            if identifier in tools:
                raise ValueError('Duplicate tool start')
            started = event.get('started_ms')
            parent_start = node_times.get(owner[0])
            if (type(started) not in (int, float) or not math.isfinite(started) or started < 0
                    or type(parent_start) not in (int, float) or not math.isfinite(parent_start)
                    or started < parent_start):
                raise ValueError('Invalid tool start time')
            tools[identifier] = dict(owner=owner, start=started, end=None,
                labels=tuple(event.get(key) for key in TOOL_FIELDS[1:]), phases=set(), lost=False)
            continue
        state = tools.get(identifier)
        if state is None or state['owner'] != owner or state['end'] is not None:
            raise ValueError('Detached or repeated tool observation')
        if kind == 'tool_end':
            stopped = event.get('stopped_ms')
            if (type(stopped) not in (int, float) or not math.isfinite(stopped)
                    or stopped < state['start'] or event.get('outcome') not in
                    ('success', 'partial', 'failure', 'cancelled')
                    or tuple(event.get(key) for key in TOOL_FIELDS[1:]) != state['labels']):
                raise ValueError('Invalid tool completion')
            state['end'] = stopped
        else:
            direction = event.get('direction')
            if (direction not in ('input', 'output') or direction in state['phases']
                    or direction == 'output' and 'input' not in state['phases']):
                raise ValueError('Duplicate or invalid tool capture phase')
            state['phases'].add(direction)
            state['lost'] |= event.get('reason') in ('unavailable', 'truncated', 'partial')
    return all(row['end'] is not None and row['phases'] == {'input', 'output'}
               and not row['lost'] for row in tools.values())


def tool_payload_schema(schema, field):
    """Rebase admitted local references when placing a schema in an envelope."""
    from copy import deepcopy
    from referencing.jsonschema import DRAFT202012
    result = deepcopy(schema)
    pending = [result]
    while pending:
        current = pending.pop()
        if type(current) is dict:
            if '$ref' in current:
                reference = current['$ref']
                if type(reference) is not str or not reference.startswith('#/'):
                    raise ValueError('Tool capture schema requires local references')
                current['$ref'] = '#/properties/' + field + '/' + reference[2:]
            pending.extend(DRAFT202012.subresources_of(current))
    return result


class ToolObservation:
    """Retain under the existing authority lease; fan out only after release."""
    def __init__(self, spec, *, alias=None, instance=None):
        from uuid import uuid4
        from .attribution import current_workflow_visit, current_tool_capture_services
        self.owner = current_workflow_visit()
        self.services = current_tool_capture_services()
        self.journal = None if self.owner is None else self.owner[0]
        self.identifier = str(uuid4())
        self.spec, self.alias, self.instance = spec, alias or spec.name, instance
        self.labels, self.started = {}, None

    def loss(self):
        if self.journal is not None:
            getattr(self.journal, 'journal', self.journal).record_evidence_loss()

    def begin(self, admitted):
        import re
        import time
        for field, original in (('tool_name', self.alias), ('tool_instance', self.instance)):
            if (type(original) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:/-]{0,127}', original)
                    and admitted.get(field) == original):
                self.labels[field] = original
        # Complete remote projection and the live local owner must both admit
        # structural labels; printability or LOG_CONTENT alone is insufficient.
        boundary = getattr(self.journal, 'boundary', None) or self.journal._boundary
        safe_labels = boundary.content_export(self.labels)
        self.labels = {key: value for key, value in self.labels.items() if safe_labels.get(key) == value}
        self.started = time.time() * 1000
        self.journal.observe(dict(event='tool_start', tool_invocation_id=self.identifier,
            visit_id=self.owner[1], attempt_id=self.owner[2], started_ms=self.started, **self.labels))

    async def capture(self, direction, values, schema, *, unavailable=False, before_dispatch=None):
        if self.journal is None:
            if before_dispatch is not None:
                before_dispatch()
            return None
        from .capture_authority import capture_node
        from .journal import defer_capture_callbacks
        from api_agent.telemetry.privacy import EmissionBoundary
        observation = self
        services = self.services or {}
        authority = (callable(services.get('capture_authority'))
                     or type(services.get('emissions')) is EmissionBoundary)
        callbacks = []
        barrier_passed = before_dispatch is None
        barrier_error = False
        class AdmissionJournal:
            def capture_json(self, **kwargs):
                nonlocal barrier_passed, barrier_error
                if not authority or unavailable:
                    kwargs.update(values={}, capture_reason='unavailable' if unavailable else 'withheld')
                if before_dispatch is not None:
                    try:
                        before_dispatch()
                    except Exception:
                        barrier_error = True
                        raise
                    barrier_passed = True
                    observation.begin(kwargs['values'])
                return observation.journal.capture_json(**kwargs)
        try:
            # capture_node owns the complete credential lease. This outer scope
            # also defers the no-remote-authority path; the wrapper's independent
            # destination projections and journal retention remain inside it.
            with defer_capture_callbacks(callbacks):
                result = await capture_node(AdmissionJournal(), services,
                    visit_id=self.owner[1], attempt_id=self.owner[2],
                    tool_invocation_id=self.identifier, direction=direction,
                    values=values, schema=schema)
        except Exception:
            if barrier_error:
                raise
            self.loss()
            if not barrier_passed and before_dispatch is not None:
                before_dispatch()
            result = None
        for callback, row in callbacks:
            try:
                callback(row)
            except (Exception, asyncio.CancelledError):
                self.loss()
        return result

    def end(self, outcome):
        if self.journal is None or self.started is None:
            return
        import time
        try:
            self.journal.observe(dict(event='tool_end', tool_invocation_id=self.identifier,
                visit_id=self.owner[1], attempt_id=self.owner[2], stopped_ms=time.time() * 1000,
                outcome=outcome, **self.labels))
        except Exception:
            self.loss()
