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
    return dict(LLM_PROVIDER=str(factory)+':factory',LLM_REASONING_TEST_TOKEN_REPORTING=True)


def test_scope_uses_exact_contributed_choice(config):
    assert c.scope(config | {'LLM_REASONING_TEST_MODEL':'synthetic.model'}) == 'synthetic.model'
    with pytest.raises(ValueError, match='LLM_REASONING_TEST_MODEL'):
        c.scope(config)


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


@pytest.mark.parametrize('limit, advertised', [(None,None), (4096,None), (4096,4096), (16000,16000), (16384,16384)])
def test_ordinary_integration_real_http_native_effort_scoring_and_cleanup(config, limit, advertised):
    from api_agent.providers.llm import AvailableSelection
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.providers.cache_policy import configured_request
    native=[];closed=[];properties=[]
    class Provider:
        def describe(self):return (AvailableSelection(c.MODEL,family='synthetic',version='1',features=('reasoning_effort','max_output_tokens'),reasoning_efforts=('low','high'),max_output_tokens=advertised),)
        def complete(self,request):
            request = configured_request(request, None, reasoning_effort='medium')
            async def invoke():
                def reply(sent):
                    body=json.loads(sent.content);native.append(body)
                    effort=body['reasoning']['effort']
                    data=dict(status='completed',model=c.MODEL,output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='arbitrary response text')])],
                              usage=dict(input_tokens=1,output_tokens=200,input_tokens_details=dict(cached_tokens=0),
                                         output_tokens_details=dict(reasoning_tokens={'low':100,'high':175}[effort])))
                    if limit is not None:
                        return httpx.Response(200,json=data)
                    return httpx.Response(200,text='data: '+json.dumps(dict(type='response.completed',response=data))+'\n\n',headers={'content-type':'text/event-stream'})
                async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                    return await complete_text(client=client,base_url='https://api.openai.com/v1',access_token='synthetic-token',subscription=limit is None,api_mode='responses',request=request)
            return asyncio.run(invoke())
        def close(self):closed.append(True)
    provider=Provider();module=integration_module()
    module.reasoning_ready.__wrapped__(SimpleNamespace(values=config | {'LLM_REASONING_TEST_MODEL':c.MODEL, 'LLM_REASONING_TEST_LOW_EFFORT':'low', 'LLM_REASONING_TEST_HIGH_EFFORT':'high'},owner_root=Path.cwd()))
    module.test_reasoning_contrast((c.MODEL, ('low','high'), limit, 150),SimpleNamespace(getfixturevalue=lambda name:provider),lambda name,value:properties.append((name,value)))
    # The ordinary test's scoring assertion executes before any wire assertions.
    assert len(native)==6
    assert all(row.get('max_output_tokens') == limit for row in native)
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
        def describe(self):return (AvailableSelection(c.MODEL,family='synthetic',version='1',features=('reasoning_effort',),reasoning_efforts=('low','high')),)
        def complete(self,request):
            owner=Owned({'LOG_CONTENT':False},tmp_path,password_manager_registry={},skip_unavailable=False)
            try:return owner.complete(request)
            finally:owner.close();closed.append(owner.closed)
    module=integration_module()
    with pytest.raises(httpx.HTTPStatusError):
        module.test_reasoning_contrast((c.MODEL, ('low','high'), None, 150),SimpleNamespace(getfixturevalue=lambda name:Provider()),lambda name,value:properties.append((name,value)))
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
        with pytest.raises(AssertionError, match="catalog setup"):
            module.test_reasoning_contrast((c.MODEL, ('low','high'), None, 150),SimpleNamespace(getfixturevalue=lambda name:remote),lambda name,value:properties.append((name,value)))
    finally:remote.close()
    assert requests==['describe'] and remote.loop.is_closed()
    assert json.loads(properties[0][1]) == []


