"""Present quota snapshots and independently supplied provider cost estimates."""
import math
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
    return (f'${value:.9f}'.rstrip('0') if 0 < value < .001 else f'${value:.3f}') if number(value) else 'unavailable'


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
    """Sum frozen advance predictions; absent/incomplete history stays unavailable."""
    amounts = [(cost.get('advance_prediction') or {}).get('estimated_cost_usd') for cost in costs]
    return math.fsum(amounts) if amounts and all(number(value) for value in amounts) else None


TOKEN_LABELS = (('input_tokens', 'input'), ('output_tokens', 'output'),
                ('cache_read_tokens', 'cached input'), ('cache_creation_tokens', 'cache write'),
                ('reasoning_tokens', 'reasoning'), ('total_tokens', 'total (input + output)'))


def has_tokens(report, test_id):
    return any(type(row.get(field)) is int and row[field] > 0
               for row in report.get('usage_observations', report.get('usage_rows', ())) if row.get('session_id') == test_id
               for field, _ in TOKEN_LABELS)


def tokens_text(report, test_id=None):
    """Cached/reasoning counts are subsets, never added to total tokens."""
    rows = [row for row in report.get('usage_observations', report.get('usage_rows', ()))
            if test_id is None or row.get('session_id') == test_id]
    unmeasured = report.get('unmeasured_calls', {})
    unknown_calls = sum(unmeasured.values()) if test_id is None else unmeasured.get(test_id, 0)
    costs = (report if test_id is None else report.get('test_costs', {}).get(test_id, {})).get('category_costs_usd', {})
    parts = []
    for field, label in TOKEN_LABELS:
        values = [row.get(field) for row in rows]
        known = [value for value in values if type(value) is int and value >= 0]
        unknown = (unknown_calls > 0 or len(known) != len(values)
                   or any(field in row.get('unknown_fields', ()) for row in rows))
        value = (f'{sum(known):,}' + (' + unknown' if unknown else '') if known or not unknown
                 else 'unavailable')
        parts.append(f'  {label}: {value} — {money(costs.get(field))}')
    return ('Tokens this run:\n' + '\n'.join(parts)
            + '\n  Cached input and cache writes are included in input; reasoning is included in output.')


def final_usage_text(report):
    delta = report.get('pct_point_delta')
    count = report.get('prompt_count', 0)
    per_turn = delta / count if number(delta) and type(count) is int and count > 0 else None
    lines = [line for line in (starting_usage_line(report), quota_line(report, 'after', 'Final')) if line]
    if quota_observed(report):
        lines.append(f'Quota change: {percentage(delta)}')
    lines.append(f'Full user turns: {count}')
    if quota_observed(report):
        lines.append(f"Quota points per turn: {f'{per_turn:.3f}' if per_turn is not None else 'unavailable'}")
    lines.extend((f"This suite cost: {money(report.get('estimated_cost_usd'))}",
                  f"  Historical average: {money(historical_average(report.get('test_costs', {}).values()))}",
                  f"Cost per turn: {money(report.get('cost_per_prompt_usd'))}"))
    if quota_observed(report):
        lines.extend((f"Quota reset countdown: {report.get('reset_countdown_hms') or 'unavailable'}",
                      'Cost attribution assumes exclusive use of this account during the operation; other activity invalidates it.'))
        if report.get('quota_unavailable_reason'):
            lines.append(f"Quota unavailable: {report['quota_unavailable_reason']}")
    if quota_reset_observed(report):
        lines.append(QUOTA_RESET_NOTE)
    if report.get('cost_basis'):
        lines.append(f"Cost basis: {report['cost_basis']}")
    lines.append(tokens_text(report))
    return '\n'.join(lines)
