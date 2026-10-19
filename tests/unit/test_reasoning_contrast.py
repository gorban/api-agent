"""Threshold, real HTTP/native routing and ordinary integration-owner controls."""
import asyncio
from collections import Counter
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from api_agent.testing import reasoning_contrast as c


def pair(gap=50):
    return dict(calls=[dict(effort=effort,status='completed',
        reasoning_tokens=count,requested_model=c.MODEL,actual_model=None)
        for effort,count in (('low',1000),('high',1000+gap))])


@pytest.mark.parametrize('gaps,expected',[
    ([50,50,-49],True),([50,50,0],True),([50,50,50],True),
    ([49,50,49],False),([50,49,0],False),([50,50,-50],False),
    ([100,100,-51],False),([-50,100,100],False),
    ([89,29,49],False),([-47,7,17],False),
    ([112,-94,-214],False),([-123,92,101],False),([-22,-37,41],False)])
def test_signed_three_pair_threshold(gaps,expected):
    assert c.evaluate([pair(value) for value in gaps])==dict(passed=expected,differences=gaps)


@pytest.mark.parametrize('edit',[{'status':'length'},{'reasoning_tokens':None},
    {'reasoning_tokens':True},{'reasoning_tokens':-1},{'actual_model':'other'},
    {'requested_model':'other'}])
def test_all_calls_must_be_valid(edit):
    records=[pair() for _ in range(3)];records[2]['calls'][0].update(edit)
    assert not c.evaluate(records)['passed']


def test_incomplete_or_relabelled_pairs_are_not_discarded():
    assert not c.evaluate([pair(),pair()])['passed']
    records=[pair(),pair(),dict(calls=[])]
    assert not c.evaluate(records)['passed']
    records[2]=pair();records[2]['calls'].reverse()
    assert not c.evaluate(records)['passed']


@pytest.fixture
def config():
    factory=Path(c.__file__).resolve().parents[1]/'llm_providers/gpt/factory.py'
    return dict(LLM_PROVIDER=str(factory)+':factory')


@pytest.mark.parametrize('setting',[{'GPT_AUTH_MODE':'api_key'},{'GPT_API_MODE':'completions'},
    {'GPT_BASE_URL':'https://openrouter.ai/api/v1'},{'LLM_ALLOWED_MODELS':'gpt-6-sol'}])
def test_scope_rejects_other_routes_and_actual_disallowed_model(config,setting):
    from api_agent.providers.contracts import ProviderAccessError
    with pytest.raises((ValueError,ProviderAccessError)):
        c.scope(config|setting)


def test_scope_physical_provider_and_verified_efforts(config,tmp_path,monkeypatch):
    c.scope(config|{'LLM_DEFAULT_MODEL':'gpt-6-sol','LLM_REASONING_EFFORT':'off'})
    for value in (None,'',1):
        with pytest.raises(ValueError,match='Select'):c.scope({'LLM_PROVIDER':value})
    with pytest.raises(ValueError,match='owner root'):c.scope({'LLM_PROVIDER':'factory.py:factory'})
    factory=Path(config['LLM_PROVIDER'].rpartition(':')[0])
    c.scope({'LLM_PROVIDER':'factory.py:factory'},owner_root=factory.parent)
    with pytest.raises(ValueError,match='included'):c.scope({'LLM_PROVIDER':str(factory)+':other'})
    other=tmp_path/'other.py';other.write_text('def factory(): pass')
    with pytest.raises(ValueError,match='included'):c.scope({'LLM_PROVIDER':str(other)+':factory'})
    from api_agent.llm_providers.gpt import reasoning
    monkeypatch.setattr(reasoning,'reasoning_efforts',lambda *a,**kw:None)
    with pytest.raises(ValueError,match='unverified'):c.scope(config)


def response(body,**overrides):
    row=dict(session_id=body['session_id'],response_text='arbitrary response text',outcome='completed',
        usage=dict(input_tokens=1,output_tokens=200,cached_input_tokens=0,
                   reasoning_tokens={'low':100,'high':175}[body['reasoning_effort']]))
    row.update(overrides);return row


