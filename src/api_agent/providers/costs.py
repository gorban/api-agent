"""Bounded provider-owned list quotes and quota payment policies."""
import math
from .contracts import ContractError


def valid_amount(value):
    return type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value)


def validate_quote(quote):
    if (type(quote) is not dict or set(quote) != {'list_cost_usd', 'payment'}
            or (quote['list_cost_usd'] is not None and not valid_amount(quote['list_cost_usd']))):
        raise ContractError('Invalid provider cost quote')
    payment = quote['payment']
    if payment is not None and (type(payment) is not dict
            or set(payment) != {'usd', 'quota_seconds', 'period'}
            or not valid_amount(payment['usd'])
            or type(payment['quota_seconds']) is not int or not 0 < payment['quota_seconds'] <= 31536000
            or payment['period'] not in ('quota', 'calendar_month', 'day', 'week')):
        raise ContractError('Invalid quota payment policy')
    return quote


def quote_cost(provider, request, result):
    method = getattr(provider, 'quote_cost', None)
    return validate_quote(method(request, result)) if callable(method) else {'list_cost_usd': None, 'payment': None}
