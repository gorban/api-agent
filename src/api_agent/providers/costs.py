"""Bounded provider-owned list quotes and quota payment policies."""
import math
import calendar
from datetime import datetime, timezone
from .contracts import ContractError


def period_seconds(period, *, now=None):
    """Use fixed days/weeks and the actual current UTC calendar month."""
    if period in ('day', 'week'):
        return 86400 if period == 'day' else 604800
    if period not in ('month', 'calendar_month'):
        raise ContractError('Unsupported accounting period.')
    date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return calendar.monthrange(date.year, date.month)[1] * 86400


def valid_amount(value):
    return type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value)


MIN_REPORTED_COST_USD = 0.000001


def validate_billing(value):
    """Optional content-free charge evidence, independent of list prices."""
    import re
    if (type(value) is not dict or set(value) != {'usd', 'request_id', 'generation_id'}
            or (value['usd'] is not None and not valid_amount(value['usd']))
            or any(item is not None and (type(item) is not str or
                   re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', item) is None)
                   for item in (value['request_id'], value['generation_id']))):
        raise ContractError('Invalid reported billing evidence')
    return value


def qualified_charge(billing, usage):
    """Keep sub-display-unit nonzero-token charges as raw evidence only."""
    if billing is None:
        return None
    validate_billing(billing)
    amount = billing['usd']
    if amount is None or (amount < MIN_REPORTED_COST_USD and
                          [usage.get('input_tokens'), usage.get('output_tokens')] != [0, 0]):
        return None
    return amount


def validate_quote(quote):
    if (type(quote) is not dict or set(quote) not in ({'list_cost_usd', 'payment'}, {'list_cost_usd', 'payment', 'accounting'})
            or (quote['list_cost_usd'] is not None and not valid_amount(quote['list_cost_usd']))):
        raise ContractError('Invalid provider cost quote')
    payment = quote['payment']
    if payment is not None and (type(payment) is not dict
            or set(payment) != {'usd', 'quota_seconds', 'period'}
            or not valid_amount(payment['usd'])
            or type(payment['quota_seconds']) is not int or not 0 < payment['quota_seconds'] <= 31536000
            or payment['period'] not in ('quota', 'calendar_month', 'day', 'week')):
        raise ContractError('Invalid quota payment policy')
    if 'accounting' in quote:
        from api_agent.telemetry.accounting import validate_metadata
        validate_metadata(quote['accounting'])
    return quote


def quote_cost(provider, request, result):
    method = getattr(provider, 'quote_cost', None)
    return validate_quote(method(request, result)) if callable(method) else {'list_cost_usd': None, 'payment': None}
