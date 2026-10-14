"""Review reproductions cross the real recording, history and HTTP boundaries."""
import asyncio
from copy import deepcopy
import csv
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import Usage
from api_agent.telemetry.estimation import EstimateSnapshot
from api_agent.telemetry.history import HistoryCache
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import run_recorded_integration
from test_recorded_integration import fixture, Storage
from test_router_billing import artifact, card
from test_gpt_inference import result, run
from api_agent.llm_providers.gpt.cache_policy import prompt_cache_fields


@pytest.mark.parametrize('log_content',[False,True])
def test_recording_parent_preserves_cache_writes_through_callback_csv_and_rebuilt_history(tmp_path,monkeypatch,log_content):
    context,test,config=fixture(tmp_path)
    config['LOG_CONTENT']=log_content
    monkeypatch.setattr('api_agent.providers.rates.freeze_pricing',lambda *args: None)
    callbacks=[]
    identifier=str(uuid4())
    def runner(context,tests,**hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('inference_usage',identity=test,text=json.dumps({
            'call_id':identifier,'selection':'synthetic-model','usage':vars(Usage(100,50,20,30,40))})))
        outcome=TestResult(test,'passed')
        hooks['on_result'](outcome)
        completed=IntegrationRunResult(0,(outcome,),'')
        hooks['after_run'](completed)
        return completed
    completed=run_recorded_integration(context,[test],config,accepted=True,runner=runner,
        storage=Storage(),on_event=callbacks.append)
    assert completed.persisted and completed.exit_code==0
    assert json.loads(callbacks[0].text)['usage']['cache_write_tokens']==40
    saved=json.loads((completed.directory/'accounting.json').read_text())
    assert saved['tests'][0]['calls'][0]['usage']['cache_write_tokens']==40
    with (completed.directory/'token-usage.csv').open() as stream:
        count=list(csv.DictReader(stream))[0]
    assert count['cache_creation_tokens']=='40' and count['input_tokens']=='100' and count['total_tokens']=='150'
    cache=HistoryCache(tmp_path/'rebuild.sqlite3')
    cache.ingest('synthetic',completed.manifest,(completed.directory/'accounting.json').read_bytes())
    cache.close()
    reopened=HistoryCache(tmp_path/'rebuild.sqlite3')
    try:
        assert reopened.records()[0][1]['tests'][0]['calls'][0]['usage']['cache_write_tokens']==40
    finally:
        reopened.close()


def test_completed_charge_history_with_known_prices_preserves_first_and_two_sample_execution_costs():
    current=dict(artifact(.0042,prices=card()),run_id='current')
    prior=dict(artifact(.0021,prices=card()),run_id='prior')
    empty=EstimateSnapshot({},card=card())
    first=empty.completed_averages(current,empty.complete(current))['t']
    snapshot=EstimateSnapshot({},card=card(),records=[({},prior)])
    average=snapshot.completed_averages(current,snapshot.complete(current))['t']
    assert first['estimated_cost_usd']==pytest.approx(.0042) and first['sample_count']==1
    assert average['estimated_cost_usd']==pytest.approx(.00315) and average['sample_count']==2
    assert current['tests'][0]['list_cost_usd']==pytest.approx(.0000036)


def test_mixed_reported_charge_and_unbilled_request_preserves_each_contribution():
    current=artifact(.0042,prices=card())
    row=current['tests'][0]
    unbilled=deepcopy(row['calls'][0])
    unbilled['quote']['accounting'].pop('billing')
    row['calls'].append(unbilled)
    row['observed_call_count']=2
    row['list_cost_usd']*=2
    estimate=EstimateSnapshot({},card=card()).complete(current)['t']
    assert estimate['estimated_cost_usd']==pytest.approx(.0042036)
    assert estimate['basis']=='mixed'
    assert row['calls'][0]['quote']['accounting']['billing']['usd']==.0042


