"""Simultaneous exact binomial calibration and bounded independent coverage."""
from math import comb

ASSUMPTION = 'Independent graph trials; same model, effective settings and source hashes'
PILOT_MAX_GRAPHS = 60
DEFAULT_COVERAGE_CEILING = 60
MAX_COVERAGE_CEILING = 120


def coverage_ceiling(value=DEFAULT_COVERAGE_CEILING):
    """Admit an explicit operating budget without changing statistical claims."""
    if type(value) is not int or not 1 <= value <= MAX_COVERAGE_CEILING:
        raise ValueError('LLM_WORKFLOW_TEST_MAX_GRAPHS must be an integer from 1 to 120')
    return value


def lower_probability(hits, trials, alpha):
    """One-sided Clopper–Pearson lower bound, inverted without approximations."""
    if type(hits) is not int or type(trials) is not int or not 0 <= hits <= trials or trials < 1 or not 0 < alpha < 1:
        raise ValueError('Invalid binomial observations')
    if hits == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(64):
        middle = (lo + hi) / 2
        tail = sum(comb(trials, k) * middle ** k * (1 - middle) ** (trials - k) for k in range(hits, trials + 1))
        if tail < alpha:
            lo = middle
        else:
            hi = middle
    return lo


def _report(hits, trials, ceiling):
    bounds = {node: lower_probability(count, trials, .05 / len(hits)) for node, count in sorted(hits.items())}
    for runs in range(1, ceiling + 1):
        missing = sum((1 - probability) ** runs for probability in bounds.values())
        if missing <= .001:
            maximum = runs + 4
            if maximum > ceiling:
                raise ValueError('Calibrated minimum plus safety margin exceeds the operating ceiling')
            buffered_missing = sum((1 - probability) ** maximum for probability in bounds.values())
            return dict(trials=trials, hits=hits, bounds=bounds, minimum_runs=runs,
                        operating_ceiling=ceiling,
                        safety_margin=4, minimum_missing_node_bound=missing, max_runs=maximum,
                        missing_node_bound=buffered_missing, confidence=.95, target=.999,
                        assumption=ASSUMPTION)
    raise ValueError('Observed coverage does not justify the target within the operating ceiling')


def calibration(observations, required, *, ceiling=DEFAULT_COVERAGE_CEILING):
    ceiling = coverage_ceiling(ceiling)
    required = frozenset(required)
    if not observations or len(observations) > PILOT_MAX_GRAPHS or not required:
        raise ValueError('Calibration requires up to 60 observations, nodes and a bounded ceiling')
    if any(not set(run) <= required for run in observations):
        raise ValueError('Calibration contains undeclared nodes')
    return _report({node: sum(node in run for run in observations) for node in sorted(required)}, len(observations), ceiling)


FIRST = frozenset({'first_draft', 'first_format', 'first_guard'})
DECLARED = FIRST | frozenset({'second_draft', 'second_format', 'second_guard'})
ROUTES = frozenset({'skip', 'next_a', 'next_b'})
TARGETS = DECLARED | ROUTES | {'internal_retry'}
COVERAGE_POLICY = 'nodes_routes_internal_retry_v2'


PROOF_FIELDS = {
    'scope_start': ('scope_id', 'parent_id', 'owner', 'kind', 'component', 'node', 'attempt', 'max_passes', 'attempt_id'),
    'scope_end': ('scope_id', 'parent_id', 'owner', 'kind', 'component', 'node', 'attempt', 'max_passes', 'attempt_id', 'outcome'),
    'attempt': ('scope_id', 'component', 'attempt', 'max_passes', 'attempt_id'),
    'node': ('scope_id', 'node', 'component', 'attempt'),
    'edge': ('owner', 'source', 'target', 'condition', 'implicit', 'visit_id', 'attempt_id'),
}


def measurement(trial, values, nodes, events):
    """Retain emitted structural proof; no scopes or edges are reconstructed."""
    proof = [dict(event=event['event'], **{key: event[key] for key in PROOF_FIELDS[event['event']] if key in event})
             for event in events if event['event'] in PROOF_FIELDS
             and (event['event'] != 'edge' or event.get('owner') == 'GRAPH')]
    attempts = [{key: event[key] for key in ('scope_id', 'component', 'attempt', 'max_passes')}
                for event in proof if event['event'] == 'attempt']
    return dict(trial=trial, route=values['first_draft']['route'], nodes=sorted(nodes), attempts=attempts, proof=proof)


