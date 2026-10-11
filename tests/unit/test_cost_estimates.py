"""Quota priority, payment periods and attribution do not invent charges."""
from datetime import datetime, timezone
from types import SimpleNamespace
import math

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.costs import validate_quote, quote_cost, valid_amount
from api_agent.telemetry.costs import CostLedger
from api_agent.telemetry.usage import SuiteUsage
from api_agent.llm_providers.gpt.pricing import quote

PAYMENT = {'usd':100,'quota_seconds':604800,'period':'calendar_month'}


def priced(amount, payment=None):
    return {'list_cost_usd':amount,'payment':payment}


@pytest.mark.parametrize('amount', [False, -1, float('inf'), float('nan'), '1', 10**400])
def test_invalid_quotes_do_not_cross_ipc(amount):
    assert not valid_amount(amount)
    with pytest.raises(ContractError):
        validate_quote(priced(amount))


@pytest.mark.parametrize('value', [None, [], {}, {'list_cost_usd':1},
    priced(1, {}), priced(1, dict(PAYMENT, usd=False)),
    priced(1, dict(PAYMENT, quota_seconds=False)), priced(1, dict(PAYMENT, quota_seconds=0)),
    priced(1, dict(PAYMENT, quota_seconds=31536001)), priced(1, dict(PAYMENT, period='year'))])
def test_invalid_shape_policy(value):
    with pytest.raises(ContractError):
        validate_quote(value)


def test_optional_quote_and_frozen_record_identity():
    assert quote_cost(object(), None, None) == priced(None)
    provider=SimpleNamespace(quote_cost=lambda *a:priced(3))
    assert quote_cost(provider, None, None)==priced(3)
    ledger=CostLedger(); ledger.record('c','t'); q=priced(3, dict(PAYMENT)); ledger.record('c','t',q)
    q['payment']['usd']=999
    ledger.record('c','t',priced(3,dict(PAYMENT)))
    with pytest.raises(ValueError,match='Conflicting'):
        ledger.record('c','other',priced(3))
    with pytest.raises(ValueError):
        ledger.record('c','t',priced(4))
    assert ledger.calls['c'][1]['payment']['usd']==100


@pytest.mark.parametrize('year,month,days', [(2026,10,31),(2026,4,30),(2026,2,28),(2028,2,29)])
def test_calendar_month_weekly_quota_preferred_and_proportioned(year,month,days):
    ledger=CostLedger();ledger.record('a','first',priced(1, PAYMENT));ledger.record('b','second',priced(3,PAYMENT))
    result=ledger.report({'pct_point_delta':2},{'first':1,'second':3},now=datetime(year,month,2,tzinfo=timezone.utc))
    expected=.02*100/(days/7)
    assert result['estimated_cost_usd']==pytest.approx(expected)
    assert result['list_cost_usd']==4 and result['cost_basis']=='quota_payment'
    assert result['payment_month']==f'{year}-{month:02}'
    assert result['test_costs']['first']['estimated_cost_usd']==pytest.approx(expected/4)
    assert result['test_costs']['second']['cost_per_prompt_usd']==pytest.approx(expected/4)
    assert result['cost_per_prompt_usd']==pytest.approx(expected/4)
    assert result['unallocated_cost_usd']==pytest.approx(0)


def test_daily_allocation_and_reset_fallback():
    ledger=CostLedger();ledger.record('a','t',priced(4,{'usd':100,'quota_seconds':86400,'period':'quota'}))
    result=ledger.report({'pct_point_delta':2},{'t':1})
    assert result['estimated_cost_usd']==2 and result['payment_month'] is None
    usage=SuiteUsage('suite'); usage.add_cost('a','t',priced(4, PAYMENT))
    usage.record_prompt(turn_id='t',prompt_id='p',kind='initial')
    report=usage.report()
    assert report['estimated_cost_usd']==4 and report['cost_basis']=='list_price'
    assert report['quota_unavailable_reason']=='missing_snapshot'


def test_missing_prices_zero_cost_and_zero_prompt_denominators():
    ledger=CostLedger();ledger.record('a','t',priced(None,PAYMENT))
    result=ledger.report({'pct_point_delta':2},{'t':0})
    assert result['cost_basis']=='quota_payment' and result['estimated_cost_usd']>0
    assert result['cost_per_prompt_usd'] is None
    assert result['test_costs']['t']['estimated_cost_usd'] is None
    assert result['unallocated_cost_usd']==result['estimated_cost_usd']
    assert ledger.report({}, {})['cost_basis']=='unavailable'
    zero=CostLedger();zero.record('z','t',priced(0,PAYMENT))
    result=zero.report({'pct_point_delta':0},{'t':1})
    assert result['test_costs']['t']['estimated_cost_usd']==0
    assert result['cost_per_prompt_usd']==0
    result=zero.report({'pct_point_delta':1},{'t':0})
    assert result['test_costs']['t']['estimated_cost_usd'] is None
    assert result['unallocated_cost_usd']==result['estimated_cost_usd']
    assert CostLedger().report({}, {'never_called':0})['test_costs']['never_called']['estimated_cost_usd']==0
    unknown=CostLedger();unknown.record('u','t')
    assert unknown.report({}, {})['test_costs']['t']['basis']=='unavailable'


