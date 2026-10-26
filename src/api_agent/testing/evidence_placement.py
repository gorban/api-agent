"""Place stored evidence using observed identities within one recorded test."""
from copy import deepcopy


def legacy_owner(packet):
    """Classify structural fixture evidence without inferring execution owners."""
    if 'events' in packet:
        roots = {event.get('scope_id') for event in packet['events']
                 if type(event) is dict and event.get('event') == 'scope_start'
                 and event.get('kind') == 'graph' and event.get('parent_id') is None}
        return ({'kind': 'legacy_graph', 'scope_id': next(iter(roots))}
                if len(roots) == 1 else {'kind': 'unattached'}), 'Execution diagnostics'
    if 'trial' in packet and 'seen' in packet and 'complete' in packet:
        return {'kind': 'trial', 'trial_index': packet['trial']}, 'Coverage progress'
    if 'coverage' in packet:
        return {'kind': 'test'}, 'Coverage summary'
    return {'kind': 'test'}, 'Workflow execution evidence'


def observed_call_scopes(steps):
    """Freeze untimed call claims before accounting replaces draft parameters."""
    claims = {}

    def visit(step, invocation=None):
        parameters = {row['name']: row['value'] for row in step.get('parameters', [])}
        invocation = parameters.get('invocation_id', invocation)
        scope = parameters.get('scope_id')
        for name in parameters:
            if scope is not None and name.startswith('call ') and name.endswith(' outcome'):
                claims.setdefault(name[5:-8], []).append((invocation, scope))
        for child in step.get('steps', []):
            visit(child, invocation)

    for step in steps:
        visit(step)
    return claims


def place_evidence(steps, queued, *, call_scopes=None):
    """Return test attachments; attach unique observed owners in place.

    Queued rows contain an owner and an existing file reference. Reapplying is
    safe after accounting creates call leaves or execution grouping is rebuilt.
    No graph, trial or call is invented to receive evidence.
    """
    sources = {reference['source'] for _, reference in queued}
    candidates = []

    def visit(step, invocation=None):
        parameters = {row['name']: row['value'] for row in step.get('parameters', [])}
        invocation = parameters.get('invocation_id', invocation)
        if 'attachments' in step:
            step['attachments'] = [row for row in step['attachments'] if row['source'] not in sources]
        candidates.append((step, parameters, invocation))
        for child in step.get('steps', []):
            visit(child, invocation)

    for step in steps:
        visit(step)
    remaining = []
    for owner, reference in queued:
        kind = owner['kind']
        matches = []
        for step, parameters, invocation in candidates:
            if kind == 'graph':
                matches_owner = (step['name'].startswith('graph ') and invocation == owner['invocation_id'])
            elif kind == 'scope':
                matches_owner = (invocation == owner['invocation_id'] and parameters.get('scope_id') == owner['scope_id'])
            elif kind == 'legacy_graph':
                matches_owner = (step['name'].startswith('graph ') and parameters.get('scope_id') == owner['scope_id'])
            elif kind == 'call':
                claims = (call_scopes or {}).get(owner['call_id'], ())
                matches_owner = (step['name'] == 'call ' + owner['call_id'] or
                    (invocation, parameters.get('scope_id')) in claims)
            elif kind == 'trial':
                matches_owner = (step['name'].startswith('Trial ') and parameters.get('trial_index') == str(owner['trial_index']))
            else:
                matches_owner = False
            if matches_owner:
                matches.append(step)
        row = deepcopy(reference)
        if len(matches) == 1:
            matches[0].setdefault('attachments', []).append(row)
        else:
            if kind != 'test':
                row['name'] = 'Unattached ' + row['name']
            remaining.append(row)
    return remaining
