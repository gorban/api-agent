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
        parameters = [dict(name='scope_id', value=identifier)]
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
        elif parent is None:
            roots.append(rows[identifier])
        else:
            raise ValueError('Workflow scope parent is absent')
    if roots and (len(roots) != 1 or roots[0]['name'] != 'graph GRAPH'):
        raise ValueError('Workflow scopes require exactly one graph root')
    calls = set()
    inclusive = {identifier: [] for identifier in scopes}
    for event in events:
        if event.get('event') != 'call':
            continue
        call_id, visit_id = event.get('call_id'), event.get('visit_id')
        if (not _identifier(call_id) or call_id in calls or visit_id not in rows
                or scopes[visit_id].get('kind') != 'node'):
            raise ValueError('Workflow leaf call lacks a unique node visit')
        calls.add(call_id)
        usage = event.get('usage') or {}
        if type(usage) is not dict:
            raise ValueError('Invalid workflow leaf usage')
        cursor = visit_id
        while cursor in scopes:
            inclusive[cursor].append(usage)
            cursor = scopes[cursor]['parent_id']
        parameters = [dict(name=name, value=str(value)) for name, value in usage.items()
                      if name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens',
                                  'reasoning_tokens', 'total_tokens', 'estimated_cost_usd',
                                  'charged_cost_usd', 'basis') and value is not None]
        if event.get('selection') is not None:
            parameters.append(dict(name='selection', value=str(event['selection'])))
        parent = rows[visit_id]
        start, stop = event.get('started_ms'), event.get('stopped_ms')
        if _time(start) and _time(stop) and parent['start'] <= start <= stop <= parent['stop']:
            parent['steps'].append(dict(name='call ' + call_id, status='passed' if event.get('outcome') == 'completed' else 'broken',
                                        start=int(start), stop=int(stop), steps=[], parameters=parameters))
        else:
            parent['parameters'].extend(dict(name='call ' + call_id + ' ' + item['name'], value=item['value'])
                                        for item in parameters)
    for identifier, usages in inclusive.items():
        if not usages:
            continue
        for name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens',
                     'reasoning_tokens', 'total_tokens', 'estimated_cost_usd',
                     'charged_cost_usd'):
            values = [usage.get(name) for usage in usages]
            amount = math.fsum(values) if all(type(value) in (int, float) and math.isfinite(value)
                                             and value >= 0 for value in values) else None
            rows[identifier]['parameters'].append(dict(name='inclusive_' + name,
                                                       value=str(amount) if amount is not None else 'unknown'))
    return roots