def test_pair_nonce_identity_usage_and_reported_spend_across_runs():
    requests=[];events=[]
    billing=dict(usd=.012,request_id='synthetic',generation_id=None)
    def reply(sent):
        body=json.loads(sent.content);requests.append(body)
        return httpx.Response(200,json=response(body,billing=billing,actual_model=c.MODEL,serving_revision='synthetic-v1'))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return [await c.run_pairs(client,'http://127.0.0.1:1','nonce',emit=events.append) for _ in range(2)]
    runs=asyncio.run(run())
    assert all(c.evaluate(rows)['passed'] for rows in runs)
    assert len(requests)==12 and len({row['session_id'] for row in requests})==12
    markers=[]
    prompts={row['messages'][0]['content'] for row in requests}
    assert len(prompts)==6
    for prompt in prompts:
        low,high=sorted((row for row in requests if row['messages'][0]['content']==prompt),
                          key=lambda row: {'low':0,'high':1}[row['reasoning_effort']])
        assert low['messages']==high['messages']
        assert [low['reasoning_effort'],high['reasoning_effort']]==['low','high']
        text=low['messages'][0]['content'];prefix,puzzle=text.split('\n',1)
        assert puzzle==c.PUZZLE and prefix.startswith('Run marker: ')
        UUID(prefix.removeprefix('Run marker: '));markers.append(prefix)
    assert len(set(markers))==6
    calls=[row for row in events if row['event']=='call']
    assert len(calls)==12 and all(row['actual_cost_usd']==.012 and row['billing']==billing for row in calls)
    assert all(row['serving_revision']=='synthetic-v1' and row['usage']['input_tokens']==1 for row in calls)


@pytest.mark.parametrize('case',['unknown','identity','model','length','usage','http','cancel'])
def test_invalid_or_failed_calls_stop_without_replacement(case):
    requests=[];events=[]
    def reply(sent):
        body=json.loads(sent.content);requests.append(body)
        if case=='cancel':raise asyncio.CancelledError()
        if case=='http':return httpx.Response(503,json={'code':'unavailable'})
        row=response(body)
        if case=='unknown':row['usage']['reasoning_tokens']=None
        if case=='identity':row['session_id']=str(uuid4())
        if case=='model':row['actual_model']='other'
        if case=='length':row['outcome']='length'
        if case=='usage':row['usage']['reasoning_tokens']=True
        return httpx.Response(200,json=row)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await c.run_pairs(client,'http://127.0.0.1:1','nonce',emit=events.append)
    with pytest.raises(asyncio.CancelledError if case=='cancel' else Exception):asyncio.run(run())
    assert len(requests)==6
    assert len([row for row in events if row['event']=='intent'])==6
    assert len([row for row in events if row['event']=='call'])==6
    assert events[-1]['status']==('interrupted' if case=='cancel' else 'failed' if case in ('identity','usage','http') else 'length' if case=='length' else 'completed')
    if case=='unknown':assert events[-1]['reasoning_tokens'] is None


def integration_module():
    path=Path(__file__).resolve().parents[1]/'integration/test_reasoning_contrast.py'
    spec=importlib.util.spec_from_file_location('controlled_reasoning_integration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_ordinary_integration_real_http_native_effort_scoring_and_cleanup(config):
    from api_agent.providers.llm import AvailableSelection
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.providers.cache_policy import configured_request
    native=[];closed=[];properties=[]
    class Provider:
        def describe(self):return (AvailableSelection(c.MODEL,features=('reasoning_effort',),reasoning_efforts=('low','high')),)
        def complete(self,request):
            request = configured_request(request, None, reasoning_effort='medium')
            async def invoke():
                def reply(sent):
                    body=json.loads(sent.content);native.append(body)
                    effort=body['reasoning']['effort']
                    data=dict(status='completed',model=c.MODEL,output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='arbitrary response text')])],
                              usage=dict(input_tokens=1,output_tokens=200,input_tokens_details=dict(cached_tokens=0),
                                         output_tokens_details=dict(reasoning_tokens={'low':100,'high':175}[effort])))
                    return httpx.Response(200,text='data: '+json.dumps(dict(type='response.completed',response=data))+'\n\n',headers={'content-type':'text/event-stream'})
                async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                    return await complete_text(client=client,base_url='https://api.openai.com/v1',access_token='synthetic-token',subscription=True,api_mode='responses',request=request)
            return asyncio.run(invoke())
        def close(self):closed.append(True)
    provider=Provider();module=integration_module()
    module.reasoning_ready.__wrapped__(SimpleNamespace(values=config,owner_root=Path.cwd()))
    module.test_reasoning_contrast(None,SimpleNamespace(getfixturevalue=lambda name:provider),lambda name,value:properties.append((name,value)))
    # The ordinary test's scoring assertion executes before any wire assertions.
    assert len(native)==6
    prompts = {row['input'][0]['content'] for row in native}
    for prompt in prompts:
        low, high = sorted((row for row in native if row['input'][0]['content']==prompt),
                             key=lambda row: {'low':0,'high':1}[row['reasoning']['effort']])
        assert [low['reasoning']['effort'],high['reasoning']['effort']]==['low','high']
        assert low['input'] == high['input']
        content = low['input'][0]['content']
        prefix, task = content.split('\n', 1)
        assert prefix.startswith('Run marker: ')
        UUID(prefix.removeprefix('Run marker: '))
        assert task == c.PUZZLE
        assert 'Define solve(edges, start, goal, red_count, min_blue, max_run)' in task
        assert 'Reply with only executable Python code' in task
        assert {key: value for key, value in low.items() if key != 'reasoning'} == {
            key: value for key, value in high.items() if key != 'reasoning'}
    assert len({row['input'][0]['content'].split('\n', 1)[0] for row in native}) == 3
    assert closed==[], 'HTTP consumer must not dispose the normal fixture owner'
    provider.close();assert closed==[True]
    events=json.loads(properties[0][1]);assert len([row for row in events if row['event']=='call'])==6


