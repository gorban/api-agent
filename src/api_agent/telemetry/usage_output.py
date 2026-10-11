"""Present quota snapshots and independently supplied provider cost estimates."""
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from .estimation import quota_reset_observed
from .usage import QuotaReadError

QUOTA_RESET_NOTE = 'Quota reset/change detected: quota cost unavailable; this run is excluded from cost history.'


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def percentage(value):
    return f'{value:.0f}%' if number(value) else 'unavailable'


def money(value):
    if number(value) and 0 < value < 1e-9:
        return f'${value:.3g}'
    if not number(value):
        return 'unavailable'
    return _decimal_money(Decimal(f'{value:.9f}'))


def _decimal_money(value):
    if 0 < value < Decimal('1e-9'):
        return '$' + format(value.normalize(), 'g')
    whole, fraction = format(value, 'f').partition('.')[::2]
    fraction = fraction.rstrip('0')
    if value >= Decimal('.001') or value == 0:
        fraction = fraction.ljust(3, '0')
    return '$' + whole + ('.' + fraction if fraction else '')


def starting_usage_line(report):
    return quota_line(report, 'before', 'Starting')


def quota_line(report, phase, label):
    value = report.get('pct_used_' + phase)
    if number(value):
        return f'{label} quota used: {percentage(value)}'
    error = report.get('quota_errors', {}).get(phase)
    if error is not None:
        try:
            message = QuotaReadError(**error).message
        except (TypeError, ValueError):
            # An evidence boundary may have redacted a coincidentally matching code.
            message = QuotaReadError('request').message
        return f"{label} quota used: Error: {message}"
    return ''


def quota_observed(report):
    return (number(report.get('pct_used_before')) or number(report.get('pct_used_after'))
            or number(report.get('pct_point_delta')) or bool(report.get('quota_errors')))



def historical_average(costs):
    """Prefer post-run history; retain legacy advance-only reports as recorded."""
    amounts = [(cost.get('historical_average', cost.get('advance_prediction')) or {}).get('estimated_cost_usd') for cost in costs]
    return math.fsum(amounts) if amounts and all(number(value) for value in amounts) else None


def displayed_cost(value, report):
    """Explain an unpriced unchanged quota without discarding known estimates."""
    delta = report.get('pct_point_delta')
    if (not number(value) and number(delta) and delta == 0
            and not report.get('quota_unavailable_reason') and not quota_reset_observed(report)):
        return 'quota did not change'
    return money(value)


TOKEN_LABELS = (('input_tokens', 'input'), ('output_tokens', 'output'),
                ('cache_read_tokens', 'cached input'), ('cache_creation_tokens', 'cache write'),
                ('reasoning_tokens', 'reasoning'), ('total_tokens', 'total (input + output)'))


def has_tokens(report, test_id):
    return any(type(row.get(field)) is int and row[field] > 0
               for row in report.get('usage_observations', report.get('usage_rows', ())) if row.get('session_id') == test_id
               for field, _ in TOKEN_LABELS)


def _partition_row(row):
    """Derive display counts without changing inclusive CSV observations."""
    result = dict(row)
    for parent, subsets in (('input_tokens', ('cache_read_tokens', 'cache_creation_tokens')),
                            ('output_tokens', ('reasoning_tokens',))):
        value = row.get(parent)
        cached = row.get('cache_read_tokens')
        if type(value) is not int or value < 0 or (parent == 'input_tokens' and type(cached) is not int):
            result[parent] = None
            continue
        result[parent] = value - sum(row[key] for key in subsets if type(row.get(key)) is int and row[key] >= 0)
        if result[parent] < 0:
            result[parent] = None
    return result


def _exclusive_costs(scope, rows):
    """Older persisted category amounts included their subset charges."""
    costs = dict(scope.get('category_costs_usd', {}))
    version = scope.get('category_costs_version')
    if version == 2:
        return costs
    if version not in (None, 1):
        return {}
    for parent, subsets in (('input_tokens', ('cache_read_tokens', 'cache_creation_tokens')),
                            ('output_tokens', ('reasoning_tokens',))):
        if not number(costs.get(parent)):
            continue
        amounts = []
        for field in subsets:
            amount = costs.get(field)
            if number(amount):
                amounts.append(amount)
            elif any(type(row.get(field)) is int and row[field] > 0 for row in rows):
                costs[parent] = None
                break
        else:
            costs[parent] = max(0., costs[parent] - math.fsum(amounts))
    return costs


