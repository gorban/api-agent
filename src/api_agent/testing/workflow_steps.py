"""Translate explicit workflow outcomes and observed attention into Allure steps."""
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


def attention_status(*statuses):
    """Native attention is monotonic: terminal failure outranks retry/cancellation."""
    return max(statuses, key={"passed": 0, "broken": 1, "failed": 2}.__getitem__)


def _capture_status(step, direction, reason):
    details = {
        'captured': 'Payload captured.',
        'partial': 'Only part of the payload was admitted.',
        'truncated': 'Payload exceeded a capture limit.',
        'withheld': 'Payload withheld by the destination content policy.',
        'unavailable': 'No admitted payload was recorded; it or its capture authority was unavailable.',
        'not_observed': 'No capture event was recorded; execution may not have reached or produced this phase.',
        'ambiguous': 'Multiple direct payload captures were recorded for this phase.',
        'privacy_withheld': 'Payload withheld by credential scrubbing.',
        'unclassified': 'No eligible plain I/O fields were declared.',
        'unsupported': 'Payload does not match the supported capture contract.',
        'payload_limit': 'Payload exceeded a capture limit.',
        'unknown': 'Capture status was absent or unrecognized.',
    }
    if type(reason) is not str or reason not in details:
        reason = 'unknown'
    name = direction + '_capture'
    step['parameters'][:] = [item for item in step['parameters']
                            if item['name'] not in (name, name + '_detail')]
    step['parameters'].extend((dict(name=name, value=reason),
        dict(name=name + '_detail', value=details[reason])))


def transport_attempt_steps(ledger):
    """Project admitted physical evidence without creating logical call rows."""
    if ledger is None:
        return []
    from api_agent.providers.generation_attempts import validate_transport_attempts
    ledger = validate_transport_attempts(ledger)
    steps = []
    for row in ledger['attempts']:
        recovered = row['retry_delay_seconds'] is not None
        parameters = [dict(name='transport_' + key, value=str(row[key]))
                      for key in ('id', 'index', 'execution', 'retry_delay_seconds')
                      if row[key] is not None]
        for key in ('outcome', 'status', 'error_kind', 'started_ms', 'stopped_ms'):
            if row.get(key) is not None:
                parameters.append(dict(name='transport_' + key, value=str(row[key])))
        steps.append(dict(name='transport attempt ' + str(row['index']),
                          status=('broken' if recovered or row.get('error_kind') == 'cancelled' else
                                  'failed' if row.get('outcome') == 'failed' else
                                  'broken' if row.get('outcome') in ('started', 'observed') else 'passed'),
                          steps=[], parameters=parameters))
    if not ledger['complete']:
        steps.append(dict(name='transport evidence incomplete', status='broken', steps=[], parameters=[]))
    return steps


def _bubble_attention(step):
    parameters = {item['name']: item['value'] for item in step['parameters']}
    if 'scope_id' in parameters and 'outcome' in parameters:
        outcome = parameters['outcome']
        step['status'] = ('failed' if outcome == 'failed' else 'broken'
                          if outcome != 'passed' or parameters.get('retry_observed') == 'true'
                          else 'passed')
    if 'recovered_by_scope_id' in parameters and step['status'] == 'failed':
        step['status'] = 'broken'
    for child in step["steps"]:
        _bubble_attention(child)
        step["status"] = attention_status(step["status"], child["status"])
        if any(item == dict(name='retry_observed', value='true') for item in child['parameters']):
            if not any(item['name'] == 'retry_observed' for item in step['parameters']):
                step['parameters'].append(dict(name='retry_observed', value='true'))