def test_nonempty_response_content_does_not_gate_reasoning_measurement():
    responses=iter(['raise RuntimeError("must never execute")', 'not Python', 'arbitrary content',
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


def test_catalog_requires_high_support_before_inference():
    from api_agent.testing.model_choices import catalog_choice
    from api_agent.providers.llm import AvailableSelection
    with pytest.raises(ValueError, match='effort'):
        catalog_choice([AvailableSelection(c.MODEL, family='synthetic', version='1', features=('reasoning_effort',),
            reasoning_efforts=('low','medium'))], c.MODEL, ('low','high'))


@pytest.mark.parametrize('efforts', [('', 'high'), ('high','low'), ('low','low'), (None,'high')])
def test_reasoning_configuration_rejects_invalid_or_reversed_controls(efforts):
    values = {'LLM_REASONING_TEST_MODEL':'synthetic', 'LLM_REASONING_TEST_TOKEN_REPORTING':True,
        'LLM_REASONING_TEST_LOW_EFFORT':efforts[0], 'LLM_REASONING_TEST_HIGH_EFFORT':efforts[1]}
    with pytest.raises(ValueError, match='two distinct'):
        integration_module().reasoning_ready.__wrapped__(SimpleNamespace(values=values, owner_root=None))


def test_non_gpt_off_high_pair_uses_exact_configured_model():
    calls=[]
    def reply(sent):
        body=json.loads(sent.content); calls.append(body)
        data=response(body, actual_model='vendor.model') if body['reasoning_effort'] != 'off' else dict(
            session_id=body['session_id'], response_text='arbitrary', outcome='completed',
            actual_model='vendor.model', usage=dict(input_tokens=1, output_tokens=200, cached_input_tokens=0, reasoning_tokens=0))
        return httpx.Response(200,json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await c.run_pairs(client,'http://127.0.0.1:1','nonce', model='vendor.model', efforts=('off','high'), max_output_tokens=4096)
    records=asyncio.run(run())
    assert c.evaluate(records, model='vendor.model', efforts=('off','high'))['passed']
    assert len(calls)==6 and {call['model'] for call in calls} == {'vendor.model'}
    assert Counter(call['reasoning_effort'] for call in calls) == {'off':3,'high':3}
    assert all(call['max_output_tokens'] == 4096 for call in calls)
    assert not c.evaluate(records)['passed']


@pytest.mark.parametrize('value', [None, False, 'true', 1])
def test_scope_rejects_unqualified_counter_reporting(config, value):
    with pytest.raises(ValueError, match='reasoning-token reporting'):
        c.scope(config | {'LLM_REASONING_TEST_MODEL':'synthetic', 'LLM_REASONING_TEST_TOKEN_REPORTING':value})


@pytest.mark.parametrize('limit', [0, -1, True, '', '4096', 16385])
def test_reasoning_configuration_rejects_invalid_output_cap(config, limit):
    values = config | {'LLM_REASONING_TEST_MODEL':c.MODEL, 'LLM_REASONING_TEST_LOW_EFFORT':'low',
        'LLM_REASONING_TEST_HIGH_EFFORT':'high', 'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS':limit}
    with pytest.raises(ValueError, match='positive integer'):
        integration_module().reasoning_ready.__wrapped__(SimpleNamespace(values=values, owner_root=None))


@pytest.mark.parametrize('maximum', [4096, 4097, 16000, 16384])
def test_reasoning_configuration_preserves_explicit_cap(config, maximum):
    values = config | {'LLM_REASONING_TEST_MODEL':c.MODEL, 'LLM_REASONING_TEST_LOW_EFFORT':'low',
        'LLM_REASONING_TEST_HIGH_EFFORT':'high', 'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS':maximum}
    assert integration_module().reasoning_ready.__wrapped__(SimpleNamespace(values=values, owner_root=None)) == (c.MODEL, ('low','high'), maximum, 150)


@pytest.mark.parametrize('features, message', [(('max_output_tokens',), 'advertised model limit'), ((), 'advertised model support')])
def test_reasoning_cap_rejects_catalog_overflow_before_inference(features, message):
    from api_agent.providers.llm import AvailableSelection
    provider = SimpleNamespace(describe=lambda:(AvailableSelection(c.MODEL, features=('reasoning_effort',)+features, max_output_tokens=2048,
        reasoning_efforts=('low','high')),))
    properties=[]
    with pytest.raises(ValueError, match=message):
        integration_module().test_reasoning_contrast((c.MODEL, ('low','high'), 4096, 150),
            SimpleNamespace(getfixturevalue=lambda name:provider), lambda name,value:properties.append((name,value)))
    assert json.loads(properties[0][1]) == []


@pytest.mark.parametrize('count', [None, True, -1, 4096, 4097, '4095', 4095.0])
def test_completed_bounded_measurement_rejects_cap_or_invalid_output_count(count):
    row = pair()['calls'][0] | {'max_output_tokens':4096, 'response_text_present':True, 'usage':{'output_tokens':count}}
    assert not c.valid_call(row)
    assert row['status'] == 'completed'


@pytest.mark.parametrize('usage', [None, [], {}, {'output_tokens':None}])
def test_bounded_measurement_requires_reported_output_usage(usage):
    row = pair()['calls'][0] | {'max_output_tokens':4096, 'response_text_present':True, 'usage':usage}
    assert not c.valid_call(row)


@pytest.mark.parametrize('maximum', [False, 0, -1, '4096', 4096.0])
def test_bounded_measurement_requires_a_positive_integer_cap(maximum):
    row = pair()['calls'][0] | {'max_output_tokens':maximum, 'response_text_present':True, 'usage':{'output_tokens':1}}
    assert not c.valid_call(row)


@pytest.mark.parametrize('count', [0, 4095])
def test_bounded_measurement_accepts_only_counts_strictly_below_cap(count):
    row = pair()['calls'][0] | {'max_output_tokens':4096, 'response_text_present':True, 'usage':{'output_tokens':count}}
    assert c.valid_call(row)


def test_cap_reaching_completed_sample_cannot_pass_three_pair_threshold():
    records = [pair() for _ in range(3)]
    records[1]['calls'][1].update(max_output_tokens=4096, response_text_present=True, usage={'output_tokens':4096})
    assert not c.evaluate(records)['passed']
    assert records[1]['calls'][1]['status'] == 'completed'


def test_run_pairs_rejects_cap_reaching_completed_without_replacements():
    calls=[];events=[]
    def reply(sent):
        body=json.loads(sent.content);calls.append(body)
        data=response(body)
        data['usage']['output_tokens'] = 4096
        return httpx.Response(200,json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await c.run_pairs(client,'http://127.0.0.1:1','nonce',max_output_tokens=4096,emit=events.append)
    with pytest.raises(ValueError, match='invalid measurement'):
        asyncio.run(run())
    observed = [row for row in events if row['event'] == 'call']
    assert len(calls) == len(observed) == 6
    assert all(row['status'] == 'completed' and row['usage']['output_tokens'] == 4096 for row in observed)



@pytest.mark.parametrize('flag', [False, None, 1, 'true'])
@pytest.mark.parametrize('maximum', [None, 4096])
def test_explicit_nonboolean_or_false_text_presence_cannot_qualify_measurement(flag, maximum):
    row = pair()['calls'][0] | {'max_output_tokens':maximum, 'response_text_present':flag,
        'usage':{'output_tokens':200}}
    assert not c.valid_call(row)


def test_bounded_measurement_requires_text_presence_flag_but_legacy_unbounded_can_omit_it():
    row = pair()['calls'][0] | {'usage':{'output_tokens':200}}
    assert c.valid_call(row)
    row['max_output_tokens'] = 4096
    assert not c.valid_call(row)
    row['response_text_present'] = True
    assert c.valid_call(row)


@pytest.mark.parametrize('text', ['', ' \n\t'])
@pytest.mark.parametrize('maximum', [None, 4096])
def test_run_pairs_rejects_empty_or_whitespace_text_without_exporting_content(text, maximum):
    events=[];calls=[]
    def reply(sent):
        body=json.loads(sent.content);calls.append(body)
        return httpx.Response(200,json=response(body,response_text=text))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await c.run_pairs(client,'http://127.0.0.1:1','nonce',max_output_tokens=maximum,emit=events.append)
    with pytest.raises(ValueError, match='invalid measurement'):
        asyncio.run(run())
    observed = [row for row in events if row['event'] == 'call']
    assert len(calls) == len(observed) == 6
    assert all(row['status'] == 'completed' and row['response_text_present'] is False for row in observed)
    assert all('text' not in row and 'response_text' not in row for row in events)


@pytest.mark.parametrize('model', ['openai.gpt-oss-20b', 'openai.gpt-oss-120b'])
@pytest.mark.parametrize('maximum', [16001, 16384])
def test_real_bedrock_catalog_cap_rejects_before_comparison_inference(maximum, model):
    from api_agent.llm_providers.bedrock.catalog import selection
    provider=SimpleNamespace(describe=lambda:(selection(model,'responses'),))
    with pytest.raises(ValueError, match='advertised model limit'):
        integration_module().test_reasoning_contrast((model,('low','high'),maximum,150),
            SimpleNamespace(getfixturevalue=lambda name:provider),lambda name,value:None)


@pytest.mark.parametrize('model', ['openai.gpt-oss-20b', 'openai.gpt-oss-120b'])
def test_real_bedrock_catalog_and_native_encoder_preserve_16000_through_application_http(monkeypatch, model):
    from api_agent.llm_providers.bedrock.catalog import selection
    from api_agent.llm_providers.bedrock.mantle import encode
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    native=[];properties=[]
    from api_agent.testing import studio_http
    backend_type=studio_http.StudioBackend
    budgets=[]
    def backend(**kwargs):
        budgets.append(kwargs['timeout'])
        return backend_type(**kwargs)
    monkeypatch.setattr(studio_http,'StudioBackend',backend)
    module=integration_module()
    original_run=module.run_pairs
    async def matched_run(client,*args,**kwargs):
        assert client.timeout.read == kwargs['timeout_seconds'] == 300
        return await original_run(client,*args,**kwargs)
    module.run_pairs=matched_run
    def complete(request):
        body=encode(request,'responses');native.append(body)
        count={'low':100,'medium':175}[body['reasoning']['effort']]
        return LLMResult(Message('assistant',(TextContent('synthetic reply'),)),
            Usage(input_tokens=1,output_tokens=200,reasoning_tokens=count),actual_model=model)
    provider=SimpleNamespace(describe=lambda:(selection(model,'responses'),), complete=complete)
    module.test_reasoning_contrast((model,('low','medium'),16000,300),
        SimpleNamespace(getfixturevalue=lambda name:provider),lambda name,value:properties.append((name,value)))
    assert budgets == [300]
    assert len(native)==6
    assert all(body['max_output_tokens']==16000 and body['model']==model and 'max_tokens' not in body for body in native)
    assert Counter(body['reasoning']['effort'] for body in native)=={'low':3,'medium':3}
    rows=json.loads(properties[0][1])
    assert all(row['response_text_present'] for row in rows if row['event']=='call')


@pytest.mark.parametrize('value', [True, False, None, '300', 0, -1, float('nan'), float('inf'), float('-inf')])
def test_reasoning_timeout_rejected_before_provider_access(config, value):
    values = config | {'LLM_REASONING_TEST_TIMEOUT_SECONDS': value}
    with pytest.raises(ValueError, match='positive finite'):
        integration_module().reasoning_ready.__wrapped__(SimpleNamespace(values=values, owner_root=None))


@pytest.mark.parametrize('value', [150, 300, .05])
def test_reasoning_timeout_preserves_explicit_budget(value):
    assert c.reasoning_timeout({'LLM_REASONING_TEST_TIMEOUT_SECONDS':value}) == value
    assert c.reasoning_timeout({}) == 150


def test_run_pairs_rejects_invalid_budget_without_http():
    async def run():
        with pytest.raises(ValueError, match='positive finite'):
            await c.run_pairs(None, 'unused', 'unused', timeout_seconds=True)
    asyncio.run(run())


def test_reasoning_shorter_parent_deadline_is_preserved():
    from api_agent.providers.progress import component_timeout
    requests=[]
    async def reply(sent):
        requests.append(sent)
        await asyncio.sleep(1)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(TimeoutError):
                async with component_timeout('Synthetic parent', .03):
                    await c.run_pairs(client,'http://127.0.0.1:1','nonce',timeout_seconds=300)
    asyncio.run(run())
    assert len(requests) == 6