@pytest.mark.parametrize('status',['in_progress','incomplete'])
@pytest.mark.parametrize('item_type',['message','reasoning'])
@pytest.mark.parametrize('subscription',[False,True])
def test_unfinished_terminal_items_fail_json_and_terminal_only_sse_without_losing_usage(status,item_type,subscription):
    data=result()
    item={'type':'reasoning'} if item_type=='reasoning' else deepcopy(data['output'][0])
    item['status']=status
    data['output'].insert(0,item)
    response=httpx.Response(200,text='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n',
        headers={'content-type':'text/event-stream'}) if subscription else httpx.Response(200,json=data)
    with pytest.raises(ProviderAccessError) as caught:
        run(response,subscription=subscription)
    assert caught.value.usage.input_tokens==3 and caught.value.usage.output_tokens==1


@pytest.mark.parametrize('selection',['gpt-6-unknown','gpt-6-luna-2026-02-30','gpt-4.1-2026-02-30',
    'gpt-4.1-２０２６-１０-１１','gpt-6-luna-２０２６１０１１'])
@pytest.mark.parametrize('policy',['medium','high'])
def test_official_cache_preflight_rejects_unknown_families_and_invalid_ascii_snapshots(selection,policy):
    with pytest.raises(ProviderAccessError,match='unknown'):
        prompt_cache_fields(policy,selection,base_url='https://api.openai.com/v1',subscription=False)
    assert prompt_cache_fields('',selection,base_url='https://api.openai.com/v1',subscription=False)=={}


@pytest.mark.parametrize('allow',[False,True])
def test_redaction_expansion_cannot_replace_a_completed_loopback_result(allow):
    from api_agent.providers.llm import CompletionRequest,LLMRequest,LLMResult,Message,TextContent
    from api_agent.providers.setup_transport import SetupClient,SetupServer
    from api_agent.telemetry.privacy import ContentPolicy,EmissionBoundary
    from api_agent.testing.inference_content import export_content
    from test_setup_transport import Registry,CAPABILITY
    request=LLMRequest('synthetic-model',CompletionRequest((Message('user',(TextContent('Ok'),)),)))
    expected=LLMResult(Message('assistant',(TextContent('al\n'*21845),)),Usage(100,50,20,30,40))
    boundary=EmissionBoundary(ContentPolicy(allow),secrets=('al',))
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.setup_transport import MAX_FRAME, _content_frame
    wire=result_to_wire(expected)
    assert len(expected.payload.content[0].text) <= 65536
    assert len(json.dumps(wire,ensure_ascii=False).encode()) <= 131072
    rows=export_content(boundary,request,expected)
    if allow:
        response=next(row for row in rows if row['phase']=='response')
        assert len(json.dumps(response, separators=(',', ':')).encode()) > MAX_FRAME
        assert _content_frame(response) is None
    else:
        assert rows == ()
    class Owner(Registry):
        async def complete(self,binding,request):
            self.calls+=1
            return expected
        def export_content(self,binding,request,value):
            return export_content(boundary,request,value)
    async def scenario():
        registry=Owner()
        server=SetupServer(registry,CAPABILITY,timeout=2)
        descriptor=await server.start()
        def consumer():
            client=SetupClient(descriptor,timeout=2)
            try:
                completed=client.complete(request)
                return completed,client.evidence
            finally:
                client.close()
        try:
            actual,evidence=await asyncio.to_thread(consumer)
            assert actual==expected and registry.calls==1
            assert all('al' not in json.dumps(row) for row in evidence)
            assert [row['phase'] for row in evidence]==(['request'] if allow else [])
        finally:
            await server.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('evidence',[None,{}, {'phase':'bad','content':{}},
    {'phase':'request','content':[]}, {'phase':'request','content':{'number':float('nan')}},
    {'phase':'response','content':{'text':'x'*262144}}])
def test_invalid_or_oversized_optional_evidence_has_no_frame(evidence):
    from api_agent.providers.setup_transport import _content_frame
    assert _content_frame(evidence) is None