def bubble_attention(step):
    """Propagate attention, revalidating recovery after projection enrichment."""
    def walk(item):
        yield item
        for child in item['steps']:
            yield from walk(child)

    items = list(walk(step))
    scopes = {parameter['value']: item for item in items for parameter in item['parameters']
              if parameter['name'] == 'scope_id'}
    while True:
        _bubble_attention(step)
        changed = False
        for item in items:
            replacement = next((parameter['value'] for parameter in item['parameters']
                                if parameter['name'] == 'recovered_by_scope_id'), None)
            if replacement is not None and (replacement not in scopes or scopes[replacement]['status'] == 'failed'):
                item['parameters'][:] = [parameter for parameter in item['parameters']
                                        if parameter['name'] != 'recovered_by_scope_id']
                item['status'] = 'failed'
                changed = True
            elif replacement is not None:
                # Complementary projections may add failed call evidence after
                # the original node recovery was established.
                for descendant in walk(item):
                    if descendant['status'] == 'failed':
                        descendant['status'] = 'broken'
                        if not any(parameter['name'] == 'recovered_by_scope_id'
                                   for parameter in descendant['parameters']):
                            descendant['parameters'].append(dict(name='recovered_by_scope_id', value=replacement))
                            changed = True
        if not changed:
            return


def _recover_scope_attempts(scopes, rows, attempts):
    """Only an observed successful retry in the same controller recovers a visit."""
    def mark(step, replacement):
        if step['status'] == 'failed':
            step['status'] = 'broken'
            step['parameters'].append(dict(name='recovered_by_scope_id', value=replacement))
        for child in step['steps']:
            mark(child, replacement)

    def failed(step):
        return step['status'] == 'failed' or any(failed(child) for child in step['steps'])

    for identifier, scope in reversed(tuple(scopes.items())):
        parent = scopes.get(scope['parent_id'])
        attempt = scope.get('attempt')
        if (scope['kind'] != 'node' or scope['end']['outcome'] != 'failed'
                or parent is None or parent['kind'] != 'component'
                or parent['end']['outcome'] != 'passed'
                or type(attempt) is not int or attempt < 1):
            continue
        controller = parent
        while (controller['kind'] == 'component' and controller.get('max_passes') == 1
               and controller['end']['outcome'] == 'passed'):
            controller = scopes[controller['parent_id']]
        maximum = scope.get('max_passes')
        observed = (min(maximum, attempts.get(controller['scope_id'], 0))
                    if type(maximum) is int and maximum > 1
                    and controller['kind'] == 'component' and controller['end']['outcome'] == 'passed'
                    and controller.get('max_passes') == maximum else 0)
        for successor_id, successor in scopes.items():
            next_attempt = successor.get('attempt')
            if (successor['kind'] == 'node' and successor['parent_id'] == scope['parent_id']
                    and successor['owner'] == scope['owner']
                    and successor['end']['outcome'] == 'passed'
                    and successor.get('max_passes') == maximum
                    and type(next_attempt) is int
                    and attempt < next_attempt <= observed
                    and successor['started_ms'] >= scope['end']['stopped_ms']
                    and not failed(rows[successor_id])):
                mark(rows[identifier], successor_id)
                break


