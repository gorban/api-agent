"""Present quota snapshots and independently supplied provider cost estimates."""
import math


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def percentage(value):
    return f'{value:.3f}%' if number(value) else 'unavailable'


def money(value):
    return (f'${value:.9f}'.rstrip('0') if 0 < value < .001 else f'${value:.3f}') if number(value) else 'unavailable'


def starting_usage_line(report):
    return f"Starting quota used: {percentage(report.get('pct_used_before'))}"


TOKEN_LABELS = (('input_tokens', 'input'), ('output_tokens', 'output'),
                ('cache_read_tokens', 'cached input'), ('cache_creation_tokens', 'cache write'),
                ('reasoning_tokens', 'reasoning'), ('total_tokens', 'total'))


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
    parts = []
    for field, label in TOKEN_LABELS:
        values = [row.get(field) for row in rows]
        known = [value for value in values if type(value) is int and value >= 0]
        unknown = (unknown_calls > 0 or len(known) != len(values)
                   or any(field in row.get('unknown_fields', ()) for row in rows))
        value = (f'{sum(known):,}' + (' + unknown' if unknown else '') if known or not unknown
                 else 'unavailable')
        parts.append(f'{label} {value}')
    return 'Tokens: ' + ' · '.join(parts)


def final_usage_text(report):
    delta = report.get('pct_point_delta')
    count = report.get('prompt_count', 0)
    per_turn = delta / count if number(delta) and type(count) is int and count > 0 else None
    lines = [tokens_text(report), starting_usage_line(report),
             f"Final quota used: {percentage(report.get('pct_used_after'))}",
             f'Quota change: {percentage(delta)}',
             f'Full user turns: {count}',
             f"Quota points per turn: {f'{per_turn:.3f}' if per_turn is not None else 'unavailable'}",
             f"Estimated cost: {money(report.get('estimated_cost_usd'))}",
             f"Estimated cost per turn: {money(report.get('cost_per_prompt_usd'))}",
             f"Quota reset countdown: {report.get('reset_countdown_hms') or 'unavailable'}",
             'Cost attribution assumes exclusive use of this account during the operation; other activity invalidates it.']
    if report.get('cost_basis'):
        lines.append(f"Cost basis: {report['cost_basis']}")
    if report.get('quota_unavailable_reason'):
        lines.append(f"Quota unavailable: {report['quota_unavailable_reason']}")
    return '\n'.join(lines)
