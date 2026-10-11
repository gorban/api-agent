"""Displayed token buckets and rounded dollar contributions form a partition."""
from copy import deepcopy
from decimal import Decimal
import re

import pytest

from api_agent.providers.rates import bundled_rate_card, price_usage, USAGE_COST_FIELDS
from api_agent.telemetry.usage_costs import attach_category_costs, test_category_costs as allocate
from api_agent.telemetry.usage_output import tokens_text, _partition_row, _exclusive_costs, _rounded_costs
from test_usage_costs import call, row


def priced_report(*, writes=0, reasoning=24, estimate=.00004008):
    evidence = row(call(usage={'input_tokens':2054,'output_tokens':38,
        'cached_input_tokens':2048,'reasoning_tokens':reasoning,'cache_write_tokens':writes}))
    artifact = {'rate_card':bundled_rate_card(), 'tests':[
        {**evidence,'test_id':'test','stabilized_estimate':{'estimated_cost_usd':estimate}}]}
    report = {'estimated_cost_usd':estimate,'test_costs':{'test':{'estimated_cost_usd':estimate}},
        'usage_rows':[{'session_id':'test','input_tokens':2054,'output_tokens':38,
            'cache_read_tokens':2048,'cache_creation_tokens':writes,'reasoning_tokens':reasoning,'total_tokens':2092}]}
    attach_category_costs(report,artifact)
    return report, artifact


def assert_contributions_sum(text):
    amounts = [Decimal(value) for value in re.findall(r'\$([\d.e+\-]+)',text)]
    assert amounts and sum(amounts[:-1]) == amounts[-1]


@pytest.mark.parametrize('scope',[None,'test'])
@pytest.mark.parametrize('writes',[0,None])
def test_reported_charge_displays_nonoverlapping_costs_and_preserves_evidence(scope,writes):
    report, artifact = priced_report(writes=writes)
    text = tokens_text(report,scope)
    label = 'non-cached input' if writes == 0 else 'non-cached input (writes unreported)'
    assert f'{label}: 6 — $0.0000006' in text
    assert 'non-reasoning output: 14 — $0.000007' in text
    assert 'cached input: 2,048 — $0.00002048' in text
    assert 'reasoning: 24 — $0.000012' in text
    assert 'total: 2,092 — $0.00004008' in text
    assert ('Unreported cache writes' in text) == (writes is None)
    assert_contributions_sum(text)
    assert artifact['tests'][0]['calls'][0]['usage']['cache_write_tokens'] is writes
    assert artifact['tests'][0]['calls'][0]['usage']['input_tokens'] == 2054
    assert artifact['tests'][0]['stabilized_estimate']['category_costs_version'] == 2


def test_unknown_writes_allow_only_an_explicit_derived_split_not_list_price_qualification():
    report, artifact = priced_report(writes=None)
    evidence = artifact['tests'][0]['calls'][0]['usage']
    assert price_usage(artifact['rate_card'],'gpt-6-luna',evidence) is None
    assert report['category_cost_assumptions'] == ['unreported_cache_writes_at_input_rate']
    assert report['test_costs']['test']['category_cost_assumptions'] == report['category_cost_assumptions']
    assert evidence['cache_write_tokens'] is None
    assert report['category_costs_usd']['total_tokens'] == pytest.approx(.00004008)


def test_known_writes_and_reasoning_use_their_own_rates_without_double_counting():
    evidence = row(call(usage={'input_tokens':100,'output_tokens':50,
        'cached_input_tokens':20,'cache_write_tokens':40,'reasoning_tokens':30}))
    amounts = allocate(evidence,bundled_rate_card(),.0000342)
    assert amounts == pytest.approx({'input_tokens':.000004,'output_tokens':.00001,
        'cache_read_tokens':.0000002,'cache_creation_tokens':.000005,
        'reasoning_tokens':.000015,'total_tokens':.0000342})
    assert sum(amounts[field] for field in USAGE_COST_FIELDS[:-1]) == pytest.approx(amounts['total_tokens'])
    assert _partition_row({'input_tokens':100,'output_tokens':50,'cache_read_tokens':20,
        'cache_creation_tokens':40,'reasoning_tokens':30}) == {'input_tokens':40,'output_tokens':20,
            'cache_read_tokens':20,'cache_creation_tokens':40,'reasoning_tokens':30}


