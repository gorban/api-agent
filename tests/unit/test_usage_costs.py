"""Frozen category prices allocate the finalized estimate, never add token subsets."""
from copy import deepcopy
import math

import pytest

from api_agent.providers.rates import bundled_rate_card, price_usage, price_usage_parts, USAGE_COST_FIELDS
from api_agent.telemetry.usage_costs import attach_category_costs, test_category_costs as category_costs


def call(*, model='gpt-6-luna', actual=True, usage=None):
    return {'usage': usage or {'input_tokens':100,'output_tokens':50,'cached_input_tokens':20,'reasoning_tokens':30,'cache_write_tokens':0},
        'quote':{'accounting':{'actual_model':model if actual else None,'catalog_model':model,
            'rate_card':bundled_rate_card(),'service_tier':'standard'}}}


def row(*calls):
    return {'calls':list(calls),'calls_complete':True,'observed_call_count':len(calls)}


def test_frozen_prices_and_calibrated_total_preserve_cached_and_reasoning_subsets():
    card = bundled_rate_card()
    evidence = row(call())
    raw = deepcopy(evidence)
    prices = price_usage_parts(card, 'gpt-6-luna', evidence['calls'][0]['usage'])
    assert prices['input_tokens'] == pytest.approx(.0000082)
    assert prices['output_tokens'] == pytest.approx(.000025)
    assert prices['cache_read_tokens'] == pytest.approx(.0000002)
    assert prices['reasoning_tokens'] == pytest.approx(.000015)
    assert prices['total_tokens'] == price_usage(card, 'gpt-6-luna', evidence['calls'][0]['usage'])
    costs = category_costs(evidence, card, 1)
    assert costs['input_tokens'] == pytest.approx(8/33.2)
    assert costs['output_tokens'] == pytest.approx(10/33.2)
    assert costs['cache_read_tokens'] == pytest.approx(.2/33.2)
    assert costs['reasoning_tokens'] == pytest.approx(15/33.2)
    assert costs['cache_creation_tokens'] == 0
    assert costs['total_tokens'] == pytest.approx(1)
    assert math.fsum(costs[field] for field in USAGE_COST_FIELDS[:-1]) == pytest.approx(1)
    assert costs['cache_read_tokens'] + costs['reasoning_tokens'] > 0
    assert evidence == raw


def test_long_context_prices_apply_the_same_factors_to_subset_categories():
    evidence = row(call(model='gpt-6-sol', usage={'input_tokens':272001,'output_tokens':50,
        'cached_input_tokens':20,'reasoning_tokens':30,'cache_write_tokens':0}))
    card = bundled_rate_card()
    parts = price_usage_parts(card, 'gpt-6-sol', evidence['calls'][0]['usage'])
    assert parts['input_tokens'] == pytest.approx(271981 * .000004 + 20 * .0000004)
    assert parts['output_tokens'] == pytest.approx(50 * .000015)
    assert parts['cache_read_tokens'] == pytest.approx(20 * .0000004)
    assert parts['reasoning_tokens'] == pytest.approx(30 * .000015)
    assert parts['total_tokens'] == price_usage(card, 'gpt-6-sol', evidence['calls'][0]['usage'])


def test_multiple_models_and_attempts_use_summed_prices_not_raw_token_proportions():
    first, second = call(), call(model='gpt-4.1-nano')
    evidence = row(first, first, second)
    card = bundled_rate_card()
    weights = [price_usage_parts(card, item['quote']['accounting']['actual_model'], item['usage']) for item in evidence['calls']]
    costs = category_costs(evidence, card, .42)
    total = math.fsum(weight['total_tokens'] for weight in weights)
    for weight in weights:
        weight['input_tokens'] -= weight['cache_read_tokens'] + weight['cache_creation_tokens']
        weight['output_tokens'] -= weight['reasoning_tokens']
    for field in USAGE_COST_FIELDS:
        if field == 'cache_creation_tokens':
            assert costs[field] == 0
        else:
            assert costs[field] == pytest.approx(.42 * math.fsum(weight[field] for weight in weights) / total)
    assert costs['input_tokens'] != pytest.approx(.42 * 300 / 450)


@pytest.mark.parametrize('card', [None, bundled_rate_card()])
def test_legacy_unknown_revision_uses_quote_snapshot_and_catalog_id(card):
    evidence = row(call(actual=False))
    evidence['calls'][0]['quote']['accounting']['rate_card']['models']['gpt-6-luna']['input'] = .0001
    costs = category_costs(evidence, card, 1)
    assert costs['input_tokens'] == pytest.approx(.008/.0080252)
    assert evidence['calls'][0]['quote']['accounting']['actual_model'] is None


