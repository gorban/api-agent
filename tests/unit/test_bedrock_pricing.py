"""Local public list quotes use exact qualified scope and freeze call evidence."""
from dataclasses import replace

import pytest

from api_agent.llm_providers.bedrock import pricing
from api_agent.llm_providers.bedrock.factory import factory
from api_agent.providers.costs import validate_quote
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage


@pytest.fixture
def llm_request():
    return LLMRequest(pricing.MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),)))


@pytest.fixture
def result():
    return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(1000, 200),
                     actual_model=pricing.MODEL, serving_revision='revision')


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_exact_regional_snapshot_and_fresh_copy(mode):
    card = pricing.bundled_rate_card({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_API_MODE': mode})
    assert card['source'] == pricing.SOURCE
    assert card['revision'] == '20261016210806'
    assert card['verified_at'] == '2026-10-25T00:00:00+00:00'
    assert card['models'][pricing.MODEL] == dict(service_tier='standard', input=.07/1000000,
        cached_input=.07/1000000, cache_write=.07/1000000, output=.30/1000000,
        threshold=None, input_multiplier=1, output_multiplier=1)
    card['models'].clear()
    assert pricing.MODEL in pricing.bundled_rate_card({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_API_MODE': mode})['models']


@pytest.mark.parametrize('region,mode', [(None, 'responses'), ('us-west-2', 'responses'),
    ('us-east-1', 'runtime'), ('US-EAST-1', 'responses')])
def test_unqualified_scope_has_no_card(region, mode):
    assert pricing.bundled_rate_card({'BEDROCK_REGION': region, 'BEDROCK_API_MODE': mode}) is None


@pytest.mark.parametrize('auth_mode', ['api_key', 'session', 'access_key'])
def test_quote_is_metered_list_price_for_each_auth_mode(llm_request, result, auth_mode):
    provider = factory({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_AUTH_MODE': auth_mode}, {})
    quote = validate_quote(provider.quote_cost(llm_request, result))
    assert quote['list_cost_usd'] == pytest.approx(.00013)
    assert quote['payment'] is None
    assert quote['accounting']['billing_mode'] == 'metered'
    assert quote['accounting']['actual_model'] == pricing.MODEL
    assert quote['accounting']['serving_revision'] == 'revision'
    quote['accounting']['rate_card']['models'].clear()
    quote['accounting']['compatibility'].clear()
    assert provider.quote_cost(llm_request, result)['accounting']['rate_card']['models']
    assert provider.quote_cost(llm_request, result)['accounting']['compatibility']


@pytest.mark.parametrize('tier,known', [(None, True), ('standard', True), ('default', True), ('priority', False)])
def test_tier_selection(llm_request, result, tier, known):
    card = pricing.bundled_rate_card({'BEDROCK_REGION': 'us-east-1'})
    quote = pricing.quote(llm_request, replace(result, service_tier=tier), rate_card=card, compatibility={})
    assert (quote['list_cost_usd'] is not None) is known


@pytest.mark.parametrize('usage', [Usage(), Usage(1000, None), Usage(None, 200)])
def test_missing_usage_stays_unknown(llm_request, result, usage):
    quote = pricing.quote(llm_request, replace(result, usage=usage),
        rate_card=pricing.bundled_rate_card({'BEDROCK_REGION': 'us-east-1'}), compatibility={})
    assert quote['list_cost_usd'] is None


def test_actual_model_controls_rate_and_missing_actual_uses_requested(llm_request, result):
    provider = factory({'BEDROCK_REGION': 'us-east-1'}, {})
    assert provider.quote_cost(llm_request, replace(result, actual_model='other'))['list_cost_usd'] is None
    assert provider.quote_cost(llm_request, replace(result, actual_model=None))['list_cost_usd'] == pytest.approx(.00013)
    assert provider.quote_cost(replace(llm_request, selected_llm='other'), replace(result, actual_model=None))['list_cost_usd'] is None


def test_unqualified_region_keeps_usage_price_unknown(llm_request, result):
    quote = factory({}, {}).quote_cost(llm_request, result)
    assert quote['list_cost_usd'] is None
    assert quote['accounting']['rate_card'] is None


@pytest.mark.parametrize('usage', [Usage(1000, 200, 300, 100, 200), Usage(1000, 200, 300),
    Usage(1000, 200, None, None, 200), Usage(1000, 200, 0, 0, 0)])
def test_input_subdivisions_share_inclusive_list_equivalent(llm_request, result, usage):
    provider = factory({'BEDROCK_REGION': 'us-east-1'}, {})
    quote = provider.quote_cost(llm_request, replace(result, usage=usage))
    assert quote['list_cost_usd'] == pytest.approx(.00013)


def test_construction_freezes_scope_without_environment_reads(llm_request, result, monkeypatch):
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    config = {'BEDROCK_REGION': 'us-east-1'}
    provider = factory(config, {})
    config['BEDROCK_REGION'] = 'us-west-2'
    assert provider.quote_cost(llm_request, result)['list_cost_usd'] == pytest.approx(.00013)
    assert factory({}, {}).quote_cost(llm_request, result)['list_cost_usd'] is None


@pytest.mark.parametrize('mode', ['responses', 'completions'])
@pytest.mark.parametrize('usage', [Usage(1000, 200), Usage(1000, 200, 300, 100, 200),
    Usage(1000, 200, None, None, 100), Usage(1000, 200, 0, 0, 0)])
def test_oss120_actual_quote_uses_inclusive_input_and_output_totals(llm_request, result, mode, usage):
    provider = factory({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_API_MODE': mode}, {})
    selected = replace(llm_request, selected_llm='openai.gpt-oss-120b')
    actual = replace(result, actual_model='openai.gpt-oss-120b', usage=usage)

    quote = validate_quote(provider.quote_cost(selected, actual))

    assert quote['list_cost_usd'] == pytest.approx(.00027)
    assert quote['payment'] is None
    assert quote['accounting']['actual_model'] == 'openai.gpt-oss-120b'
    assert quote['accounting']['service_tier'] == 'standard'
    assert quote['accounting']['rate_card']['models'] == {
        'openai.gpt-oss-120b': dict(service_tier='standard', input=.15/1000000,
            cached_input=.15/1000000, cache_write=.15/1000000, output=.60/1000000,
            threshold=None, input_multiplier=1, output_multiplier=1)}


def test_actual_oss120_model_uses_its_rate_over_requested_oss20(llm_request, result):
    provider = factory({'BEDROCK_REGION': 'us-east-1'}, {})
    quote = provider.quote_cost(llm_request, replace(result, actual_model='openai.gpt-oss-120b'))
    assert quote['list_cost_usd'] == pytest.approx(.00027)
    assert provider.quote_cost(llm_request, result)['list_cost_usd'] == pytest.approx(.00013)


@pytest.mark.parametrize('region,tier', [('us-west-2', None), ('us-east-1', 'priority')])
def test_oss120_outside_reviewed_region_or_tier_stays_unpriced(llm_request, result, region, tier):
    provider = factory({'BEDROCK_REGION': region}, {})
    selected = replace(llm_request, selected_llm='openai.gpt-oss-120b')
    actual = replace(result, actual_model='openai.gpt-oss-120b', service_tier=tier)
    assert provider.quote_cost(selected, actual)['list_cost_usd'] is None