def test_unknown_reasoning_retains_output_bucket_without_inventing_raw_zero():
    report, artifact = priced_report(reasoning=None)
    text = tokens_text(report)
    assert 'output (reasoning unreported): 38 — $0.000019' in text
    assert 'reasoning: unavailable' in text
    assert artifact['tests'][0]['calls'][0]['usage']['reasoning_tokens'] is None
    assert_contributions_sum(text)


@pytest.mark.parametrize('count',[None,-1,True,'4'])
def test_bad_or_unknown_counts_cannot_be_subtracted_as_measured_tokens(count):
    raw = {'input_tokens':count,'output_tokens':count,'cache_read_tokens':0}
    original = deepcopy(raw)
    assert _partition_row(raw)['input_tokens'] is None
    assert _partition_row(raw)['output_tokens'] is None
    assert raw == original


@pytest.mark.parametrize('parent,subsets',[('input_tokens',{'cache_read_tokens':3}),
                                          ('output_tokens',{'reasoning_tokens':3})])
def test_impossible_subset_does_not_produce_negative_display(parent,subsets):
    assert _partition_row({parent:2,**subsets})[parent] is None


@pytest.mark.parametrize('version',[None,1,2,3])
def test_legacy_category_conversion_is_versioned_and_never_mutates_raw_costs(version):
    costs = {'input_tokens':.2,'output_tokens':.8,'cache_read_tokens':.01,'reasoning_tokens':.16}
    original = deepcopy(costs)
    expected = {None:{**costs,'input_tokens':.19,'output_tokens':.64},
                1:{**costs,'input_tokens':.19,'output_tokens':.64},2:costs,3:{}}[version]
    assert _exclusive_costs({'category_costs_version':version,'category_costs_usd':costs},[]) == pytest.approx(expected)
    assert costs == original


def test_legacy_missing_subset_price_leaves_parent_allocation_unknown():
    costs = _exclusive_costs({'category_costs_usd':{'input_tokens':1,'output_tokens':2}},
        [{'cache_read_tokens':3,'reasoning_tokens':2}])
    assert costs == {'input_tokens':None,'output_tokens':None}


@pytest.mark.parametrize('total',[.004230001,1.234567891,.00000001234,2.12345e-12,0.])
def test_rounding_reconciles_every_displayed_dollar_to_displayed_total(total):
    report = {'estimated_cost_usd':total,'category_costs_version':2,
        'category_costs_usd':{'input_tokens':total/3,'output_tokens':total/3,'reasoning_tokens':total/3},
        'usage_rows':[{'input_tokens':1,'cache_read_tokens':0,'cache_creation_tokens':0,
                      'output_tokens':2,'reasoning_tokens':1,'total_tokens':3}]}
    assert_contributions_sum(tokens_text(report))


@pytest.mark.parametrize('costs,total,unallocated',[
    ({'input_tokens':.25},1,True),({'input_tokens':2},1,True),
    ({'input_tokens':0},0,False),({},0,False),({},1,True),
    ({'input_tokens':.25},None,False)])
def test_partial_and_inconsistent_prices_never_silently_drop_unallocated_cost(costs,total,unallocated):
    amounts = _rounded_costs(costs,total)
    assert ('unallocated' in amounts) == unallocated
    assert amounts.get('total_tokens') == (None if total is None else f'${total:.3f}')


def test_cost_with_no_known_positive_count_is_explicitly_unallocated():
    report = {'estimated_cost_usd':1,'category_costs_version':2,
        'category_costs_usd':{'input_tokens':1},'usage_rows':[{'input_tokens':0,'total_tokens':3}]}
    text = tokens_text(report)
    assert 'input (writes unreported): unavailable' in text
    assert 'unallocated token cost: $1.000' in text
    assert 'total: 3 — $1.000' in text
    assert 'input (writes unreported): unavailable —' not in text


def test_missing_rates_preserve_reported_total_without_inventing_category_prices():
    report, artifact = priced_report()
    artifact['rate_card'] = {'models':{}}
    attach_category_costs(report,artifact)
    text = tokens_text(report)
    assert 'non-cached input: 6\n' in text
    assert 'unallocated token cost: $0.00004008' in text
    assert_contributions_sum(text)
