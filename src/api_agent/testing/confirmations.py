"""Deterministic E2E confirmation policy, independent of execution transport."""


def confirmation_count(value):
    """Explicit emptiness disables confirmations; configuration supplies defaults."""
    if value is None or value == '':
        return 0
    if type(value) is not int or value < 0:
        raise ValueError('E2E_RERUNS must be a nonnegative integer or empty')
    return value


def confirmation_outcome(statuses, allowance):
    """Validate an executed prefix and return final status and continuation duty."""
    count = confirmation_count(allowance)
    statuses = tuple(statuses)
    if not statuses or len(statuses) > count + 1:
        raise ValueError('Invalid confirmation attempt count')
    first = statuses[0]
    if first == 'passed':
        if len(statuses) != 1:
            raise ValueError('An initial pass cannot have confirmations')
        return 'passed', False
    # Setup/execution errors are initial failures too. Skips and interruption
    # never start confirmations; suite infrastructure failures stop execution.
    if first not in {'failed', 'error', 'broken'} or count == 0:
        if len(statuses) != 1:
            raise ValueError('This initial outcome cannot have confirmations')
        return first, False
    for index, status in enumerate(statuses[1:], 1):
        if status != 'passed':
            if index != len(statuses) - 1:
                raise ValueError('Confirmation continued after an unsuccessful attempt')
            return status if status in {'cancelled', 'interrupted'} else 'failed', False
    if len(statuses) == count + 1:
        return 'passed', False
    return 'failed', True


def logical_usage(report, owners):
    """Project physical usage for the logical-test UI; artifacts stay physical."""
    from copy import deepcopy
    from math import fsum
    result = deepcopy(report)
    for name in ('usage_rows', 'usage_observations'):
        for row in result.get(name, ()):
            row['session_id'] = owners.get(row['session_id'], row['session_id'])
    for name in ('unmeasured_calls', 'measured_calls', 'unmeasured_model_calls',
                 'unmeasured_native_invocations', 'unobserved_calls'):
        counts = {}
        for key, count in result.get(name, {}).items():
            owner = owners.get(key, key)
            counts[owner] = counts.get(owner, 0) + count
        if name in result:
            result[name] = counts
    groups = {}
    for key, row in result.get('test_costs', {}).items():
        groups.setdefault(owners.get(key, key), []).append(row)
    costs = {}
    def total(rows, key):
        values = [row.get(key) for row in rows]
        return fsum(values) if all(type(value) in (int, float) for value in values) else None
    for key, rows in groups.items():
        if len(rows) == 1:
            costs[key] = rows[0]
            continue
        row = dict(rows[0])
        for field in ('estimated_cost_usd', 'raw_quota_cost_usd', 'list_cost_usd'):
            row[field] = total(rows, field)
        row['known_zero'] = (row['estimated_cost_usd'] == 0
                             and all(item.get('known_zero') is True for item in rows))
        row['incomplete'] = (row['estimated_cost_usd'] is None
                             or any(item.get('incomplete', False) for item in rows))
        bases = {item.get('basis') or 'unavailable' for item in rows if not item.get('known_zero')}
        row['basis'] = ('unavailable' if row['estimated_cost_usd'] is None or 'unavailable' in bases else
                        next(iter(bases)) if len(bases) == 1 else 'mixed' if bases else 'list_price')
        row['prompt_count'] = sum(item.get('prompt_count', 0) for item in rows)
        row['cost_per_prompt_usd'] = (None if row['estimated_cost_usd'] is None or not row['prompt_count']
                                     else row['estimated_cost_usd'] / row['prompt_count'])
        row['category_costs_usd'] = {field: total([item.get('category_costs_usd', {}) for item in rows], field)
            for field in {field for item in rows for field in item.get('category_costs_usd', {})}}
        row['category_cost_assumptions'] = sorted({value for item in rows
            for value in item.get('category_cost_assumptions', ())})
        # An individual-attempt historical average is not a logical-run average.
        row['historical_average'] = None
        row['advance_prediction'] = None
        costs[key] = row
    result['test_costs'] = costs
    return result
