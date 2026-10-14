"""Translate explicit workflow scope outcomes into nested Allure steps."""
import math
import re
from uuid import UUID


def _identifier(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def scope_steps(events):
    """An ended scope needs a real start and explicit outcome, never a heartbeat."""
    scopes = {}
    attempts = {}
    rejections = {}
    for event in events:
        if type(event) is not dict:
            raise ValueError('Workflow event must be an object')
        kind = event.get('event')
        if kind in ('attempt', 'guard') and event.get('scope_id') in scopes:
            scope_id = event['scope_id']
            if kind == 'attempt':
                attempts[scope_id] = max(attempts.get(scope_id, 0), event['attempt'])
            elif event['accepted'] is False:
                rejections[scope_id] = rejections.get(scope_id, 0) + 1
        if kind not in ('scope_start', 'scope_end'):
            continue
        identifier, parent = event.get('scope_id'), event.get('parent_id')
        if (not _identifier(identifier) or (parent is not None and (not _identifier(parent) or parent == identifier))
                or event.get('kind') not in ('graph', 'component', 'node')
                or type(event.get('owner')) is not str
                or re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', event['owner']) is None
                or not _time(event.get('started_ms'))):
            raise ValueError('Invalid workflow scope identity or start')
        if kind == 'scope_start':
            if identifier in scopes:
                raise ValueError('Duplicate workflow scope start')
            scopes[identifier] = dict(event, end=None)
        else:
            previous = scopes.get(identifier)
            if (previous is None or previous['end'] is not None
                    or any(previous.get(key) != event.get(key) for key in ('parent_id', 'owner', 'kind', 'started_ms'))
                    or event.get('outcome') not in ('passed', 'failed', 'cancelled')
                    or not _time(event.get('stopped_ms')) or event['stopped_ms'] < event['started_ms']):
                raise ValueError('Invalid workflow scope completion')
            previous['end'] = event
    rows, roots = {}, []
    for identifier, scope in scopes.items():
        end = scope['end']
        if end is None:
            raise ValueError('Workflow scope has no explicit completion')
        parameters = []
        for name in ('attempt', 'max_passes', 'declared_timeout', 'effective_timeout', 'timeout_key'):
            if scope.get(name) is not None:
                parameters.append(dict(name=name, value=str(scope[name])))
        if identifier in attempts:
            parameters.append(dict(name='attempts_reached', value=str(attempts[identifier])))
        if identifier in rejections:
            parameters.append(dict(name='guard_rejections', value=str(rejections[identifier])))
        rows[identifier] = dict(name=scope['kind'] + ' ' + scope['owner'],
            status='broken' if end['outcome'] == 'cancelled' else end['outcome'],
            start=int(scope['started_ms']), stop=int(end['stopped_ms']), steps=[], parameters=parameters)
    for identifier, scope in scopes.items():
        ancestors = {identifier}
        cursor = scope['parent_id']
        while cursor in scopes:
            if cursor in ancestors:
                raise ValueError('Workflow scope ownership is cyclic')
            ancestors.add(cursor)
            cursor = scopes[cursor]['parent_id']
        parent = scope['parent_id']
        if parent in rows:
            owner = rows[parent]
            row = rows[identifier]
            if row['start'] < owner['start'] or row['stop'] > owner['stop']:
                raise ValueError('Workflow child interval is outside its owner')
            owner['steps'].append(row)
        else:
            roots.append(rows[identifier])
    return roots
