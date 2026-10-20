"""Bounded owner-sequenced workflow observations for recorded tests."""
from hashlib import sha256
import json
import math
import re
import time
from threading import Lock
from uuid import uuid4, UUID

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

_MAX_EVENTS = 8192
_MAX_BYTES = 4 * 1024 * 1024
_SAFE_LABEL = re.compile(r'[A-Za-z][A-Za-z0-9_.:/-]{0,127}\Z')
_UNSAFE_FIELD = re.compile(r'(?i)(?:auth|token|secret|password|credential|cookie|session|continuation|reasoning|analysis|thought|deliberation|scratchpad|internal|hidden|chain.of.thought|private.key|bearer|(?:^|_)cot(?:_|$))')


def _plain_values(data, schema, fields, boundary):
    """Only owner-classified top-level plain user I/O scalars may be retained."""
    if type(data) is not dict or type(schema) is not dict or schema.get('type') != 'object':
        raise ValueError('Plain workflow capture requires a declared object')
    properties = schema.get('properties')
    if type(properties) is not dict:
        raise ValueError('Plain workflow capture requires declared fields')
    if (type(fields) not in (tuple, list) or len(fields) > 16
            or any(type(field) is not str for field in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError('Invalid plain workflow capture fields')
    result = {}
    truncated_fields = []
    for key in fields:
        if (type(key) is not str or not _SAFE_LABEL.fullmatch(key)
                or _UNSAFE_FIELD.search(key) or key not in properties):
            raise ValueError('Unsafe plain workflow capture field')
        if key not in data:
            continue
        rule, value = properties[key], data[key]
        if type(rule) is not dict:
            raise ValueError('Invalid plain workflow capture schema')
        kind = rule.get('type')
        if kind == 'string' and type(value) is str:
            if len(value) > 65536:
                raise ValueError('Plain workflow capture input exceeds limit')
            # Scrub the original scalar: a secret can straddle the display limit.
            value = boundary.content_export({'value': value}).get('value')
            if type(value) is not str:
                raise ValueError('Plain workflow capture value was withheld')
            if len(value) > 512:
                truncated_fields.append(key)
            result[key] = value[:512]
        elif kind == 'boolean' and type(value) is bool:
            result[key] = value
        elif kind in ('integer', 'number') and type(value) is int and abs(value) <= (1 << 53) - 1:
            result[key] = value
        elif kind == 'number' and type(value) is float and math.isfinite(value):
            result[key] = value
        elif value is None and kind == 'null':
            result[key] = None
        else:
            raise ValueError('Plain workflow capture field is not scalar')
    return result, truncated_fields


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _capture_contract(value, depth=0):
    if depth > 4:
        return False
    if type(value) is dict:
        return (len(value) <= 32 and all(type(key) is str
                and _SAFE_LABEL.fullmatch(key) and not _UNSAFE_FIELD.search(key)
                and _capture_contract(item, depth + 1) for key, item in value.items()))
    if type(value) is list:
        return len(value) <= 16 and all(_capture_contract(item, depth + 1) for item in value)
    return (value is None or type(value) is bool
            or type(value) is int and abs(value) <= (1 << 53) - 1
            or type(value) is float and math.isfinite(value)
            or type(value) is str and len(value) <= 512)


def projection(definition):
    """Describe the admitted selection without exposing machine paths or source text."""
    if definition is None:
        raise ValueError('An admitted workflow definition is required')
    steps = [dict(id=step.id, kind=step.kind, parent=step.parent)
             for step in definition.layout.steps]
    edges = []
    for parent in (None, *(step.id for step in definition.layout.steps if step.kind == 'component')):
        children = [step for step in definition.layout.steps if step.parent == parent]
        if not children:
            continue
        owner = parent or 'GRAPH'
        edges.append(dict(owner=owner, source='START', target=children[0].id, condition='always', implicit=True))
        for index, step in enumerate(children):
            declared = [row for row in definition.layout.tables['Edges'] if row['From'] == step.id]
            if declared:
                edges.extend(dict(owner=owner, source=step.id, target=row['To'],
                                  condition=row['When'], implicit=False) for row in declared)
            else:
                edges.append(dict(owner=owner, source=step.id,
                                  target=children[index + 1].id if index + 1 < len(children) else 'END',
                                  condition='always', implicit=True))
    result = dict(version=1, graph_id=definition.graph_id, definition_id=definition.definition_id,
                  entry_root=definition.entry_root, steps=steps, edges=edges,
                  component_passes=[dict(id=row['ID'], max_passes=int(row['Max passes']),
                                         guard=row['Guard'])
                                    for row in definition.layout.tables.get('Components', ())],
                  resources=[dict(kind=item.kind, logical_path=item.logical_path,
                                  sha256=item.sha256, length=len(item.content_bytes))
                             for item in definition.resources],
                  replacements=[dict(target_id=item.target_id, root_id=item.root_id)
                                for item in definition.replacements])
    if len(_encoded(result)) > 1024 * 1024:
        raise ValueError('Workflow projection exceeds 1 MiB')
    return result


def _routes_complete(projection_data, events):
    """Validate the causal scope tree and each owner's sequential route."""
    steps = {row['id']: row for row in projection_data['steps']}
    pass_components = {row['id']: row for row in projection_data['component_passes']}
    declared = {}
    for row in projection_data['edges']:
        declared.setdefault((row['owner'], row['source'], row['target'], row['implicit']), set()).add(row['condition'])
    scopes, open_scopes, routes, pending = {}, [], {}, {}
    passes, node_outcomes = {}, set()
    graph_roots = 0
    call_ids = set()
    for event in events:
        kind = event.get('event')
        if kind == 'scope_start':
            sid, parent, owner = event.get('scope_id'), event.get('parent_id'), event.get('owner')
            skind = event.get('kind')
            if not sid or sid in scopes:
                return False
            if skind == 'graph':
                graph_roots += 1
                if graph_roots != 1 or parent is not None or owner != 'GRAPH' or open_scopes:
                    return False
            else:
                step = steps.get(owner)
                if (step is None or step['kind'] != skind or not open_scopes
                        or open_scopes[-1] != parent or parent not in scopes
                        or scopes[parent]['end'] is not None):
                    return False
                expected_parent = step['parent'] or 'GRAPH'
                if scopes[parent]['owner'] != expected_parent:
                    return False
                route_key = (expected_parent, event.get('attempt_id'))
                if pending.get(route_key) != owner:
                    return False
                pending.pop(route_key)
            scopes[sid] = dict(event, end=None)
            open_scopes.append(sid)
            if skind == 'component' and owner in pass_components:
                passes[sid] = dict(next_number=1, active=None, accepted=False,
                                   route_finished=False)
        elif kind == 'scope_end':
            sid = event.get('scope_id')
            if not open_scopes or open_scopes[-1] != sid or sid not in scopes:
                return False
            start = scopes[sid]
            if (start['end'] is not None or any(start.get(key) != event.get(key)
                    for key in ('kind', 'owner', 'parent_id', 'attempt_id', 'started_ms'))
                    or type(event.get('stopped_ms')) not in (int, float)
                    or event['stopped_ms'] < start.get('started_ms', 0)):
                return False
            if start['kind'] == 'node' and event.get('outcome') == 'passed' and sid not in node_outcomes:
                return False
            if (start['kind'] == 'component' and sid in passes
                    and event.get('outcome') == 'passed' and not passes[sid]['accepted']):
                return False
            start['end'] = event
            open_scopes.pop()
        elif kind == 'edge':
            owner, source, target = event.get('owner'), event.get('source'), event.get('target')
            attempt = event.get('attempt_id')
            key = (owner, attempt)
            if owner in pass_components and target == 'END':
                if not open_scopes or scopes[open_scopes[-1]]['owner'] != owner:
                    return False
            conditions = declared.get((owner, source, target, event.get('implicit')))
            if conditions is None or event.get('condition', 'always') not in conditions:
                return False
            if key not in routes:
                if source != 'START':
                    return False
                routes[key] = 'START'
            if routes[key] != source or key in pending or routes[key] == 'END':
                return False
            if source == 'START':
                if event.get('visit_id') is not None:
                    return False
                if owner in pass_components:
                    if not open_scopes or scopes[open_scopes[-1]]['owner'] != owner:
                        return False
                    state = passes.get(open_scopes[-1])
                    if (state is None or state['active'] != attempt
                            or state['route_finished']):
                        return False
            else:
                sid = event.get('visit_id')
                scope = scopes.get(sid)
                if (scope is None or scope['end'] is None or scope['owner'] != source
                        or scope.get('attempt_id') != attempt
                        or scope['end']['sequence'] != event['sequence'] - 1):
                    return False
            routes[key] = target
            if owner in pass_components and target == 'END':
                passes[open_scopes[-1]]['route_finished'] = True
            if target != 'END':
                pending[key] = target
        elif kind == 'attempt':
            sid = event.get('scope_id')
            state = passes.get(sid)
            row = pass_components.get(event.get('component'))
            if (state is None or not open_scopes or open_scopes[-1] != sid
                    or row is None or scopes[sid]['owner'] != row['id']
                    or type(event.get('attempt')) is not int
                    or event['attempt'] != state['next_number']
                    or event['attempt'] > row['max_passes']
                    or event.get('max_passes') != row['max_passes']
                    or state['active'] is not None or state['accepted']
                    or not event.get('attempt_id')):
                return False
            state['active'] = event['attempt_id']
            state['route_finished'] = False
        elif kind == 'guard':
            sid = event.get('scope_id')
            state = passes.get(sid)
            row = pass_components.get(event.get('component'))
            guard_visit = any(scope['owner'] == row['guard']
                              and scope['kind'] == 'node'
                              and scope.get('attempt_id') == state['active']
                              and scope['end'] is not None
                              and scope['end'].get('outcome') == 'passed'
                              for scope in scopes.values()) if state is not None and row is not None else False
            if (state is None or not open_scopes or open_scopes[-1] != sid
                    or event.get('component') != scopes[sid]['owner']
                    or event.get('attempt') != state['next_number']
                    or state['active'] is None or not state['route_finished']
                    or type(event.get('accepted')) is not bool or not guard_visit):
                return False
            state['accepted'] = event['accepted']
            state['active'] = None
            state['next_number'] += 1
        elif kind == 'node':
            sid = event.get('scope_id')
            if (sid not in scopes or scopes[sid]['kind'] != 'node'
                    or scopes[sid]['end'] is not None or sid not in open_scopes
                    or event.get('node') != scopes[sid]['owner']
                    or sid in node_outcomes):
                return False
            node_outcomes.add(sid)
        elif kind == 'call':
            sid, call_id = event.get('visit_id'), event.get('call_id')
            scope = scopes.get(sid)
            if (not call_id or call_id in call_ids or scope is None
                    or scope['kind'] != 'node' or scope['end'] is not None
                    or sid not in open_scopes
                    or scope.get('attempt_id') != event.get('attempt_id')):
                return False
            call_ids.add(call_id)
        elif kind == 'capture':
            scope = scopes.get(event.get('visit_id'))
            if scope is None or scope['kind'] != 'node' or scope['end'] is not None:
                return False
        else:
            return False
    return (graph_roots == 1 and not open_scopes and not pending
            and routes.get(('GRAPH', None)) == 'END'
            and all(cursor == 'END' for cursor in routes.values()))


class WorkflowJournal:
    """Accept only structural events, scrub before retention, and disclose gaps."""

    def __init__(self, definition, *, log_content=False, secrets=(), test_id=None,
                 source_manifest=None, boundary=None, turn_id=None):
        self.projection = projection(definition)
        self.invocation_id = str(uuid4())
        self.turn_id = str(uuid4()) if turn_id is None else turn_id
        try:
            if type(self.turn_id) is not str or str(UUID(self.turn_id)) != self.turn_id:
                raise ValueError
        except ValueError:
            raise ValueError('Invalid workflow turn identity') from None
        self.test_id = test_id
        self.source_manifest = source_manifest
        if boundary is not None and not isinstance(boundary, EmissionBoundary):
            raise TypeError('Workflow boundary must be an emission boundary')
        self._boundary = boundary or EmissionBoundary(ContentPolicy(True), secrets=secrets)
        self._events = []
        self._call_ids = {}
        self._lock = Lock()
        self._bytes = 0
        self._complete = False
        self._finalized = False
        self._gap = None
        self._log_content = log_content is True

    def observe(self, row):
        self._observe(row, allow_values=False)

    def _observe(self, row, *, allow_values):
        if type(row) is not dict or type(row.get('event')) is not str:
            raise ValueError('Invalid workflow observation')
        if 'values' in row and not allow_values:
            raise ValueError('Workflow values require declared capture')
        # Explicit allowlist prevents accidental retention of inputs, outputs,
        # credentials, continuation state, or exception objects.
        allowed = ('event', 'scope_id', 'parent_id', 'owner', 'kind', 'node', 'component',
                   'attempt', 'max_passes', 'source', 'target', 'condition', 'implicit', 'elapsed',
                   'started_ms', 'stopped_ms', 'declared_timeout', 'effective_timeout',
                   'timeout_key', 'outcome', 'accepted', 'visit_id', 'attempt_id',
                   'call_id', 'cause_id', 'usage', 'selection', 'direction',
                   'values', 'reason', 'accounting', 'plain_fields', 'capture_class',
                   'truncated_fields')
        for key in ('event', 'owner', 'kind', 'node', 'component', 'source', 'condition',
                    'target', 'outcome', 'timeout_key', 'selection'):
            value = row.get(key)
            if value is not None and (type(value) is not str or _SAFE_LABEL.fullmatch(value) is None):
                raise ValueError('Unsafe workflow observation label')
        for key in ('scope_id', 'parent_id', 'visit_id', 'attempt_id', 'call_id', 'cause_id'):
            value = row.get(key)
            if value is not None and (type(value) is not str or len(value) > 64):
                raise ValueError('Invalid workflow observation identity')
        clean = self._boundary.content_export({key: row[key] for key in allowed if key in row})
        clean.update(sequence=len(self._events) + 1, event_id=str(uuid4()),
                     invocation_id=self.invocation_id, turn_id=self.turn_id)
        size = len(_encoded(clean))
        with self._lock:
            if self._finalized:
                raise ValueError('Journal already finalized')
            if self._gap is not None:
                return
            if clean['event'] == 'call':
                visit_id = clean.get('visit_id')
                open_visit = False
                for prior in self._events:
                    if prior.get('scope_id') == visit_id:
                        if prior.get('event') == 'scope_start' and prior.get('kind') == 'node':
                            open_visit = prior.get('attempt_id') == clean.get('attempt_id')
                        elif prior.get('event') == 'scope_end':
                            open_visit = False
                if not open_visit:
                    raise ValueError('Leaf call has no open node visit')
                call_id = clean.get('call_id')
                previous = self._call_ids.get(call_id)
                if previous is not None:
                    if previous != (visit_id, clean.get('attempt_id')):
                        raise ValueError('Leaf call identity has conflicting owner')
                    return
            clean['sequence'] = len(self._events) + 1
            size = len(_encoded(clean))
            if len(self._events) >= _MAX_EVENTS or self._bytes + size > _MAX_BYTES:
                self._gap = 'event_limit'
                return
            self._events.append(clean)
            self._bytes += size
            if clean['event'] == 'call':
                self._call_ids[clean['call_id']] = (clean['visit_id'], clean.get('attempt_id'))

    def capture(self, *, visit_id, direction, values, schema, plain_fields=None):
        """Capture only owner-classified plain user I/O before observation/IPC."""
        if direction not in ('input', 'output'):
            raise ValueError('Invalid workflow capture direction')
        if type(visit_id) is not str or not visit_id:
            raise ValueError('Workflow capture requires a node visit')
        if not self._log_content or not plain_fields:
            self.observe(dict(event='capture', visit_id=visit_id, direction=direction,
                              reason='withheld' if not self._log_content else 'unclassified'))
            return
        safe, truncated_fields = _plain_values(values, schema, plain_fields, self._boundary)
        clean = self._boundary.content_export({'values': safe}).get('values')
        event = dict(event='capture', visit_id=visit_id, direction=direction,
                     reason='truncated' if truncated_fields else 'captured',
                     values=clean, plain_fields=sorted(plain_fields),
                     capture_class='plain_user_io')
        if truncated_fields:
            event['truncated_fields'] = sorted(truncated_fields)
        self._observe(event, allow_values=True)

    def record_call(self, *, visit_id, attempt_id=None, usage=None, selection=None,
                    elapsed=None, outcome='completed', call_id=None):
        if type(visit_id) is not str or not visit_id:
            raise ValueError('A leaf call requires a node visit owner')
        if usage is not None and (type(usage) is not dict or set(usage) - {
                'input_tokens', 'output_tokens', 'cached_input_tokens',
                'reasoning_tokens', 'cache_write_tokens', 'total_tokens', 'estimated_cost_usd',
                'charged_cost_usd', 'basis'}):
            raise ValueError('Invalid leaf call accounting')
        if usage is not None:
            for key, value in usage.items():
                if key == 'basis':
                    if type(value) is not str or _SAFE_LABEL.fullmatch(value) is None:
                        raise ValueError('Invalid leaf call accounting basis')
                elif value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError('Invalid leaf call accounting amount')
        unverified_tool = call_id is None
        call_id = str(uuid4()) if unverified_tool else call_id
        try:
            if type(call_id) is not str or str(UUID(call_id)) != call_id:
                raise ValueError
        except ValueError:
            raise ValueError('Invalid leaf call identity') from None
        stopped_ms = time.time() * 1000
        started_ms = stopped_ms - elapsed * 1000 if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None
        self.observe(dict(event='call', call_id=call_id, visit_id=visit_id,
                          attempt_id=attempt_id, usage=usage, selection=selection,
                          elapsed=elapsed, started_ms=started_ms,
                          stopped_ms=stopped_ms if started_ms is not None else None,
                          outcome=outcome,
                          accounting='unverified_tool' if unverified_tool else 'central_call_id'))
        return call_id

    def finalize(self):
        with self._lock:
            if self._finalized:
                raise ValueError('Journal already finalized')
            self._finalized = True
            events = json.loads(_encoded(self._events))
            complete = self._gap is None and _routes_complete(self.projection, events)
            payload = dict(version=1, projection=json.loads(_encoded(self.projection)),
                           source_manifest=json.loads(_encoded(self.source_manifest)),
                           invocation_id=self.invocation_id, turn_id=self.turn_id,
                           test_id=self.test_id, events=events,
                           completeness='complete' if complete else 'partial',
                           gap=self._gap, content='permitted_scrubbed' if self._log_content else 'withheld')
            payload['sha256'] = sha256(_encoded(payload)).hexdigest()
            return payload


def validate_journal(payload, *, expected_definition=None):
    """Admit only the already-scrubbed, bounded structural envelope."""
    if type(payload) is not dict or payload.get('version') != 1:
        raise ValueError('Invalid workflow journal version')
    if len(_encoded(payload)) > _MAX_BYTES + 1024 * 1024:
        raise ValueError('Workflow journal exceeds retention bound')
    expected = payload.get('sha256')
    unsigned = {key: value for key, value in payload.items() if key != 'sha256'}
    if type(expected) is not str or sha256(_encoded(unsigned)).hexdigest() != expected:
        raise ValueError('Workflow journal digest mismatch')
    if set(payload) != {'version', 'projection', 'source_manifest', 'invocation_id', 'turn_id', 'test_id',
                        'events', 'completeness', 'gap', 'content', 'sha256'}:
        raise ValueError('Unexpected workflow journal field')
    projection_data = payload['projection']
    if expected_definition is not None and projection_data != projection(expected_definition):
        raise ValueError('Workflow projection differs from independently admitted definition')
    if (type(projection_data) is not dict or set(projection_data) != {
            'version', 'graph_id', 'definition_id', 'entry_root', 'steps', 'edges', 'component_passes',
            'resources', 'replacements'} or projection_data['version'] != 1
            or projection_data['entry_root'] not in ('base', 'pack')
            or type(projection_data['graph_id']) is not str
            or _SAFE_LABEL.fullmatch(projection_data['graph_id']) is None
            or type(projection_data['definition_id']) is not str
            or re.fullmatch('[0-9a-f]{64}', projection_data['definition_id']) is None):
        raise ValueError('Invalid workflow projection')
    for step in projection_data['steps']:
        if (type(step) is not dict or set(step) != {'id', 'kind', 'parent'}
                or type(step['id']) is not str or _SAFE_LABEL.fullmatch(step['id']) is None
                or step['kind'] not in ('node', 'component')
                or (step['parent'] is not None and (type(step['parent']) is not str
                    or _SAFE_LABEL.fullmatch(step['parent']) is None))):
            raise ValueError('Invalid workflow projection step')
    for edge in projection_data['edges']:
        if (type(edge) is not dict or set(edge) != {'owner', 'source', 'target', 'condition', 'implicit'}
                or any(type(edge[key]) is not str or _SAFE_LABEL.fullmatch(edge[key]) is None
                       for key in ('owner', 'source', 'target', 'condition'))
                or type(edge['implicit']) is not bool):
            raise ValueError('Invalid workflow projection edge')
    for component in projection_data['component_passes']:
        if (type(component) is not dict or set(component) != {'id', 'max_passes', 'guard'}
                or type(component['id']) is not str or _SAFE_LABEL.fullmatch(component['id']) is None
                or type(component['guard']) is not str or _SAFE_LABEL.fullmatch(component['guard']) is None
                or type(component['max_passes']) is not int
                or not 1 <= component['max_passes'] <= 99):
            raise ValueError('Invalid workflow component pass contract')
    for resource in projection_data['resources']:
        path = resource.get('logical_path') if type(resource) is dict else None
        if (type(resource) is not dict or set(resource) != {'kind', 'logical_path', 'sha256', 'length'}
                or type(path) is not str or not path or path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/'))
                or type(resource['sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}', resource['sha256']) is None
                or type(resource['length']) is not int or resource['length'] < 0):
            raise ValueError('Invalid workflow projection resource')
    for replacement in projection_data['replacements']:
        if (type(replacement) is not dict or set(replacement) != {'target_id', 'root_id'}
                or type(replacement['target_id']) is not str
                or _SAFE_LABEL.fullmatch(replacement['target_id']) is None
                or replacement['root_id'] not in ('base', 'pack')):
            raise ValueError('Invalid workflow projection replacement')
    if payload['content'] not in ('withheld', 'permitted_scrubbed'):
        raise ValueError('Invalid capture policy')
    source_manifest = payload['source_manifest']
    if source_manifest is not None:
        if type(source_manifest) is not dict or source_manifest.get('version') != 1:
            raise ValueError('Invalid workflow source manifest')
        signature = source_manifest.get('sha256')
        unsigned_source = {key: value for key, value in source_manifest.items() if key != 'sha256'}
        if type(signature) is not str or sha256(_encoded(unsigned_source)).hexdigest() != signature:
            raise ValueError('Workflow source manifest digest mismatch')
    events = payload['events']
    if type(events) is not list or len(events) > _MAX_EVENTS:
        raise ValueError('Invalid workflow event count')
    allowed = {'event', 'scope_id', 'parent_id', 'owner', 'kind', 'node', 'component',
               'attempt', 'max_passes', 'source', 'target', 'condition', 'implicit', 'elapsed',
               'started_ms', 'stopped_ms', 'declared_timeout', 'effective_timeout',
               'timeout_key', 'outcome', 'accepted', 'sequence', 'event_id',
               'invocation_id', 'turn_id', 'visit_id', 'attempt_id', 'call_id',
               'cause_id', 'usage', 'selection', 'direction', 'values', 'reason', 'accounting',
               'plain_fields', 'capture_class', 'truncated_fields'}
    event_ids = set()
    for sequence, event in enumerate(events, 1):
        if type(event) is not dict or set(event) - allowed or event.get('sequence') != sequence:
            raise ValueError('Invalid workflow event sequence or field')
        event_id = event.get('event_id')
        try:
            valid_event_id = type(event_id) is str and str(UUID(event_id)) == event_id
        except ValueError:
            valid_event_id = False
        if not valid_event_id or event_id in event_ids:
            raise ValueError('Invalid or duplicate workflow event identity')
        event_ids.add(event_id)
        if event.get('invocation_id') != payload['invocation_id'] or event.get('turn_id') != payload['turn_id']:
            raise ValueError('Workflow event owner mismatch')
        if event.get('event') == 'capture':
            if payload['content'] == 'withheld' and 'values' in event:
                raise ValueError('Withheld workflow capture contains values')
            if 'values' in event and not _capture_contract(event['values']):
                raise ValueError('Unsafe workflow capture values')
            if 'values' in event:
                fields = event.get('plain_fields')
                truncated = event.get('truncated_fields', [])
                if (event.get('capture_class') != 'plain_user_io'
                        or type(fields) is not list or len(fields) > 16
                        or any(type(field) is not str or _UNSAFE_FIELD.search(field)
                               for field in fields)
                        or type(truncated) is not list or len(truncated) > 16
                        or any(type(field) is not str or _UNSAFE_FIELD.search(field)
                               for field in truncated)
                        or type(event['values']) is not dict
                        or len(set(truncated)) != len(truncated)
                        or set(truncated) - set(fields)
                        or set(truncated) - set(event['values'])
                        or event.get('reason') != ('truncated' if truncated else 'captured')
                        or set(event['values']) - set(fields)
                        or any(type(value) not in (type(None), str, int, float, bool)
                               for value in event['values'].values())):
                    raise ValueError('Unclassified workflow capture values')
    node_visits = {event.get('scope_id') for event in events
                   if event.get('event') == 'scope_start' and event.get('kind') == 'node'}
    call_ids = set()
    for event in events:
        if event.get('event') == 'call':
            if event.get('visit_id') not in node_visits or event.get('call_id') in call_ids:
                raise ValueError('Leaf call lacks a unique node visit owner')
            call_ids.add(event['call_id'])
    routes_complete = _routes_complete(payload['projection'], events)
    complete = payload['gap'] is None and routes_complete
    if payload['completeness'] != ('complete' if complete else 'partial'):
        raise ValueError('Workflow completeness mismatch')
    return payload