def test_optional_export_failure_and_duplicates_preserve_primary_result(monkeypatch):
    from api_agent.providers.llm import CompletionRequest,LLMRequest,LLMResult,Message,TextContent
    from api_agent.providers.setup_transport import SetupClient,SetupServer
    from test_setup_transport import Registry,CAPABILITY
    expected=LLMResult(Message('assistant',(TextContent('Ok'),)),Usage(1,1,0,0,0))
    request=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Ok'),)),)))
    class Owner(Registry):
        async def complete(self,binding,request): return expected
        def export_content(self,*args):
            yield {'phase':'request','content':{}}
            yield {'phase':'request','content':{}}
            raise RuntimeError('Synthetic optional export failure')
    async def scenario():
        server=SetupServer(Owner(),CAPABILITY,timeout=2)
        endpoint=await server.start()
        def consumer():
            client=SetupClient(endpoint,timeout=2)
            try:
                value=client.complete(request)
                assert client.evidence==[{'phase':'request','content':{}}]
                return value
            finally:
                client.close()
        try:
            assert await asyncio.to_thread(consumer)==expected
        finally:
            await server.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', ['incomplete','unknown','count_mismatch'])
def test_qualified_call_cannot_hide_an_unavailable_call(invalid):
    value=artifact(.0042,prices=card())
    row=value['tests'][0]
    second=deepcopy(row['calls'][0])
    second['quote']['accounting'].pop('billing')
    second['quote']['list_cost_usd']=None
    second['quote']['accounting']['actual_model']='unknown'
    row['calls'].append(second)
    row['observed_call_count']=2
    if invalid=='incomplete': row['calls_complete']=False
    if invalid=='count_mismatch': row['observed_call_count']=3
    estimate=EstimateSnapshot({},card=card()).complete(value)['t']
    assert estimate['estimated_cost_usd'] is None and estimate['basis']=='unavailable'


def test_missing_current_prices_still_allow_per_request_frozen_fallback():
    value=artifact(.0042,prices=card())
    row=value['tests'][0]
    missing=deepcopy(row['calls'][0])
    missing['quote']['accounting'].pop('billing')
    row['calls'].append(missing)
    row['observed_call_count']=2
    completed=EstimateSnapshot({}).complete(value)['t']
    assert completed['estimated_cost_usd']==pytest.approx(.0042036)
    assert completed['basis']=='mixed'


def test_mixed_billing_survives_immutable_parent_recording(tmp_path,monkeypatch):
    from test_router_billing import REQUEST
    context,test,config=fixture(tmp_path)
    prices=card()
    monkeypatch.setattr('api_agent.providers.rates.freeze_pricing',lambda *args:prices)
    source=artifact(.0042,prices=prices)['tests'][0]['calls'][0]
    unbilled=deepcopy(source)
    unbilled['quote']['accounting'].pop('billing')
    def runner(context,tests,**hooks):
        hooks['before_run']()
        for call in (source,unbilled):
            hooks['on_event'](RunnerEvent('inference_usage',identity=test,text=json.dumps(dict(
                call_id=str(uuid4()),selection=REQUEST.selected_llm,usage=call['usage'],
                quote=call['quote'],actual_model=REQUEST.selected_llm))))
        outcome=TestResult(test,'passed')
        hooks['on_result'](outcome)
        completed=IntegrationRunResult(0,(outcome,),'')
        hooks['after_run'](completed)
        return completed
    result=run_recorded_integration(context,[test],config,accepted=True,storage=Storage(),runner=runner)
    assert result.persisted and result.exit_code==0
    recorded=json.loads((result.directory/'accounting.json').read_text())['tests'][0]
    assert recorded['stabilized_estimate']['estimated_cost_usd']==pytest.approx(.0042036)
    assert recorded['stabilized_estimate']['basis']=='mixed'
    assert recorded['calls'][0]['quote']['accounting']['billing']['usd']==.0042
    assert 'billing' not in recorded['calls'][1]['quote']['accounting']
    assert result.usage['estimated_cost_usd']==pytest.approx(.0042036)


@pytest.mark.parametrize('selection',['gpt-6-luna-2030-12-31','gpt-6.1-sol-20301231','gpt-4.1-2030-12-31'])
def test_verified_official_family_permits_valid_future_snapshot(selection):
    assert prompt_cache_fields('high',selection,base_url='https://api.openai.com/v1',subscription=False)