def structural_targets(row):
    """Check successful lifecycles and bind attempts, nodes and root edges."""
    proof = row['proof']
    if type(proof) is not list or not proof:
        raise ValueError('Missing executed structural proof')
    scopes, active, attempts, nodes, edges, attempt_ids = {}, [], {}, set(), [], set()
    graph_id = None
    node_hits = {}
    for event in proof:
        if type(event) is not dict or event.get('event') not in PROOF_FIELDS:
            raise ValueError('Invalid executed structural event')
        kind = event['event']
        if set(event) - {'event', *PROOF_FIELDS[kind]}:
            raise ValueError('Unexpected executed structural fields')
        if kind == 'scope_start':
            scope = event.get('scope_id')
            if type(scope) is not str or not scope or scope in scopes:
                raise ValueError('Duplicate or invalid executed scope')
            owner, category = event.get('owner'), event.get('kind')
            if category == 'graph':
                if graph_id is not None or active or owner != 'GRAPH':
                    raise ValueError('Invalid executed graph scope')
                graph_id = scope
            elif (not active or event.get('parent_id') != active[-1]
                  or (category == 'component' and (owner not in {'first', 'second'} or active[-1] != graph_id))
                  or (category == 'node' and (owner not in DECLARED or scopes[active[-1]].get('kind') != 'component'))
                  or category not in {'component', 'node'}):
                raise ValueError('Executed scope ownership disagrees')
            if category == 'component' and event.get('component') != owner:
                raise ValueError('Executed component scope metadata disagrees with owner')
            scopes[scope] = event
            active.append(scope)
        elif kind == 'scope_end':
            scope = event.get('scope_id')
            start = scopes.get(scope)
            if (not active or active[-1] != scope or start is None or event.get('outcome') != 'passed'
                    or {key: value for key, value in event.items() if key not in {'event', 'outcome'}}
                    != {key: value for key, value in start.items() if key != 'event'}):
                raise ValueError('Executed scope did not complete successfully')
            active.pop()
        elif kind == 'attempt':
            scope = event.get('scope_id')
            start = scopes.get(scope, {})
            identity = event.get('attempt_id')
            previous = attempts.get(scope, [])
            if (not active or active[-1] != scope or start.get('kind') != 'component'
                    or event.get('component') != start.get('owner')
                    or event.get('max_passes') != start.get('max_passes')
                    or type(event.get('attempt')) is not int or event['attempt'] != len(previous) + 1
                    or type(identity) is not str or not identity or identity in attempt_ids):
                raise ValueError('Attempt is not bound to its executed component scope')
            attempt_ids.add(identity)
            attempts.setdefault(scope, []).append(event)
        elif kind == 'node':
            scope = event.get('scope_id')
            start = scopes.get(scope, {})
            parent = start.get('parent_id')
            current = attempts.get(parent, [])
            if (not active or active[-1] != scope or start.get('kind') != 'node'
                    or event.get('node') != start.get('owner') or event.get('component') != scopes.get(parent, {}).get('owner')
                    or not current or event.get('attempt') != current[-1]['attempt']
                    or start.get('attempt_id') != current[-1]['attempt_id']
                    or start.get('node') != event.get('node') or start.get('component') != event.get('component')
                    or start.get('attempt') != event.get('attempt')
                    or not event['node'].startswith(event['component'] + '_')):
                raise ValueError('Successful node is not bound to its executed attempt')
            if type(start.get('max_passes')) is not int or start['max_passes'] != current[-1]['max_passes']:
                raise ValueError('Executed node pass limit disagrees with its component attempt')
            key = parent, event['attempt']
            if event['node'] in node_hits.get(key, set()):
                raise ValueError('Duplicate successful node within executed attempt')
            node_hits.setdefault(key, set()).add(event['node'])
            nodes.add(event['node'])
        else:
            if active != [graph_id] or event.get('owner') != 'GRAPH' or event.get('attempt_id') is not None:
                raise ValueError('Root edge is not bound to its executed graph')
            source = event.get('source')
            if source != 'START' and (event.get('visit_id') not in scopes or scopes[event['visit_id']].get('owner') != source):
                raise ValueError('Root edge source disagrees with executed component')
            edges.append((source, event.get('target'), event.get('condition'), event.get('implicit')))
    expected_edges = [('START', 'first', None, True), ('first', 'END', 'first_draft.route == "skip"', False)] if row['route'] == 'skip' else [
        ('START', 'first', None, True), ('first', 'second', 'otherwise', False), ('second', 'END', 'always', True)]
    if graph_id is None or active or edges != expected_edges:
        raise ValueError('Executed root edges disagree with route or graph completion')
    components = [event['owner'] for event in scopes.values() if event['kind'] == 'component']
    if sorted(components) != (['first'] if row['route'] == 'skip' else ['first', 'second']):
        raise ValueError('Executed components disagree with route')
    for scope, visits in attempts.items():
        expected = {scopes[scope]['owner'] + '_' + suffix for suffix in ('draft', 'format', 'guard')}
        if any(node_hits.get((scope, visit['attempt'])) != expected for visit in visits):
            raise ValueError('Executed attempt lacks its successful node visits')
    if nodes != set(row['nodes']):
        raise ValueError('Executed successful nodes disagree with measurement')
    actual_attempts = [{key: event[key] for key in ('scope_id', 'component', 'attempt', 'max_passes')}
                       for event in proof if event['event'] == 'attempt']
    if actual_attempts != row['attempts']:
        raise ValueError('Observed attempts disagree with executed proof')
    return graph_id, set(scopes)


