"""Token-category allocations of the already-finalized test and suite estimates."""
from copy import deepcopy
from dataclasses import replace
import math

from api_agent.providers.costs import valid_amount
from api_agent.providers.rates import USAGE_COST_FIELDS, price_usage_parts


def allocation_weights(card, model, usage):
    """Disjoint estimated weights; unknown writes stay in ordinary input.

    This never replaces raw usage or qualifies missing-price history. Only the
    already-known total is distributed: unreported writes use input-rate weights,
    and unreported reasoning stays in the output-rate bucket.
    """
    from api_agent.providers.llm import Usage
    observed = Usage(**usage)
    estimated = replace(observed, cache_write_tokens=observed.cache_write_tokens or 0,
                        reasoning_tokens=observed.reasoning_tokens or 0)
    parts = price_usage_parts(card, model, estimated)
    if parts['total_tokens'] is not None:
        parts['input_tokens'] = max(0., math.fsum((parts['input_tokens'],
            -parts['cache_read_tokens'], -parts['cache_creation_tokens'])))
        parts['output_tokens'] = max(0., parts['output_tokens'] - parts['reasoning_tokens'])
    return parts


def test_category_costs(row, card, estimate):
    """Use frozen list-price weights, scaled to this test's finalized cost basis."""
    unavailable = dict.fromkeys(USAGE_COST_FIELDS)
    calls = row.get('calls')
    if (row.get('calls_complete') is not True or not isinstance(calls, list)
            or row.get('observed_call_count', len(calls)) != len(calls) or not valid_amount(estimate)):
        return unavailable
    if not calls:
        return dict.fromkeys(USAGE_COST_FIELDS, 0.) if estimate == 0 else unavailable
    weights = []
    for call in calls:
        metadata = (call.get('quote') or {}).get('accounting') or {}
        if metadata.get('service_tier') not in (None, 'standard', 'default'):
            return unavailable
        model = metadata.get('actual_model')
        # Legacy quotes can price their recorded catalog ID even when the
        # serving revision is unknown; preserve that quote's frozen rate card.
        prices = card if model and card is not None else metadata.get('rate_card')
        model = model or metadata.get('catalog_model')
        if prices is None or model is None or call.get('usage') is None:
            return unavailable
        try:
            parts = allocation_weights(prices, model, call['usage'])
        except (ValueError, TypeError):
            return unavailable
        if parts['total_tokens'] is None:
            return unavailable
        weights.append(parts)
    total = math.fsum(part['total_tokens'] for part in weights)
    if total == 0 and estimate != 0:
        return unavailable
    return {field: None if any(part[field] is None for part in weights) else
            estimate * (math.fsum(part[field] for part in weights) / total) if total else 0.
            for field in USAGE_COST_FIELDS}


def attach_category_costs(report, artifact):
    """Derived Allure evidence and display share one allocation, including retries."""
    parts = []
    for row in artifact['tests']:
        costs = report['test_costs'][row['test_id']]
        categories = test_category_costs(row, artifact['rate_card'], costs.get('estimated_cost_usd'))
        costs['category_costs_usd'] = categories
        costs['category_costs_version'] = 2
        costs['category_cost_assumptions'] = (['unreported_cache_writes_at_input_rate']
            if categories['total_tokens'] is not None and any(
                call.get('usage', {}).get('cache_write_tokens') is None
                and (call.get('usage', {}).get('input_tokens') or 0) > (call.get('usage', {}).get('cached_input_tokens') or 0)
                for call in row.get('calls', ())) else [])
        row['stabilized_estimate']['category_costs_usd'] = deepcopy(categories)
        row['stabilized_estimate']['category_costs_version'] = 2
        row['stabilized_estimate']['category_cost_assumptions'] = costs['category_cost_assumptions'].copy()
        parts.append(categories)
    report['category_costs_usd'] = {field: None if any(part[field] is None for part in parts)
        else math.fsum(part[field] for part in parts) for field in USAGE_COST_FIELDS}
    report['category_costs_version'] = 2
    report['category_cost_assumptions'] = sorted({assumption for costs in report.get('test_costs', {}).values()
        for assumption in costs.get('category_cost_assumptions', ())})
