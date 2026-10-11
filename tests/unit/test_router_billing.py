"""Reported dollars stay distinct from frozen prices and rounded observations."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json

import httpx
import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.costs import validate_billing, qualified_charge
from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent, Usage, LLMResult
from api_agent.providers.inference import result_to_wire, result_from_wire
from api_agent.providers.rates import parse_openrouter, price_usage, _router_context_rate, _rate
from api_agent.llm_providers.gpt.inference import complete_text, _billing
from api_agent.llm_providers.gpt.pricing import quote
from api_agent.telemetry.estimation import EstimateSnapshot, reported_charge, predictions_for
from api_agent.telemetry.accounting import validate_metadata, validate_accounting, build_accounting, compatibility
from api_agent.telemetry.usage import SuiteUsage
from api_agent.telemetry.usage_costs import attach_category_costs
from api_agent.telemetry.usage_output import final_usage_text
from api_agent.testing.inference_usage import publish, capture
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult

REQUEST = LLMRequest('openai/gpt-6-luna', CompletionRequest((Message('user', (TextContent('Ok'),)),)))
BILLING = dict(usd=.0000036, request_id='req-review', generation_id='gen-review')


def card():
    return parse_openrouter(json.dumps({'data': [dict(id=REQUEST.selected_llm,
        pricing=dict(prompt='.0000001', completion='.0000005', input_cache_read='.00000001', input_cache_write='.000000125'))]}).encode())


def response(responses=False):
    data = dict(id='gen-review', request_id='req-review', model=REQUEST.selected_llm)
    usage = dict(cost=.0000036)
    if responses:
        data.update(status='completed', output=[dict(type='message', role='assistant', content=[dict(type='output_text', text='Ok')])])
        usage.update(input_tokens=11, output_tokens=5, input_tokens_details=dict(cached_tokens=0), output_tokens_details=dict(reasoning_tokens=0))
    else:
        data.update(choices=[dict(finish_reason='stop', message=dict(role='assistant', content='Ok'))])
        usage.update(prompt_tokens=11, completion_tokens=5, prompt_tokens_details=dict(cached_tokens=0), completion_tokens_details=dict(reasoning_tokens=0))
    return dict(data, usage=usage)


@pytest.mark.parametrize('responses', [False, True])
@pytest.mark.parametrize('endpoint', ['https://openrouter.ai/api/v1', 'https://api.openai.com/v1'])
def test_charge_capture_on_recognized_route_only(responses, endpoint):
    import asyncio
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(responses)))) as client:
            return await complete_text(client=client, base_url=endpoint, access_token='private-secret',
                subscription=False, api_mode='responses' if responses else 'completions', request=REQUEST)
    result = asyncio.run(run())
    assert result.usage.input_tokens == 11 and result.usage.output_tokens == 5
    assert result.billing == (BILLING if 'openrouter' in endpoint else None)
    assert result.usage.cache_write_tokens == (0 if 'openrouter' in endpoint else None)
    assert result_from_wire(result_to_wire(result)) == result
    quoted = quote(REQUEST, result, direct=False, subscription=False, rate_card=card())
    if 'openrouter' in endpoint:
        assert quoted['list_cost_usd'] == pytest.approx(.0000036)
        assert validate_metadata(quoted['accounting'])['billing'] == BILLING
    else:
        assert 'billing' not in quoted['accounting']
    assert 'billing' not in quote(REQUEST, result, direct=False, subscription=True)['accounting']


@pytest.mark.parametrize('value', [None, {}, {'usd':0}, dict(BILLING, extra='secret'), dict(BILLING, usd=True),
    dict(BILLING, usd=-1), dict(BILLING, usd=float('nan')), dict(BILLING, usd=float('inf')),
    dict(BILLING, request_id=3), dict(BILLING, generation_id='\n'), dict(BILLING, generation_id='x'*201)])
def test_billing_shape_rejects_unsafe_values(value):
    with pytest.raises(ContractError):
        validate_billing(value)


def test_optional_ids_and_missing_cost():
    value = dict(usd=None, request_id=None, generation_id=None)
    assert validate_billing(value) is value
    assert qualified_charge(value, {}) is None
    assert qualified_charge(None, {}) is None
    assert _billing({}, 'req-header', 'secret') == dict(usd=None,request_id='req-header',generation_id=None)
    assert _billing(dict(id='gen-only'), None, 'secret')['generation_id'] == 'gen-only'
    with pytest.raises(ProviderAccessError, match='billing metadata'):
        _billing(dict(id='gen-secret',usage=dict(cost=.1)), None, 'secret')
    with pytest.raises(ProviderAccessError, match='billing metadata'):
        _billing(dict(id='gen-safe',usage=dict(cost=-1)), None, 'secret')
    with pytest.raises(ContractError):
        result_from_wire(dict(text='Ok',outcome='completed',usage=vars(Usage()),billing={}))


@pytest.mark.parametrize('amount,usage,expected', [(0,{'input_tokens':1},None), (.0000009,{'output_tokens':1},None),
    (.000001,{'input_tokens':1},.000001), (0,{'input_tokens':0,'output_tokens':0},0), (.0000009,{},None)])
def test_sub_display_charge_is_not_a_zero_cost_observation(amount, usage, expected):
    assert qualified_charge(dict(BILLING,usd=amount), usage) == expected


def artifact(amount=.0000036, *, usage=None, prices=None):
    result=LLMResult(Message('assistant',(TextContent('Ok'),)), usage or Usage(11,5,0,0,0), actual_model=REQUEST.selected_llm,billing=dict(BILLING,usd=amount))
    evidence=quote(REQUEST,result,direct=False,subscription=False,rate_card=prices)
    settings=compatibility(dict(_COST_PROVIDER_IDENTITY='a'*64,LLM_DEFAULT_MODEL='model',LLM_CACHE_POLICY='off',E2E_RERUNS=0,_COST_PACK_IDENTITY='b'*64,_COST_REQUEST_SETTINGS={}))
    evidence['accounting']['compatibility']=settings
    row=dict(test_id='t',node_id='test.py::test',source_fingerprint='fingerprint',compatibility=settings,outcomes=['passed'],
        calls_complete=True,observed_call_count=1,list_cost_usd=evidence['list_cost_usd'],calls=[dict(quote=evidence,usage=vars(result.usage))])
    return dict(tests=[row],quota={},rate_card=prices)


def test_qualified_charge_preferred_without_prices_and_tiny_fallback():
    value=artifact(prices=card())
    value['tests'][0]['calls'][0]['quote']['accounting']['billing']['usd']=.0000042
    snap=EstimateSnapshot({},card=card())
    completed=snap.complete(value)['t']
    assert completed['basis']=='reported_charge'
    assert completed['estimated_cost_usd']==.0000042
    assert value['tests'][0]['list_cost_usd']==pytest.approx(.0000036)
    assert EstimateSnapshot({}).complete(artifact())['t']['estimated_cost_usd']==.0000036
    for raw in (0,.0000001):
        value=artifact(raw,prices=card())
        completed=snap.complete(value)['t']
        assert completed['basis']=='list_price'
        assert completed['estimated_cost_usd']==pytest.approx(.0000036)
        assert reported_charge(value['tests'][0]) is None
        assert value['tests'][0]['calls'][0]['quote']['accounting']['billing']['usd']==raw


def test_history_fallback_uses_qualified_charge_only():
    test=SimpleNamespace(id='t',node_id='test.py::test')
    value=artifact()
    expected=value['tests'][0]['compatibility']
    predict=lambda value:predictions_for([test],[({},value)],None,{'t':'fingerprint'},expected)['t']
    assert predict(value)['estimated_cost_usd']==.0000036
    assert predict(value)['basis']=='historical_reported_charge'
    assert predict(artifact(0))['estimated_cost_usd'] is None
    assert predict(artifact(.0000001))['sample_count']==0
    # Its tokens may still be repriced by current rates, just as zero quota deltas.
    assert predictions_for([test],[({},artifact(0))],card(),{'t':'fingerprint'},expected)['t']['estimated_cost_usd']==pytest.approx(.0000036)


@pytest.mark.parametrize('change', ['incomplete','missing_calls','empty','unobserved','subscription','missing_billing'])
def test_partial_or_non_key_evidence_never_supplies_a_charge(change):
    row=artifact()['tests'][0]
    if change=='incomplete':row['calls_complete']=False
    elif change=='missing_calls':row['calls']=None
    elif change=='empty':row['calls']=[]
    elif change=='unobserved':row['observed_call_count']=2
    elif change=='subscription':row['calls'][0]['quote']['accounting']['billing_mode']='subscription'
    else:row['calls'][0]['quote']['accounting'].pop('billing')
    assert reported_charge(row) is None


def test_catalog_sentinels_keep_valid_rates_and_explicit_thresholds():
    rate=dict(prompt='.0000001',completion='.0000005',input_cache_read='.00000001',input_cache_write='.000000125',
        overrides=[dict(min_prompt_tokens=272000,prompt='.0000002',completion='.00000075',input_cache_read='.00000002',input_cache_write='.00000025')])
    rows=[dict(id='router',pricing=dict(prompt='-1',completion='-1')),dict(id='router-output',pricing=dict(prompt='0',completion='-1')),dict(id='gpt',pricing=rate)]
    snapshot=parse_openrouter(json.dumps(dict(data=rows)).encode())
    assert price_usage(snapshot,'router',Usage(11,5,0,0,0)) is None
    assert price_usage(snapshot,'gpt',Usage(11,5,0,0,0))==pytest.approx(.0000036)
    assert price_usage(snapshot,'gpt',Usage(272000,1,0,0,0))==pytest.approx(.05440075)
    rows[-1]['pricing']['prompt']='-2'
    with pytest.raises(ContractError):parse_openrouter(json.dumps(dict(data=rows)).encode())


@pytest.mark.parametrize('overrides', [3,[],[{},{}],[3],[{}],[dict(min_prompt_tokens=True)],[dict(min_prompt_tokens=1)],
    [dict(min_prompt_tokens=20,prompt='x')], [dict(min_prompt_tokens=20,prompt=None)],
    [dict(min_prompt_tokens=20,prompt='nan')], [dict(min_prompt_tokens=20,prompt='0')],
    [dict(min_prompt_tokens=20,prompt='2',input_cache_read='3',input_cache_write='2',completion='2')]])
def test_unsupported_context_tiers_are_unavailable(overrides):
    assert _router_context_rate(_rate(1,1,1),overrides)['input'] is None


def test_unknown_cache_rate_does_not_infer_context_price():
    assert _router_context_rate(_rate(1,None,1),[dict(min_prompt_tokens=20,prompt='2')])['input'] is None


def test_worker_evidence_to_allure_accounting_and_display(tmp_path,monkeypatch):
    path=tmp_path/'events';monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path));monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test (call)')
    result=LLMResult(Message('assistant',(TextContent('Ok'),)),Usage(11,5,0,0,0),actual_model=REQUEST.selected_llm,billing=BILLING)
    tracker=SuiteUsage('run');identifier=publish(REQUEST)
    publish(REQUEST,result_from_wire(result_to_wire(result)),call_id=identifier,quote=quote(REQUEST,result,direct=False,subscription=False,rate_card=card()))
    for line in path.read_text().splitlines():
        capture(SimpleNamespace(text=json.dumps(json.loads(line)['data']),identity=SimpleNamespace(id='t')),tracker)
    test=TestIdentity('t','repo','','test.py::test',root=tmp_path)
    value=build_accounting(SimpleNamespace(run_id='00000000-0000-4000-8000-000000000001'),[test],{'_COST_RATE_CARD':card()},tracker,[TestResult(test,'passed')])
    assert validate_accounting(json.loads(json.dumps(value)))['tests'][0]['calls'][0]['quote']['accounting']['billing']==BILLING
    costs=EstimateSnapshot({},card=card()).complete(value)
    report=tracker.report();report['test_costs']['t'].update(costs['t']);report['estimated_cost_usd']=costs['t']['estimated_cost_usd'];report['cost_basis']=costs['t']['basis']
    value['tests'][0]['stabilized_estimate']=costs['t']
    attach_category_costs(report,value)
    assert report['category_costs_usd']['input_tokens']==pytest.approx(.0000011)
    assert report['category_costs_usd']['output_tokens']==pytest.approx(.0000025)
    assert 'This suite cost: $0.0000036' in final_usage_text(report)
    assert 'Cost basis: reported_charge' in final_usage_text(report)
    assert tracker.rows()[0]['total_tokens']==16
    from hashlib import sha256
    from api_agent.telemetry.history import HistoryCache
    payload=json.dumps(value).encode()
    manifest=dict(schema=1,repo='repo',mode='integration',started_at='2026-10-11T00:00:00Z',finished_at='2026-10-11T01:00:00Z',run_id=value['run_id'],status='completed',files={'accounting.json':sha256(payload).hexdigest()})
    cache=HistoryCache(tmp_path/'cost.sqlite3')
    cache.ingest('review',manifest,payload)
    cache.close()
    cache=HistoryCache(tmp_path/'cost.sqlite3')
    restored=cache.records('review')[0][1]
    assert restored['tests'][0]['calls'][0]['quote']['accounting']['billing']==BILLING
    assert restored['tests'][0]['stabilized_estimate']['estimated_cost_usd']==.0000036
    cache.close()
    assert 'private-secret' not in path.read_text()