def _rounded_costs(costs, total):
    """Largest-remainder rounding makes displayed contributions sum to total."""
    fields = [field for field, _ in TOKEN_LABELS if field != 'total_tokens']
    known = {field: costs[field] for field in fields if number(costs.get(field))}
    if not number(total):
        return {field: money(value) for field, value in known.items()}
    combined = math.fsum(known.values())
    close = math.isclose(combined, total, rel_tol=1e-10, abs_tol=0)
    if combined > total and not close:
        known = {}
        combined = 0.
    if combined < total and not close:
        known['unallocated'] = total - combined
    displayed = {'total_tokens': money(total)}
    positive = {field: Decimal(str(value)) for field, value in known.items() if value > 0}
    if not positive:
        displayed.update({field: money(0) for field in known})
        return displayed
    precision = max(9, 2 - min(positive.values()).adjusted())
    with localcontext() as context:
        context.prec = max(28, precision + 32)
        quantum = Decimal(1).scaleb(-precision)
        target = int((Decimal(money(total)[1:]) / quantum).to_integral_value(rounding=ROUND_HALF_EVEN))
        denominator = sum(positive.values())
        exact = {field: target * value / denominator for field, value in positive.items()}
        units = {field: int(value.to_integral_value(rounding=ROUND_FLOOR)) for field, value in exact.items()}
        ranked = sorted(exact, key=lambda field: exact[field] - units[field], reverse=True)
        for field in ranked[:target - sum(units.values())]:
            units[field] += 1
        displayed.update({field: _decimal_money(Decimal(units.get(field, 0)) * quantum) for field in known})
    return displayed


def tokens_text(report, test_id=None):
    """Display disjoint counts/prices; CSV input/output totals stay inclusive."""
    rows = [row for row in report.get('usage_observations', report.get('usage_rows', ()))
            if test_id is None or row.get('session_id') == test_id]
    unmeasured = report.get('unmeasured_calls', {})
    unknown_calls = sum(unmeasured.values()) if test_id is None else unmeasured.get(test_id, 0)
    scope = report if test_id is None else report.get('test_costs', {}).get(test_id, {})
    costs = _exclusive_costs(scope, rows)
    total = scope.get('estimated_cost_usd')
    total = total if number(total) else costs.get('total_tokens')
    writes_unknown = any(row.get('cache_creation_tokens') is None or 'cache_creation_tokens' in row.get('unknown_fields', ()) for row in rows)
    reasoning_unknown = any(row.get('reasoning_tokens') is None or 'reasoning_tokens' in row.get('unknown_fields', ()) for row in rows)
    labels = {'input_tokens': 'non-cached input (writes unreported)' if writes_unknown else 'non-cached input',
              'output_tokens': 'output (reasoning unreported)' if reasoning_unknown else 'non-reasoning output',
              'total_tokens': 'total'}
    rows = [_partition_row(row) for row in rows]
    # A charge without a positive observed count cannot be displayed on that
    # category; retain it explicitly as unallocated rather than losing money.
    visible_costs = {field: value for field, value in costs.items() if any(
        type(row.get(field)) is int and row[field] > 0 for row in rows)}
    prices = _rounded_costs(visible_costs, total)
    parts = []
    for field, label in TOKEN_LABELS:
        values = [row.get(field) for row in rows]
        known = [value for value in values if type(value) is int and value >= 0]
        unknown = (unknown_calls > 0 or len(known) != len(values)
                   or any(field in row.get('unknown_fields', ()) for row in rows))
        value = (f'{sum(known):,}' + (' + unknown' if unknown else '') if known or not unknown
                 else 'unavailable')
        suffix = f' — {prices[field]}' if sum(known) > 0 and field in prices else ''
        parts.append(f'  {labels.get(field, label)}: {value}{suffix}')
    if 'unallocated' in prices:
        parts.insert(-1, '  unallocated token cost: ' + prices['unallocated'])
    if 'unreported_cache_writes_at_input_rate' in scope.get('category_cost_assumptions', ()):
        parts.append('  Unreported cache writes use ordinary-input weights for this estimated split.')
    return 'Tokens this run:\n' + '\n'.join(parts)


def final_usage_text(report):
    delta = report.get('pct_point_delta')
    count = report.get('prompt_count', 0)
    per_turn = delta / count if number(delta) and type(count) is int and count > 0 else None
    average = historical_average(report.get('test_costs', {}).values())
    average_per_turn = average / count if number(average) and type(count) is int and count > 0 else None
    lines = [line for line in (starting_usage_line(report), quota_line(report, 'after', 'Final')) if line]
    if quota_observed(report):
        lines.append(f'Quota change: {percentage(delta)}')
    lines.append(f'Full user turns: {count}')
    if quota_observed(report):
        lines.append(f"Quota points per turn: {f'{per_turn:.3f}' if per_turn is not None else 'unavailable'}")
    lines.extend((f"This suite cost: {displayed_cost(report.get('estimated_cost_usd'), report)}",
                  f"  Historical average: {displayed_cost(average, report)}",
                  f"Cost per turn: {displayed_cost(report.get('cost_per_prompt_usd'), report)}",
                  f"  Historical average: {displayed_cost(average_per_turn, report)}"))
    if quota_observed(report):
        lines.append(f"Quota reset countdown (HH:MM:SS): {report.get('reset_countdown_hms') or 'unavailable'}")
        if report.get('quota_unavailable_reason'):
            lines.append(f"Quota unavailable: {report['quota_unavailable_reason']}")
    if quota_reset_observed(report):
        lines.append(QUOTA_RESET_NOTE)
    if report.get('cost_basis') and report['cost_basis'] != 'unavailable':
        lines.append(f"Cost basis: {report['cost_basis']}")
    lines.append(tokens_text(report))
    return '\n'.join(lines)
