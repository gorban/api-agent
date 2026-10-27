"""Reviewed regional Mantle Standard list prices, independent of AWS identity.

The public AmazonBedrock offer version 20261016210806 publishes Standard
input/output at USD 0.0000700000/0.0003000000 per 1K tokens for OSS20B
and USD 0.0001500000/0.0006000000 per 1K tokens for OSS120B.
The public two-category schedule is interpreted as an inclusive input list
equivalent: input subdivisions share that rate without asserting zero cache
usage or a caching capability. This bounded interpretation applies only here.
https://aws.amazon.com/bedrock/pricing/
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-20b.html
The model card defines an omitted/default tier as Standard.
These estimates are list prices, not account discounts or invoice evidence.
"""
from copy import deepcopy

from api_agent.providers.rates import compact_rate_card, price_usage, validate_rate_card

SOURCE = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/us-east-1/index.json'
MODEL = 'openai.gpt-oss-20b'


def bundled_rate_card(config):
    """Return fresh exact regional evidence; unqualified scopes stay unavailable."""
    if config.get('BEDROCK_REGION') != 'us-east-1' or config.get('BEDROCK_API_MODE', 'responses') not in ('responses', 'completions'):
        return None
    return validate_rate_card(dict(version=1, source=SOURCE, revision='20261016210806',
        verified_at='2026-10-25T00:00:00+00:00', fallback=True, currency='USD', unit='token',
        models={MODEL: dict(service_tier='standard', input=.07 / 1000000,
            cached_input=.07 / 1000000, cache_write=.07 / 1000000,
            output=.30 / 1000000, threshold=None, input_multiplier=1, output_multiplier=1),
            'openai.gpt-oss-120b': dict(service_tier='standard', input=.15 / 1000000,
            cached_input=.15 / 1000000, cache_write=.15 / 1000000,
            output=.60 / 1000000, threshold=None, input_multiplier=1, output_multiplier=1)}))


def quote(request, result, *, rate_card, compatibility):
    """Freeze each call's applicable evidence without claiming an AWS payment."""
    model = result.actual_model or request.selected_llm
    card = compact_rate_card(rate_card, model=model) if rate_card is not None else None
    amount = (price_usage(card, model, result.usage)
              if card is not None and result.service_tier in (None, 'default', 'standard') else None)
    return {'list_cost_usd': amount, 'payment': None, 'accounting': dict(
        version=1, requested_model=request.selected_llm, catalog_model=request.selected_llm,
        actual_model=result.actual_model, serving_revision=result.serving_revision,
        service_tier='standard' if result.service_tier in (None, 'default', 'standard') else result.service_tier,
        billing_mode='metered',
        cache_policy=request.payload.generation.cache_policy,
        reasoning_effort=request.payload.generation.reasoning_effort or None,
        rate_card=card, compatibility=deepcopy(compatibility))}
