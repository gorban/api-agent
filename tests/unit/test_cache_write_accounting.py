"""Cache charges remain disjoint and immutable across recording boundaries."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
import json

import pytest

from api_agent.providers import rates
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import result_from_wire, result_to_wire
from api_agent.providers.llm import Usage, LLMResult, Message, TextContent, LLMRequest, CompletionRequest, GenerationOptions
from api_agent.telemetry.accounting import validate_metadata, build_accounting
from api_agent.telemetry.estimation import call_signature
from api_agent.telemetry.history import HistoryCache
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.inference_usage import publish, capture
from api_agent.llm_providers.gpt.factory import GPTProvider
from test_historical_pricing import _html, _grouped


def test_partition_prices_cache_writes_without_additive_fees_or_reasoning():
    parts = rates.price_usage_parts(rates.bundled_rate_card(), 'gpt-6-luna', Usage(100, 50, 20, 30, 40))
    # 40 ordinary + 20 reads + 40 writes, not 80 ordinary plus a write fee.
    assert parts['input_tokens'] == pytest.approx((40 * .1 + 20 * .01 + 40 * .125) / 1e6)
    assert parts['cache_creation_tokens'] == pytest.approx(40 * .125 / 1e6)
    assert parts['reasoning_tokens'] == pytest.approx(30 * .5 / 1e6)
    assert parts['output_tokens'] == pytest.approx(50 * .5 / 1e6)
    assert parts['total_tokens'] == pytest.approx(parts['input_tokens'] + parts['output_tokens'])


@pytest.mark.parametrize('writes', [None, 40])
def test_missing_write_price_never_silently_prices_writes_as_ordinary(writes):
    card = rates.bundled_rate_card()
    del card['models']['gpt-6-luna']['cache_write']
    assert rates.validate_rate_card(card) is card  # Immutable legacy evidence stays readable.
    assert rates.price_usage(card, 'gpt-6-luna', Usage(100, 50, 20, 30, writes)) is None
    assert rates.price_usage(card, 'gpt-6-luna', Usage(100, 50, 20, 30, 0)) is not None


def test_unknown_counts_and_equal_rates_and_free_explicit_writes():
    card = rates.bundled_rate_card()
    assert rates.price_usage(card, 'gpt-6-luna', Usage(100, 50, 20, 30)) is None
    assert rates.price_usage(card, 'gpt-4.1-nano', Usage(100, 50, 20, 30)) is not None
    assert rates.price_usage_parts(card, 'gpt-4.1-nano', Usage(100, 50, 20, 30))['cache_creation_tokens'] is None
    card['models']['gpt-6-luna']['cache_write'] = 0
    assert rates.price_usage(card, 'gpt-6-luna', Usage(100, 50, 20, 30, 80)) == pytest.approx(25.2 / 1e6)


@pytest.mark.parametrize('usage', [Usage(100, 50, 20, 30), Usage(100, 50, 20, 30, 40)])
def test_success_and_failure_worker_transport_preserves_write_observations(usage):
    from api_agent.providers.setup_transport import _error, _raise_error
    result = LLMResult(Message('assistant', (TextContent('Ok'),)), usage)
    wire = result_to_wire(result)
    assert ('cache_write_tokens' in wire['usage']) == (usage.cache_write_tokens is not None)
    assert result_from_wire(wire).usage == usage
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(_error(ProviderAccessError('authorization', 'denied', status=403, usage=usage)))
    assert caught.value.usage == usage


@pytest.mark.parametrize('writes', [-1, True, 81])
def test_cache_reads_and_writes_cannot_exceed_input(writes):
    with pytest.raises(ValueError):
        Usage(100, 50, 20, 30, writes)


@pytest.mark.parametrize('responses', [True, False])
def test_both_api_usage_shapes_preserve_cache_writes_and_reject_overflow(responses):
    from api_agent.llm_providers.gpt.inference import _usage
    input_name, output_name, details = (('input_tokens', 'output_tokens', 'input_tokens_details') if responses else
                                       ('prompt_tokens', 'completion_tokens', 'prompt_tokens_details'))
    data = {input_name:100, output_name:50, details:{'cached_tokens':20, 'cache_write_tokens':40}}
    assert _usage(data, responses).cache_write_tokens == 40
    for value in (81, 2147483648, True):
        data[details]['cache_write_tokens'] = value
        with pytest.raises(ProviderAccessError):
            _usage(data, responses)
    with pytest.raises(ContractError):
        ProviderAccessError('authorization', 'denied', usage=Usage(cache_write_tokens=2147483648))


def test_public_parsers_keep_write_rates_and_context_scaling():
    card = rates.parse_openai(_html([['gpt-new', 4, .4, 5, 20],['gpt-5.6-sol',4,.4,5,20]]) + _grouped(groups=None))
    assert card['models']['gpt-new']['cache_write'] == .000005
    contextual = card['models']['gpt-5.6-sol']
    assert contextual['cache_write'] == .000005 and contextual['input_multiplier'] == 2
    parts = rates.price_usage_parts(card, 'gpt-5.6-sol', Usage(300000, 100, 0, 0, 300000))
    assert parts['cache_creation_tokens'] == pytest.approx(3) and parts['total_tokens'] == pytest.approx(3.003)
    raw = json.dumps({'data':[{'id':'route/model', 'pricing':{'prompt':'.000004',
        'completion':'.00002', 'input_cache_read':'.0000004', 'input_cache_write':'.000005'}}]}).encode()
    router = rates.parse_openrouter(raw)
    assert router['models']['route/model']['cache_write'] == .000005
    assert rates.price_usage(router, 'route/model', Usage(100,50,20,30,40)) == pytest.approx(.001368)


@pytest.mark.parametrize('subscription,mode', [(False,'responses'),(False,'completions'),(True,'responses')])
@pytest.mark.parametrize('failed', [False, True])
def test_http_and_streamed_usage_preserve_write_counts_even_on_failure(subscription, mode, failed):
    import httpx
    from test_gpt_inference import result, run
    data = result(mode == 'responses')
    details = 'input_tokens_details' if mode == 'responses' else 'prompt_tokens_details'
    data['usage'][details] = {'cached_tokens':1, 'cache_write_tokens':2}
    if failed:
        data = {'error':{'message':'denied'}, 'usage':data['usage']}
        with pytest.raises(ProviderAccessError) as caught:
            run(httpx.Response(403,json=data),subscription=subscription,api_mode=mode)
        assert caught.value.usage.cache_write_tokens == 2
    else:
        response = (httpx.Response(200,text='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n',
                                  headers={'content-type':'text/event-stream'}) if subscription else httpx.Response(200,json=data))
        value, _ = run(response,subscription=subscription,api_mode=mode)
        assert value.usage.cache_write_tokens == 2 and value.usage.input_tokens == 3


def test_context_and_standard_write_prices_must_agree():
    with pytest.raises(ContractError, match='conflict'):
        rates.parse_openai(_html([['gpt-5.6-sol',4,.4,6,20]]) + _grouped())
    assert rates._context_rate([4,.4,5,20], [8,.8,11,30], 272000)['input'] is None


@pytest.mark.parametrize('value', ['invalid', True])
def test_metadata_rejects_invalid_cache_policy(value):
    provider = GPTProvider({})
    request = LLMRequest('gpt-6-luna', CompletionRequest((Message('user',(TextContent('Ok'),)),)))
    result = LLMResult(Message('assistant',(TextContent('Ok'),)), Usage(100,50,20,30,40))
    metadata = provider.quote_cost(request,result)['accounting']
    metadata['cache_policy'] = value
    with pytest.raises(ContractError):
        validate_metadata(metadata)


def test_effective_per_call_policy_and_counts_survive_allure_sqlite_and_csv(tmp_path, monkeypatch):
    provider = GPTProvider({'GPT_AUTH_MODE':'api_key', 'LLM_CACHE_POLICY':'high'})
    request = LLMRequest('gpt-6-luna', CompletionRequest((Message('user',(TextContent('Ok'),)),),
        generation=GenerationOptions(cache_policy='off')))
    result = LLMResult(Message('assistant',(TextContent('Ok'),)), Usage(100,50,20,30,40), actual_model='gpt-6-luna')
    quote = provider.quote_cost(request,result)
    assert quote['accounting']['cache_policy'] == 'off'
    configured = replace(request, payload=replace(request.payload, generation=GenerationOptions()))
    assert provider.quote_cost(configured,result)['accounting']['cache_policy'] == 'high'
    first = {'calls':[{'quote':quote}], 'compatibility':quote['accounting']['compatibility']}
    # Complete fixtures are needed to exercise matching rather than missing-config rejection.
    from test_historical_estimation import compat
    first['compatibility'] = compat()
    first['calls'][0]['quote']['accounting']['compatibility'] = compat()
    second = deepcopy(first)
    second['calls'][0]['quote']['accounting']['cache_policy'] = 'high'
    assert call_signature(first) != call_signature(second)
    del second['calls'][0]['quote']['accounting']['cache_policy']
    assert call_signature(first) != call_signature(second)
    # Restore validated config metadata before persisting real artifact structure.
    quote = provider.quote_cost(request,result)
    events = tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test (call)')
    publish(request,result,quote=quote)
    tracker = SuiteUsage('run')
    identity = TestIdentity('repo::test.py::test','repo','test.py','test.py::test',root=tmp_path)
    capture(SimpleNamespace(text=json.dumps(json.loads(events.read_text())['data']),identity=identity),tracker)
    row = tracker.rows()[0]
    assert row['cache_creation_tokens'] == 40 and row['total_tokens'] == 150
    from api_agent.telemetry.usage import CSV_COLUMNS
    assert CSV_COLUMNS == ('llm_provider','llm_model','llm_calls','session_id','input_tokens',
        'output_tokens','total_tokens','cache_read_tokens','cache_creation_tokens','reasoning_tokens')
    import csv
    tracker.write_csv(tmp_path/'tokens.csv')
    with (tmp_path/'tokens.csv').open() as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == list(CSV_COLUMNS)
    assert rows[1][4:] == ['100','50','150','20','40','30']
    run_id = '00000000-0000-4000-8000-000000000001'
    artifact = build_accounting(SimpleNamespace(run_id=run_id),[identity],{},tracker,[TestResult(identity,'passed')])
    payload = json.dumps(artifact).encode()
    manifest = dict(schema=1,run_id=run_id,repo='repo',mode='integration',status='completed',
        started_at='2026-10-11T00:00:00+00:00',finished_at='2026-10-11T00:01:00+00:00',
        files={'accounting.json':sha256(payload).hexdigest()})
    cache = HistoryCache(tmp_path/'history.sqlite3')
    try:
        cache.ingest('local',manifest,payload)
        cached = cache.records()[0][1]['tests'][0]['calls'][0]
        assert cached['usage']['cache_write_tokens'] == 40
        assert cached['quote']['accounting']['cache_policy'] == 'off'
        assert cached['quote']['accounting']['rate_card']['models']['gpt-6-luna']['cache_write'] == .000000125

    finally:
        cache.close()
