"""Simultaneous exact binomial calibration and bounded independent coverage."""
from math import comb

ASSUMPTION = 'Independent graph trials; same model, effective settings and source hashes'


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
                        safety_margin=4, minimum_missing_node_bound=missing, max_runs=maximum,
                        missing_node_bound=buffered_missing, confidence=.95, target=.999,
                        assumption=ASSUMPTION)
    raise ValueError('Observed coverage does not justify the target within the operating ceiling')


def calibration(observations, required, *, ceiling=60):
    required = frozenset(required)
    if not observations or len(observations) > 60 or not required or type(ceiling) is not int or not 1 <= ceiling <= 60:
        raise ValueError('Calibration requires up to 60 observations, nodes and a bounded ceiling')
    if any(not set(run) <= required for run in observations):
        raise ValueError('Calibration contains undeclared nodes')
    return _report({node: sum(node in run for run in observations) for node in sorted(required)}, len(observations), ceiling)


FIRST = frozenset({'first_draft', 'first_format', 'first_guard'})
DECLARED = FIRST | frozenset({'second_draft', 'second_format', 'second_guard'})


def measured_calibration(records, required, *, ceiling=60):
    """Derive all statistics from contiguous successful per-trial measurements."""
    if set(required) != DECLARED or type(records) is not list or not 1 <= len(records) <= 60:
        raise ValueError('Invalid pilot measurements or fixture declaration')
    observations, routes = [], {'skip': 0, 'next_a': 0, 'next_b': 0}
    for index, row in enumerate(records, 1):
        if type(row) is not dict or set(row) != {'trial', 'route', 'nodes'} or type(row['trial']) is not int or row['trial'] != index or type(row['route']) is not str or row['route'] not in routes:
            raise ValueError('Invalid pilot trial identity or route')
        nodes = row['nodes']
        expected = FIRST if row['route'] == 'skip' else DECLARED
        if type(nodes) is not list or any(type(node) is not str for node in nodes) or nodes != sorted(expected):
            raise ValueError('Pilot route disagrees with successful declared nodes')
        routes[row['route']] += 1
        observations.append(set(nodes))
    result = calibration(observations, required, ceiling=ceiling)
    return dict(result, records=records, routes=routes)


def validate_calibration(report, required, fingerprint):
    """Recompute every claim before a coverage run can consume a saved pilot."""
    if type(report) is not dict or any(report.get(key) != value for key, value in fingerprint.items()):
        raise ValueError('Calibration is stale for model, effective settings or sources')
    expected = measured_calibration(report.get('records'), required)
    if any(type(report.get(key)) is not type(value) or report.get(key) != value for key, value in expected.items()):
        raise ValueError('Calibration claims do not match exact recomputed bounds')
    return expected['max_runs']


async def cover(run_graph, required, maximum, observe=None):
    required, seen = frozenset(required), set()
    if not required or type(maximum) is not int or not 1 <= maximum <= 60:
        raise ValueError('Coverage requires declared nodes and a run cap up to 60')
    for trial in range(1, maximum + 1):
        result, nodes = await run_graph(trial)
        if not set(nodes) <= required:
            raise ValueError('Coverage contains undeclared nodes')
        seen.update(nodes)
        if observe is not None:
            observe(dict(trial=trial, seen=sorted(seen), complete=required <= seen))
        if required <= seen:
            return dict(runs=trial, nodes=sorted(seen), stopping_reason='all_nodes_hit'), result
    raise AssertionError('Incomplete node coverage at calibrated cap')