@pytest.mark.parametrize('change', [
    lambda evidence: evidence.update(calls_complete=False),
    lambda evidence: evidence.update(calls=None),
    lambda evidence: evidence.update(observed_call_count=2),
    lambda evidence: evidence['calls'][0]['quote']['accounting'].update(service_tier='priority'),
    lambda evidence: evidence['calls'][0]['quote']['accounting'].update(actual_model=None, rate_card=None),
    lambda evidence: evidence['calls'][0]['quote']['accounting'].update(actual_model=None, catalog_model=None),
    lambda evidence: evidence['calls'][0].update(usage=None),
    lambda evidence: evidence['calls'][0].update(quote=None),
    lambda evidence: evidence['calls'][0]['usage'].update(input_tokens=-1),
    lambda evidence: evidence['calls'][0]['quote']['accounting'].update(actual_model='unknown'),
    lambda evidence: evidence['calls'][0]['usage'].update(cached_input_tokens=None),
    lambda evidence: evidence['calls'][0]['quote']['accounting'].update(actual_model=None, rate_card={}),
])
def test_incomplete_or_unpriced_calls_never_invent_category_costs(change):
    evidence = row(call())
    change(evidence)
    assert category_costs(evidence, bundled_rate_card(), 1) == dict.fromkeys(USAGE_COST_FIELDS)


@pytest.mark.parametrize('estimate', [None, True, -1, float('nan')])
def test_invalid_estimate_is_not_replaced_with_list_price(estimate):
    assert category_costs(row(call()), bundled_rate_card(), estimate) == dict.fromkeys(USAGE_COST_FIELDS)


def test_known_zero_calls_zero_prices_and_unknown_reasoning_remain_distinct():
    assert category_costs(row(), None, 0) == dict.fromkeys(USAGE_COST_FIELDS, 0)
    assert category_costs(row(), None, 1) == dict.fromkeys(USAGE_COST_FIELDS)
    zero = call(usage={'input_tokens':0,'output_tokens':0,'cached_input_tokens':0,'reasoning_tokens':0})
    costs = category_costs(row(zero), bundled_rate_card(), 0)
    assert costs == dict.fromkeys(USAGE_COST_FIELDS, 0)
    assert category_costs(row(zero), bundled_rate_card(), 1) == dict.fromkeys(USAGE_COST_FIELDS)
    evidence = row(call())
    evidence['calls'][0]['usage']['reasoning_tokens'] = None
    costs = category_costs(evidence, bundled_rate_card(), .02)
    assert costs['reasoning_tokens'] == 0 and costs['total_tokens'] == .02
    assert math.fsum(costs[field] for field in USAGE_COST_FIELDS[:-1]) == pytest.approx(.02)
    assert evidence['calls'][0]['usage']['reasoning_tokens'] is None


@pytest.mark.parametrize('second_estimate', [.3, None])
def test_attachment_preserves_original_evidence_and_sums_test_allocations(second_estimate):
    artifact = {'rate_card':bundled_rate_card(), 'tests':[
        {**row(call()),'test_id':'one','stabilized_estimate':{'estimated_cost_usd':.7}},
        {**row(call(model='gpt-4.1-nano')),'test_id':'two','stabilized_estimate':{'estimated_cost_usd':second_estimate}},
        {**row(),'test_id':'zero','stabilized_estimate':{'estimated_cost_usd':0}}]}
    report = {'test_costs':{item['test_id']:deepcopy(item['stabilized_estimate']) for item in artifact['tests']}}
    original = deepcopy(artifact)
    attach_category_costs(report, artifact)
    for item, before in zip(artifact['tests'], original['tests']):
        assert item['calls'] == before['calls']
        assert item['stabilized_estimate']['estimated_cost_usd'] == before['stabilized_estimate']['estimated_cost_usd']
        assert item['stabilized_estimate']['category_costs_usd'] == report['test_costs'][item['test_id']]['category_costs_usd']
    for field in USAGE_COST_FIELDS:
        parts = [item['category_costs_usd'][field] for item in report['test_costs'].values()]
        assert report['category_costs_usd'][field] == (None if None in parts else math.fsum(parts))
    assert report['test_costs']['zero']['category_costs_usd'] == dict.fromkeys(USAGE_COST_FIELDS, 0)
    # Allure derived values and live display do not share a mutable dictionary.
    report['test_costs']['one']['category_costs_usd']['total_tokens'] = 999
    assert artifact['tests'][0]['stabilized_estimate']['category_costs_usd']['total_tokens'] == .7


def test_empty_suite_categories_are_known_zero():
    report = {}
    attach_category_costs(report, {'tests':[]})
    assert report['category_costs_usd'] == dict.fromkeys(USAGE_COST_FIELDS, 0)
