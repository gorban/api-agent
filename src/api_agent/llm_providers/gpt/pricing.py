"""Provider-owned standard text list rates, verified 2026-10-10."""
# USD per million tokens. Sources: official model pages; see the provider PRD.
_RATES = {'gpt-6-luna': (.1, .01, .5), 'gpt-6-sol': (2, .2, 10),
          'gpt-6.1-sol': (2, .1, 10), 'gpt-4.1-nano': (.1, .025, .4)}


def quote(request, result, *, direct, subscription, payment_usd=100, quota_seconds=604800, pay_period='month'):
    usage = result.usage
    rates = _RATES.get(request.selected_llm) if direct else None
    amount = None
    if rates is not None and usage.input_tokens is not None and usage.output_tokens is not None:
        cached = usage.cached_input_tokens or 0
        multiplier = 2 if request.selected_llm.startswith('gpt-6') and usage.input_tokens > 272000 else 1
        # Reasoning is already included in output_tokens, never charged twice.
        amount = ((usage.input_tokens - cached) * rates[0] * multiplier
                  + cached * rates[1] * multiplier
                  + usage.output_tokens * rates[2] * (1.5 if multiplier == 2 else 1)) / 1000000
    return {'list_cost_usd': amount, 'payment': {'usd': payment_usd, 'quota_seconds': quota_seconds,
            'period': 'calendar_month' if pay_period == 'month' else pay_period} if subscription else None}