def observed_targets(row, *, maximum=PILOT_MAX_GRAPHS):
    """Legacy pilot admission retains its statistical operating ceiling."""
    return _executed_targets(row, maximum=coverage_ceiling(maximum))


def _executed_targets(row, *, maximum):
    """Validate unchanged emitted proof under an explicit ordinal bound."""
    if type(row) is not dict or set(row) != {'trial', 'route', 'nodes', 'attempts', 'proof'}:
        raise ValueError('Invalid pilot measurement schema')
    if type(row['trial']) is not int or not 1 <= row['trial'] <= maximum or type(row['route']) is not str or row['route'] not in ROUTES:
        raise ValueError('Invalid pilot trial identity or route')
    expected = FIRST if row['route'] == 'skip' else DECLARED
    if type(row['nodes']) is not list or row['nodes'] != sorted(expected):
        raise ValueError('Pilot route disagrees with successful declared nodes')
    if type(row['attempts']) is not list:
        raise ValueError('Invalid observed component attempts')
    scopes = {}
    retry = False
    for item in row['attempts']:
        if type(item) is not dict or set(item) != {'scope_id', 'component', 'attempt', 'max_passes'}:
            raise ValueError('Invalid observed component attempt')
        scope, component, attempt, maximum = (item[key] for key in ('scope_id', 'component', 'attempt', 'max_passes'))
        if (type(scope) is not str or not scope or type(component) is not str or component not in {'first', 'second'}
                or (component == 'second' and row['route'] == 'skip')
                or type(attempt) is not int or type(maximum) is not int or not 1 <= attempt <= maximum):
            raise ValueError('Invalid observed component attempt')
        previous = scopes.get(scope, (component, maximum, 0))
        if previous != (component, maximum, attempt - 1):
            raise ValueError('Component attempts must be contiguous within their observed scope')
        scopes[scope] = (component, maximum, attempt)
        retry |= attempt > 1
    if {value[0] for value in scopes.values()} != ({'first'} if row['route'] == 'skip' else {'first', 'second'}):
        raise ValueError('Missing observed component attempts')
    structural_targets(row)
    return set(row['nodes']) | {row['route']} | ({'internal_retry'} if retry else set())


def measured_calibration(records, required, *, ceiling=DEFAULT_COVERAGE_CEILING):
    """Derive all statistics from contiguous successful per-trial measurements."""
    if set(required) != DECLARED or type(records) is not list or not 1 <= len(records) <= PILOT_MAX_GRAPHS:
        raise ValueError('Invalid pilot measurements or fixture declaration')
    observations, routes = [], {route: 0 for route in sorted(ROUTES)}
    identities = set()
    for index, row in enumerate(records, 1):
        targets = observed_targets(row)
        _, scopes = structural_targets(row)
        if identities & scopes:
            raise ValueError('Independent trials reuse executed scope identities')
        identities.update(scopes)
        if row['trial'] != index:
            raise ValueError('Invalid pilot trial identity or route')
        routes[row['route']] += 1
        observations.append(targets)
    result = calibration(observations, TARGETS, ceiling=ceiling)
    return dict(result, coverage_policy=COVERAGE_POLICY, records=records, routes=routes)