def test_original_fixture_partial_error_usage_survives_http_failure(tmp_path,monkeypatch):
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import AvailableSelection,Usage
    sink=tmp_path/'provider.jsonl';properties=[];closed=[];requests=[]
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(sink))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','controlled-reasoning (call)')
    class Owned(IntegrationLLM):
        async def _complete(self,request):
            requests.append(request)
            raise ProviderAccessError('unavailable','Controlled partial failure',usage=Usage(2,100,reasoning_tokens=43))
    class Provider:
        def describe(self):return (AvailableSelection(c.MODEL,features=('reasoning_effort',),reasoning_efforts=('low','high')),)
        def complete(self,request):
            owner=Owned({'LOG_CONTENT':False},tmp_path,password_manager_registry={},skip_unavailable=False)
            try:return owner.complete(request)
            finally:owner.close();closed.append(owner.closed)
    module=integration_module()
    with pytest.raises(httpx.HTTPStatusError):
        module.test_reasoning_contrast(None,SimpleNamespace(getfixturevalue=lambda name:Provider()),lambda name,value:properties.append((name,value)))
    rows=[json.loads(line) for line in sink.read_text().splitlines()]
    usage=[row['data'] for row in rows if row['kind']=='inference_usage']
    assert len(requests)==6 and closed==[True]*6
    assert usage[-1]['usage']['reasoning_tokens']==43 and usage[-1]['outcome']=='failed'
    assert Counter(row['reasoning_effort'] for row in usage if row['outcome']=='failed')=={'low':3,'high':3}
    events=json.loads(properties[0][1]);assert len([row for row in events if row['event']=='call'])==6


def test_remote_fixture_prerequisite_cannot_silently_skip_http_comparison(monkeypatch):
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.providers.contracts import SetupPrerequisiteError
    properties=[];requests=[]
    def unavailable():
        requests.append('describe')
        raise SetupPrerequisiteError('Controlled prerequisite unavailable')
    from api_agent.providers import setup_transport
    monkeypatch.setattr(setup_transport, 'SetupClient', lambda *a,**kw: SimpleNamespace(describe=unavailable,close=lambda:None))
    remote=RemoteIntegrationLLM({})
    module=integration_module()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            module.test_reasoning_contrast(None,SimpleNamespace(getfixturevalue=lambda name:remote),lambda name,value:properties.append((name,value)))
    finally:remote.close()
    assert requests==['describe']*6 and remote.loop.is_closed()
    assert len([row for row in json.loads(properties[0][1]) if row['event']=='call'])==6


def test_response_text_does_not_gate_reasoning_measurement():
    responses=iter(['raise RuntimeError("must never execute")', 'not Python', '',
                    'import os; os._exit(99)', 'while True: pass', 'unrelated'])
    events=[]
    def reply(sent):
        body=json.loads(sent.content)
        return httpx.Response(200,json=response(body,response_text=next(responses)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await c.run_pairs(client,'http://127.0.0.1:1','nonce',emit=events.append)
    records=asyncio.run(run())
    assert c.evaluate(records)==dict(passed=True,differences=[75,75,75])
    calls=[row for row in events if row['event']=='call']
    assert len(calls)==6 and all('correct' not in row for row in calls)


def test_valid_measurement_ignores_correctness_metadata():
    row=pair()['calls'][0]
    row['correct']=False
    assert c.valid_call(row)
    del row['correct']
    assert c.valid_call(row)


def test_medium_cannot_substitute_for_high_in_score():
    records=[pair() for _ in range(3)]
    records[1]['calls'][1]['effort']='medium'
    assert not c.evaluate(records)['passed']


def test_scope_requires_high_support_before_inference(config,monkeypatch):
    from api_agent.llm_providers.gpt import reasoning
    monkeypatch.setattr(reasoning,'reasoning_efforts',lambda *a,**kw:('low','medium'))
    with pytest.raises(ValueError,match='Low and high'):
        c.scope(config)
