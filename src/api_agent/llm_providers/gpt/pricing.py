"""Provider-owned quotes with frozen public text rates and call evidence."""
from copy import deepcopy
from api_agent.providers.rates import bundled_rate_card, price_usage, compact_rate_card
from api_agent.telemetry.accounting import compatibility as cost_compatibility


def quote(request, result, *, direct, subscription, payment_usd=100, quota_seconds=604800, pay_period='month', rate_card=None, compatibility=None):
    card = rate_card if rate_card is not None else bundled_rate_card() if direct else None
    model = getattr(result, 'actual_model', None) or request.selected_llm
    tier = getattr(result, 'service_tier', None)
    amount = price_usage(card, model, result.usage) if tier in (None, 'standard', 'default') and card is not None and (direct or card['source'].startswith('https://openrouter.ai/')) else None
    result_quote = {'list_cost_usd': amount, 'payment': {'usd': payment_usd, 'quota_seconds': quota_seconds,
            'period': 'calendar_month' if pay_period == 'month' else pay_period} if subscription else None,
            'accounting': dict(version=1, requested_model=request.selected_llm, catalog_model=request.selected_llm,
                actual_model=getattr(result, 'actual_model', None), serving_revision=getattr(result, 'serving_revision', None), service_tier=tier, billing_mode='subscription' if subscription else 'api_key',
                cache_policy=request.payload.generation.cache_policy,
                reasoning_effort=request.payload.generation.reasoning_effort or None,
                rate_card=compact_rate_card(card, model=model) if card is not None and (direct or card['source'].startswith('https://openrouter.ai/')) else None,
                compatibility=deepcopy(compatibility) if compatibility is not None else cost_compatibility({}))}

    if getattr(result, 'billing', None) is not None and not subscription:
        result_quote['accounting']['billing'] = deepcopy(result.billing)
    return result_quote
