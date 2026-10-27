"""The picker entry point enforces current provider scope before history pricing."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import factory, mantle, pricing
from api_agent.providers import rates
from api_agent.telemetry.accounting import compatibility, source_fingerprint
from api_agent.testing.collection import TestIdentity, repository_label
from api_agent.testing.cost_context import cost_config
from api_agent.testing.cost_history import estimator_for


@pytest.fixture
def history_context(tmp_path):
    from api_agent.config.declarations import load_shared_schema
    from api_agent.config import resolve_config
    from api_agent.providers.loading import bootstrap_schema
    base = Path(__file__).resolve().parents[2]
    pack = tmp_path / 'synthetic'; pack.mkdir()
    (pack / 'pack.toml').write_text('version = 1\n')
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version = 1\n')
    (pack / 'test_case.py').write_text('def test_case(): pass\n')
    context = SimpleNamespace(base_root=base, owner_root=pack, pack_root=pack,
                              config_path=pack / 'cfg/config.toml')
    provider = str(Path(factory.__file__).resolve()) + ':factory'
    schema = bootstrap_schema(load_shared_schema(base), (), {}, {}, {'LLM_PROVIDER': provider}, owner_root=pack)
    config = resolve_config(schema, environ={}, dotenv={}, overrides=dict(
        LLM_PROVIDER=provider, TUI_MODE='integration', E2E_RERUNS='0',
        LLM_DEFAULT_MODEL=pricing.MODEL, LLM_CACHE_POLICY='', SKIP_ALLURE_REPORTING='True'),
        anchors={'base_root': base, 'owner_root': pack, 'pack_root': pack}).raise_for_errors().values
    test = TestIdentity('test', repository_label(context, pack), 'test_case.py', 'test_case.py::test_case', root=pack)
    resolved = cost_config(context, config)
    assert resolved['_COST_REQUEST_SETTINGS'] is not None
    assert {'BEDROCK_REGION', 'BEDROCK_API_MODE'} <= resolved['_COST_REQUEST_SETTINGS'].keys()
    settings = compatibility(resolved)
    from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent
    request = LLMRequest(pricing.MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),)))
    result = mantle.decode(dict(model=pricing.MODEL, service_tier='default', serving_revision='revision',
        status='completed', output=[], usage=dict(input_tokens=1000, output_tokens=200, total_tokens=1200)),
        'responses', selected_llm=pricing.MODEL)
    assert result.actual_model == pricing.MODEL
    assert result.serving_revision == 'revision'
    old = pricing.bundled_rate_card(config)
    old['revision'] = 'old'
    old['models'][pricing.MODEL]['output'] = 99
    quote = pricing.quote(request, result, rate_card=old, compatibility=settings)
    call = dict(quote=quote, usage=vars(result.usage))
    row = dict(test_id=test.id, node_id=test.node_id, source_fingerprint=source_fingerprint(test),
        compatibility=settings, outcomes=['passed'], calls_complete=True, observed_call_count=1, calls=[call])
    records = [(dict(finished_at='2026-10-25T00:00:00+00:00'), dict(run_id='run', tests=[row]))]
    cache = SimpleNamespace(records=lambda *args: records)
    return context, config, test, records, cache


@pytest.mark.parametrize('region,mode,expected', [('us-east-1', 'responses', pytest.approx(.00013)),
    ('us-west-2', 'responses', None), ('us-east-1', 'completions', None), ('us-east-1', 'runtime', None)])
@pytest.mark.parametrize('override', ['argument', 'config'])
@pytest.mark.parametrize('source', [rates.OPENAI, rates.BEDROCK])
def test_top_level_history_uses_current_local_card_and_exact_configuration(history_context, region, mode, expected, override, source):
    context, config, test, records, cache = history_context
    original = deepcopy(records)
    config.update(BEDROCK_REGION=region, BEDROCK_API_MODE=mode)
    wrong = rates.bundled_rate_card()
    wrong['source'] = source
    kwargs = dict(rate_card=wrong) if override == 'argument' else {}
    if override == 'config':
        config['_COST_RATE_CARD'] = wrong
    snapshot = estimator_for(context, [test], config, cache=cache,
        now=datetime(2026, 10, 25, 1, tzinfo=timezone.utc), **kwargs)
    assert snapshot.card == pricing.bundled_rate_card(config)
    assert snapshot.predictions[test.id]['estimated_cost_usd'] == expected
    assert records == original


@pytest.mark.parametrize('field,value', [('actual_model', None), ('service_tier', 'priority')])
def test_history_rejects_missing_actual_model_or_unsupported_tier(history_context, field, value):
    context, config, test, records, cache = history_context
    records[0][1]['tests'][0]['calls'][0]['quote']['accounting'][field] = value
    snapshot = estimator_for(context, [test], config, cache=cache,
                             now=datetime(2026, 10, 25, 1, tzinfo=timezone.utc))
    assert snapshot.card['source'] == pricing.SOURCE
    assert snapshot.predictions[test.id]['estimated_cost_usd'] is None