def scope_steps(events):
    """An ended scope needs a real start and explicit outcome, never a heartbeat."""
    from api_agent.workflows.tool_observations import validate_tool_events
    validate_tool_events(events)
    scopes = {}
    attempts = {}
    rejections = {}
    for event in events:
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
                    or event.get('outcome') not in ('passed', 'failed', 'cancelled', 'partial', 'unknown')
                    or not _time(event.get('stopped_ms')) or event['stopped_ms'] < event['started_ms']):
                raise ValueError('Invalid workflow scope completion')
            previous['end'] = event
    rows, roots = {}, []
    for identifier, scope in scopes.items():
        end = scope['end']
        if end is None:
            raise ValueError('Workflow scope has no explicit completion')
        parameters = [dict(name='scope_id', value=identifier),
                      dict(name='outcome', value=end['outcome'])]
        for name in ('attempt', 'max_passes', 'declared_timeout', 'effective_timeout', 'timeout_key'):
            if scope.get(name) is not None:
                parameters.append(dict(name=name, value=str(scope[name])))
        if identifier in attempts:
            parameters.append(dict(name='attempts_reached', value=str(attempts[identifier])))
        if attempts.get(identifier, 0) > 1:
            parameters.append(dict(name='retry_observed', value='true'))
        if identifier in rejections:
            parameters.append(dict(name='guard_rejections', value=str(rejections[identifier])))
        rows[identifier] = dict(name=scope['kind'] + ' ' + scope['owner'],
            status=('failed' if end['outcome'] == 'failed' else 'broken'
                    if end['outcome'] in ('cancelled', 'partial', 'unknown') or attempts.get(identifier, 0) > 1 else 'passed'),
            start=int(scope['started_ms']), stop=int(end['stopped_ms']), steps=[], parameters=parameters)
        if scope['kind'] == 'node':
            for direction in ('input', 'output'):
                _capture_status(rows[identifier], direction, 'not_observed')
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
    tool_steps = {}
    calls = set()
    inclusive = {identifier: [] for identifier in scopes}
    node_captures = set()
    for event in events:
        identifier = event.get('tool_invocation_id')
        if event.get('event') == 'tool_start':
            parent = rows[event['visit_id']]
            leaf = dict(name='Tool ' + event.get('tool_name', 'invocation'),
                status='broken', steps=[], parameters=[dict(name='tool_invocation_id', value=identifier)])
            for field in ('tool_name', 'tool_instance'):
                if field in event:
                    leaf['parameters'].append(dict(name=field, value=event[field]))
            for direction in ('input', 'output'):
                _capture_status(leaf, direction, 'not_observed')
            parent['steps'].append(leaf)
            tool_steps[identifier] = (leaf, event, parent)
        elif event.get('event') == 'tool_end':
            leaf, start, parent = tool_steps[identifier]
            outcome = event['outcome']
            leaf['status'] = ('passed' if outcome == 'success' else
                              'failed' if outcome == 'failure' else 'broken')
            leaf['parameters'].append(dict(name='outcome', value=outcome))
            # The typed lifecycle validator already proved finite ordered times
            # inside the original node interval. Round both projections alike.
            leaf.update(start=int(start['started_ms']), stop=int(event['stopped_ms']))
            parent['status'] = attention_status(parent['status'], leaf['status'])
        elif event.get('event') == 'capture':
            visit = event.get('visit_id')
            direction = event.get('direction')
            reason = event.get('reason')
            if reason in ('captured', 'partial', 'truncated') and 'values' not in event:
                reason = 'unavailable'
            if identifier in tool_steps:
                _capture_status(tool_steps[identifier][0], direction, reason)
            elif (identifier is None and visit in rows and scopes[visit]['kind'] == 'node'
                    and not any(event.get(field) is not None for field in
                                ('cause_id', 'transport_attempt_id', 'capture_source'))
                    and direction in ('input', 'output')):
                key = (visit, direction)
                reason = 'ambiguous' if key in node_captures else reason
                _capture_status(rows[visit], direction, reason)
                node_captures.add(key)
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
        outcome = event.get('outcome')
        if outcome not in ('completed', 'tool_calls', 'failed', 'cancelled', 'length', 'refused'):
            outcome = 'unknown'
        parameters.append(dict(name='outcome', value=str(outcome)))
        status = ('passed' if outcome in ('completed', 'tool_calls') else
                  'failed' if outcome == 'failed' else 'broken')
        physical_steps = transport_attempt_steps(event.get('transport_attempts'))
        if physical_steps:
            status = attention_status(status, *(step['status'] for step in physical_steps))
        parent['status'] = attention_status(parent['status'], status)
        start, stop = event.get('started_ms'), event.get('stopped_ms')
        if _time(start) and _time(stop) and parent['start'] <= start <= stop <= parent['stop']:
            parent['steps'].append(dict(name='call ' + call_id, status=status,
                                        start=int(start), stop=int(stop), steps=physical_steps, parameters=parameters))
        else:
            # Every logical call owns its detail, even when no timing interval
            # was admitted. An untimed leaf must not inflate its node parameters.
            parent['steps'].append(dict(name='call ' + call_id, status=status,
                                        steps=physical_steps, parameters=parameters))
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
    _recover_scope_attempts(scopes, rows, attempts)
    for root in roots:
        bubble_attention(root)
    return roots