def validate_calibration(report, required, fingerprint, *, ceiling=DEFAULT_COVERAGE_CEILING):
    """Recompute claims under the independently selected operating ceiling."""
    ceiling = coverage_ceiling(ceiling)
    if type(report) is not dict or any(report.get(key) != value for key, value in fingerprint.items()):
        # Emit fixed category names only: neither saved evidence nor configured
        # values belong in a prerequisite diagnostic.
        categories = ('fingerprint_version', 'sampling_method', 'coverage_policy', 'operating_ceiling', 'model', 'dependency_versions',
                      'python_version', 'backend', 'provider_identity', 'user_input_hash',
                      'effective_settings', 'sources')
        changed = [name for name in categories if type(report) is dict and name in fingerprint
                   and report.get(name) != fingerprint[name]]
        detail = ', '.join(changed) or 'report structure or additional fingerprint fields'
        raise ValueError('Calibration is stale for model, effective settings or sources. '
                         f'Changed categories: {detail}. Refresh calibration explicitly; '
                         'see docs/workflow_calibration.md. The maintenance pilot makes real LLM calls.')
    expected = measured_calibration(report.get('records'), required, ceiling=ceiling)
    # Historical reports predate this explicit field and used only cap 60.
    # Normal preflight supplies a current fingerprint containing the ceiling.
    if 'operating_ceiling' not in report and ceiling == DEFAULT_COVERAGE_CEILING:
        expected.pop('operating_ceiling')
    if any(type(report.get(key)) is not type(value) or report.get(key) != value for key, value in expected.items()):
        raise ValueError('Calibration claims do not match exact recomputed bounds')
    return expected['max_runs']


async def cover(run_graph, required, maximum, observe=None, *, ceiling=DEFAULT_COVERAGE_CEILING):
    ceiling = coverage_ceiling(ceiling)
    required, seen = frozenset(required), set()
    if not required or type(maximum) is not int or not 1 <= maximum <= ceiling:
        raise ValueError('Coverage requires declared nodes and a run cap within the selected operating ceiling')
    for trial in range(1, maximum + 1):
        result, nodes = await run_graph(trial)
        if not set(nodes) <= required:
            raise ValueError('Coverage contains undeclared nodes')
        seen.update(nodes)
        if observe is not None:
            observe(dict(trial=trial, seen=sorted(seen), complete=required <= seen))
        if required <= seen:
            summary = dict(runs=trial, nodes=sorted(seen & DECLARED if required == TARGETS else seen),
                           stopping_reason='all_targets_hit' if required == TARGETS else 'all_nodes_hit')
            if required == TARGETS:
                summary['targets'] = sorted(seen)
            return summary, result
    raise AssertionError('Incomplete coverage at calibrated cap; missing targets: ' + ', '.join(sorted(required - seen)))


# Normal coverage is finite control-flow coverage, not a statistical pilot.
DETERMINISTIC_CASES = frozenset(
    [('skip', ready, None) for ready in (False, True)] +
    [(route, first, second) for route in ('next_a', 'next_b')
     for first in (False, True) for second in (False, True)])


def coverage_case(trial_uuid):
    """Decode an unmodified fresh draw using the native fixture's decisions."""
    from .contrived import trial_decisions
    from .run_context import canonical_uuid
    canonical_uuid(trial_uuid)
    first = trial_decisions(trial_uuid, 'first')
    second = None if first['route'] == 'skip' else trial_decisions(trial_uuid, 'second')['ready']
    return first['route'], first['ready'], second


def case_targets(row, case):
    """Require actual structural proof before accepting one selected case."""
    targets = _executed_targets(row, maximum=len(DETERMINISTIC_CASES))
    if row['route'] != case[0]:
        raise AssertionError('Executed route differs from the selected UUID case')
    for component, ready in zip(('first', 'second'), case[1:]):
        if ready is False and not any(item['component'] == component and item['attempt'] > 1
                                      for item in row['attempts']):
            raise AssertionError('Selected not-ready component did not execute its correction')
    return targets
