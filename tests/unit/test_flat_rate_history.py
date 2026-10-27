"""Flat inclusive rates preserve unknown usage and provider provenance."""
from copy import deepcopy

import pytest

from api_agent.providers import rates
from api_agent.providers.llm import Usage
from api_agent.telemetry.estimation import priced


def flat_card():
    card = rates.bundled_rate_card()
    card['source'] = rates.BEDROCK
    card['models'] = {'model': rates._rate(2, 2, 3, cache_write=2)}
    return card


@pytest.mark.parametrize('writes,reasoning', [(None, None), (10, 5)])
def test_flat_unknown_cache_prices_inclusive_totals_without_inventing_subsets(writes, reasoning):
    parts = rates.price_usage_parts(flat_card(), 'model', Usage(100, 20, None, reasoning, writes))
    assert parts == dict(input_tokens=200, output_tokens=60, cache_read_tokens=None,
                        cache_creation_tokens=None if writes is None else 20,
                        reasoning_tokens=None if reasoning is None else 15, total_tokens=260)


@pytest.mark.parametrize('change', [dict(input=None), dict(cached_input=1), dict(cache_write=1),
                                    dict(output=None), dict(threshold=50, input_multiplier=2),
                                    dict(threshold=50, output_multiplier=2)])
def test_unknown_cache_rejects_nonflat_or_unknown_categories(change):
    card = flat_card()
    card['models']['model'].update(change)
    assert rates.price_usage(card, 'model', Usage(100, 20)) is None


def test_unity_threshold_keeps_flat_prices_and_gpt_remains_conservative():
    card = flat_card()
    card['models']['model']['threshold'] = 50
    assert rates.price_usage(card, 'model', Usage(100, 20)) == 260
    assert rates.price_usage(rates.bundled_rate_card(), 'gpt-6-luna', Usage(100, 20)) is None


@pytest.mark.parametrize('source', [rates.BEDROCK, rates.OPENAI, None])
def test_history_requires_matching_provider_source_and_preserves_original(source):
    card = flat_card()
    frozen = deepcopy(card)
    frozen['revision'] = 'original'
    frozen['models']['model']['input'] = 99
    evidence = dict(actual_model='model', service_tier='standard', rate_card=frozen)
    if source is None:
        del evidence['rate_card']
    else:
        frozen['source'] = source
    row = dict(calls_complete=True, calls=[dict(quote=dict(accounting=evidence), usage=dict(input_tokens=100, output_tokens=20))])
    original = deepcopy(row)
    assert priced(row, card) == (260 if source == rates.BEDROCK else None)
    assert row == original


def test_history_never_reprices_aws_usage_with_other_provider_rates():
    card = flat_card()
    row = dict(calls_complete=True, calls=[dict(quote=dict(accounting=dict(actual_model='model',
        service_tier='standard', rate_card=card)), usage=dict(input_tokens=100, output_tokens=20))])
    other = deepcopy(card)
    other['source'] = rates.OPENAI
    assert priced(row, other) is None
    assert priced(row, None) is None


@pytest.mark.parametrize('billing,expected', [(None, None),
    (dict(usd=0.5, request_id='request', generation_id=None), 0.5),
    (dict(usd=0.0000001, request_id='request', generation_id=None), None)])
def test_metered_requires_qualified_charge_evidence(billing, expected):
    from api_agent.telemetry.estimation import reported_charge, quota_allowed
    from api_agent.testing.workflow_accounting import _call_facts
    call = dict(usage=dict(input_tokens=100, output_tokens=20),
                quote=dict(accounting=dict(billing_mode='metered', billing=billing)))
    row = dict(calls_complete=True, calls=[call])
    assert reported_charge(row) == expected
    assert _call_facts(call)['reported_charge_usd'] == expected
    assert quota_allowed(row) is False


def test_metadata_accepts_metered_without_billing_evidence():
    from api_agent.telemetry.accounting import compatibility, validate_metadata
    metadata = dict(version=1, requested_model='model', catalog_model='model', actual_model='model',
        serving_revision=None, service_tier='standard', billing_mode='metered',
        rate_card=flat_card(), compatibility=compatibility({}))
    assert validate_metadata(metadata) is metadata


def test_bedrock_picker_and_freeze_use_only_current_local_provider_card(monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from api_agent.llm_providers.bedrock import factory
    card = flat_card()
    seen = []
    def local_card(config):
        seen.append(config)
        return deepcopy(card)
    monkeypatch.setitem(sys.modules, 'api_agent.llm_providers.bedrock.pricing',
                        SimpleNamespace(bundled_rate_card=local_card))
    config = dict(LLM_PROVIDER=str(Path(factory.__file__).resolve()) + ':factory',
                  _COST_RATE_CARD=rates.bundled_rate_card())
    assert rates.cached_rate_card(config=config) == card
    assert rates.freeze_pricing(None, config) == card
    assert seen == [config, config]