def test_conflicting_payment_profiles_fall_back_without_mixing_quotas():
    ledger=CostLedger();ledger.record('a','one',priced(2,PAYMENT));ledger.record('b','two',priced(3,dict(PAYMENT,usd=200)))
    result=ledger.report({'pct_point_delta':99},{'one':1,'two':1})
    assert result['cost_basis']=='list_price' and result['estimated_cost_usd']==5
    assert result['payment_policy'] is None


@pytest.mark.parametrize('model,direct,counts,expected', [
    ('gpt-6-luna',True,(100,50,20,30),.000008+.0000002+.000025),
    ('gpt-6-sol',True,(272001,50,0,30),272001*4/1e6+50*15/1e6),
    ('gpt-4.1-nano',True,(300000,50,None,None),None),
    ('gpt-6-luna',False,(100,50,20,30),None),
    ('unknown',True,(100,50,20,30),None),
    ('gpt-6-luna',True,(None,50,20,30),None),
    ('gpt-6-luna',True,(100,None,20,30),None)])
def test_provider_list_prices_do_not_double_count_cache_or_reasoning(model,direct,counts,expected):
    usage=SimpleNamespace(**dict(zip(('input_tokens','output_tokens','cached_input_tokens','reasoning_tokens'),counts)))
    result=quote(SimpleNamespace(selected_llm=model),SimpleNamespace(usage=usage),direct=direct,subscription=True)
    assert result['list_cost_usd']==pytest.approx(expected) if expected is not None else result['list_cost_usd'] is None
    assert result['payment']==PAYMENT
    assert quote(SimpleNamespace(selected_llm=model),SimpleNamespace(usage=usage),direct=direct,subscription=False)['payment'] is None


@pytest.mark.parametrize('quota,pay,days,expected', [(86400,'day',31,2),(86400,'week',31,2/7),(604800,'day',31,14),(86400,'calendar_month',31,2/31)])
def test_overrideable_quota_and_pay_periods(quota,pay,days,expected):
    ledger=CostLedger();ledger.record('call','test',priced(10,dict(usd=100,quota_seconds=quota,period=pay)))
    result=ledger.report({'pct_point_delta':2},{'test':1},now=datetime(2026,10,1,tzinfo=timezone.utc))
    assert result['estimated_cost_usd']==pytest.approx(expected)


def test_float_config_can_be_saved_and_round_trips_with_decimal_dollars():
    from api_agent.config.declarations import FieldDeclaration, schema_from_dict
    from api_agent.config.resolve import convert_value
    field=FieldDeclaration('LLM_PAY_PERIOD_LIMIT','float',validation='nonnegative')
    assert convert_value(field,'100.25',external=True,anchors=None)==100.25
    assert convert_value(field,100,external=False,anchors=None)==100.0
    schema=schema_from_dict({'version':1,'fields':{'PRICE':{'type':'float','validation':'positive','default':100.25}}})
    assert schema.fields['PRICE'].default==100.25
    for value in ('bad','nan','inf','-0.01'):
        with pytest.raises(ValueError):convert_value(field,value,external=True,anchors=None)
    with pytest.raises(ValueError):convert_value(field,True,external=False,anchors=None)
    with pytest.raises(ValueError):convert_value(FieldDeclaration('PRICE','float',validation='positive'),'0',external=True,anchors=None)


def test_gpt_provider_pay_override_and_api_key_ignores_quota(tmp_path):
    from api_agent.llm_providers.gpt.factory import GPTProvider
    request=SimpleNamespace(selected_llm='gpt-6-luna')
    result=SimpleNamespace(usage=SimpleNamespace(input_tokens=1,output_tokens=1,cached_input_tokens=0))
    subject=GPTProvider({'GPT_AUTH_MODE':'subscription','LLM_QUOTA_PERIOD':'day','LLM_PAY_PERIOD':'week','LLM_PAY_PERIOD_LIMIT':123.45})
    assert subject.quote_cost(request,result)['payment']=={'usd':123.45,'quota_seconds':86400,'period':'week'}
    subject=GPTProvider({'GPT_AUTH_MODE':'api_key','GPT_BASE_URL':'https://api.openai.com/v1'})
    assert subject.quote_cost(request,result)['payment'] is None
    import asyncio
    assert asyncio.run(subject.asnapshot(auth='synthetic')) is None
    asyncio.run(subject.aclose())
    with pytest.raises(ContractError,match='closed'):
        asyncio.run(subject.asnapshot(auth='synthetic'))


def test_terminal_completed_summary_includes_same_frozen_cost_accounting():
    from api_agent.testing.inspection_controls import finish_terminal_inspection
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.collection import TestIdentity
    identity=TestIdentity('base::test.py::test','base','test.py','test.py::test')
    execution=IntegrationRunResult(0,(TestResult(identity,'passed'),),'')
    recorded=SimpleNamespace(execution=execution,persisted=True,elapsed=61,
                             usage={'estimated_cost_usd':.0000006,'cost_basis':'list_price','prompt_count':1})
    lines=[]
    assert finish_terminal_inspection(recorded,lines.append)
    assert lines[0]=='Integration results: PASS — 1/1 passed (100.00%) — 1m 1s total.'
    assert 'This suite cost: $0.0000006' in lines[2] and 'Cost basis: list_price' in lines[2]