def attach_payloads(steps, captures, calls):
    """Place recorded file references on observed owners without inventing steps.

    ``captures`` contains durable journal capture events; ``calls`` maps unique
    call identities to phase attachments. Unowned calls remain root attachments.
    """
    scopes, leaves, untimed, tools = {}, {}, {}, {}

    def visit(step):
        scope = next((item['value'] for item in step.get('parameters', [])
                      if item['name'] == 'scope_id'), None)
        if scope is not None:
            scopes.setdefault(scope, []).append(step)
        tool_id = next((item['value'] for item in step.get('parameters', [])
                        if item.get('name') == 'tool_invocation_id'), None)
        if tool_id is not None:
            if tool_id in tools:
                raise ValueError('Ambiguous tool payload owner')
            tools[tool_id] = step
        if step.get('name', '').startswith('call '):
            identifier = step['name'][5:]
            if identifier in leaves:
                raise ValueError('Ambiguous payload call owner')
            leaves[identifier] = step
        for item in step.get('parameters', []):
            if item['name'].startswith('call ') and item['name'].endswith(' outcome'):
                identifier = item['name'][5:-8]
                if identifier in untimed:
                    raise ValueError('Ambiguous payload call owner')
                untimed[identifier] = step
        for child in step.get('steps', []):
            visit(child)

    for step in steps:
        visit(step)
    for capture, attachment in captures:
        owners = scopes.get(capture.get('visit_id'), [])
        if len(owners) > 1:
            raise ValueError('Ambiguous payload scope owner')
        owner = owners[0] if owners else None
        if owner is None:
            raise ValueError('Payload capture has no observed scope owner')
        if capture.get('cause_id') in leaves:
            call = leaves[capture['cause_id']]
            if call not in owner.get('steps', []):
                raise ValueError('LLM payload lacks its exact node owner')
            owner = call
        if capture.get('tool_invocation_id') is not None:
            tool = tools.get(capture['tool_invocation_id'])
            if tool is None or tool not in owner.get('steps', []):
                raise ValueError('Tool payload lacks its exact node owner')
            owner = tool
        attachments = owner.setdefault('attachments', [])
        if not any((item['source'], item['type']) == (attachment['source'], attachment['type']) for item in attachments):
            attachments.append(attachment)
    remaining = []
    for identifier, phases in calls.items():
        if identifier in leaves and identifier in untimed:
            raise ValueError('Ambiguous payload call owner')
        owner = leaves.get(identifier, untimed.get(identifier))
        for phase, phase_attachments in sorted(phases.items(), key=lambda item: str(item[0])):
            phase_owner = owner
            if isinstance(phase, tuple) and owner is not None:
                matches = [step for step in owner.get('steps', []) if any(
                    item.get('name') == 'transport_id' and item.get('value') == phase[0]
                    for item in step.get('parameters', []))]
                if len(matches) > 1:
                    raise ValueError('Payload transport attempt has no unique call owner')
                phase_owner = matches[0] if matches else None
            for attachment in (phase_attachments if isinstance(phase_attachments, list) else [phase_attachments]):
                if phase_owner is None:
                    remaining.append(dict(attachment, name='Unattached ' + attachment['name']))
                else:
                    attachments = phase_owner.setdefault('attachments', [])
                    matching = [index for index, item in enumerate(attachments)
                                if (item['source'], item['type']) == (attachment['source'], attachment['type'])]
                    if matching:
                        attachments[matching[0]] = attachment
                        for index in reversed(matching[1:]):
                            attachments.pop(index)
                    else:
                        attachments.append(attachment)
    return remaining
