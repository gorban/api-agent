import asyncio
import json
from types import SimpleNamespace
import pytest
from api_agent.workflows.contrived import produce, strict_json, validate_format, guard
from api_agent.workflows.execution import WorkflowError


@pytest.mark.parametrize('value', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{broken', '{"x":1e999}'])
def test_strict_json_rejects_invalid_contract(value):
    with pytest.raises(ValueError):
        strict_json(value)


def test_strict_json_keeps_unicode():
    assert strict_json('{"x":"λ","y":0.25}') == {'x': 'λ', 'y':0.25}


def test_format_preserves_verbatim_input():
    assert validate_format({'draft': {'original_input':' λ\n','final_output':' λ\n'}, 'user_input':' λ\n'}, None) == {'valid':True}


@pytest.mark.parametrize('draft', [{'original_input':'changed','final_output':'original'}, {'original_input':'original','final_output':''}])
def test_format_defect_is_fatal(draft):
    with pytest.raises(WorkflowError):
        validate_format({'draft':draft,'user_input':'original'}, None)


@pytest.mark.parametrize('ready', [True,False])
def test_guard_does_not_relax_final_pass(ready):
    assert guard({'draft':{'ready':ready}}, SimpleNamespace(attempt=2))['accepted'] is ready


@pytest.mark.parametrize('output,outcome,previous,error', [('[]','completed',None,'non-object'), ('{bad','completed',None,'invalid JSON'), ('{"route":"skip"}','completed',{'route':'next_a'},'routing'), ('{}','failed',None,'completed')])
def test_producer_rejects_contract_defects(output,outcome,previous,error):
    async def llm(request):
        return SimpleNamespace(outcome=outcome,payload=SimpleNamespace(content=(SimpleNamespace(text=output),)))
    context=SimpleNamespace(attempt=2,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{}})
    with pytest.raises(WorkflowError,match=error):
        asyncio.run(produce({'previous':previous,'feedback':None,'upstream':None,'user_input':'original'},context))


def test_producer_sends_input_and_upstream_separately():
    requests=[]
    async def llm(request):
        requests.append(request)
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text='{"route":"skip"}'),)))
    context=SimpleNamespace(attempt=2,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{}})
    result=asyncio.run(produce({'previous':{'route':'skip'},'feedback':{'reason':'repair'},'upstream':{'final_output':'accepted'},'user_input':' λ\n'},context))
    assert result == {'route':'skip'}
    assert requests[0].payload.messages[1].content[0].text == ' λ\n'
    metadata = json.loads(requests[0].payload.messages[2].content[0].text)
    assert metadata['upstream'] == {'final_output':'accepted'}
    assert list(metadata) == ['upstream','previous','feedback','attempt','max_passes','trial']


def test_build_bundled_workflow():
    from pathlib import Path
    from api_agent.workflows.contrived import build
    workflow=build(Path(__file__).resolve().parents[2],llm=None,selection='synthetic',trial='id')
    assert len(workflow.layout.steps)==8


def test_build_installed_resource_fallback(monkeypatch,tmp_path):
    from pathlib import Path
    from api_agent.workflows import contrived
    monkeypatch.setattr(contrived,'load_layout',lambda path, **kwargs: 'layout')
    monkeypatch.setattr(contrived,'Workflow',lambda layout,*args,**kwargs: layout)
    monkeypatch.setattr('importlib.resources.files',lambda package: Path(__file__).resolve().parents[2])
    assert contrived.build(tmp_path,llm=None,selection='synthetic',trial='id') == 'layout'


@pytest.mark.parametrize('route,calls', [('skip',2),('next_a',4)])
def test_complete_fixture_repairs_without_losing_upstream(route,calls):
    from pathlib import Path
    from api_agent.workflows.contrived import build
    requests, events = [], []
    async def llm(request):
        metadata=json.loads(request.payload.messages[-1].content[0].text)
        requests.append(metadata)
        draft={'route':route,'original_input':request.payload.messages[1].content[0].text,'ready':metadata['attempt'] > 1,'final_output':request.payload.messages[1].content[0].text}
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text=json.dumps(draft)),)))
    workflow=build(Path(__file__).resolve().parents[2],llm=llm,selection='synthetic',trial='id',observe=events.append)
    values=asyncio.run(workflow.arun({'user_input':' λ\n'}))
    assert values['first_draft']['ready'] is True
    assert requests[1]['previous']['route'] == route
    assert [event['accepted'] for event in events if event['event']=='guard'] == [False,True] * (calls // 2)
    assert len([event for event in events if event['event']=='llm']) == len(requests)
    assert len(requests) == calls
    assert [request['upstream'] for request in requests[2:]] == [values['first_draft']] * (calls - 2)
    assert ('second_draft' in values) == (calls == 4)


@pytest.mark.parametrize('argv,environ,expected,source', [
    ([],{},120.0,'shared_default'),
    ([],{'WORKFLOW_LLM_TIMEOUT_SECONDS':'12.5'},12.5,'environment'),
    (['--workflow-llm-timeout-seconds=7.5'],{'WORKFLOW_LLM_TIMEOUT_SECONDS':'12.5'},7.5,'cli'),
])
def test_typed_timeout_configuration_defaults_env_cli(argv,environ,expected,source):
    from pathlib import Path
    from api_agent.config import load_declarations,resolve_config
    schema=load_declarations(Path(__file__).resolve().parents[2]/'demo/cfg/config.toml')
    result=resolve_config(schema,argv=argv,environ=environ,anchors={'owner_root':Path('/synthetic')})
    result.raise_for_errors()
    assert result.values['WORKFLOW_LLM_TIMEOUT_SECONDS']==expected
    assert result.fields['WORKFLOW_LLM_TIMEOUT_SECONDS'].source==source
    assert all(not schema.fields[key].acknowledge_default for key in schema.fields if key.endswith('TIMEOUT_SECONDS'))
    assert all(key not in result.values for key in ('WORKFLOW_GRAPH_TIMEOUT_SECONDS','WORKFLOW_COMPONENT_TIMEOUT_SECONDS','WORKFLOW_NODE_TIMEOUT_SECONDS'))


def test_typed_timeout_bindings_and_default_disabled_limits():
    from pathlib import Path
    from api_agent.workflows.contrived import build
    workflow=build(Path(__file__).resolve().parents[2],llm=None,selection='synthetic',trial='id')
    assert set(workflow.timeouts)=={'GRAPH','first','second','first_draft','first_format','first_guard','second_draft','second_format','second_guard'}
    assert all(value is None for value in workflow.timeouts.values())
    assert workflow.services['llm_timeout']==120


def test_configured_llm_scope_is_clipped_to_parent(monkeypatch):
    from api_agent.providers import progress
    seen=[]
    async def llm(request):
        seen.append(progress._scope.get().timeout)
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text='{"route":"skip"}'),)))
    context=SimpleNamespace(attempt=1,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{},'llm_timeout':7.5})
    async def execute():
        async with progress.component_timeout('parent',2):
            return await produce({'previous':None,'feedback':None,'upstream':None,'user_input':'input'},context)
    assert asyncio.run(execute())=={'route':'skip'}
    assert 0 < seen[0] <= 2


def test_swallowed_llm_child_timeout_never_returns_success(monkeypatch):
    from api_agent.providers import progress
    clock=[0.0]
    monkeypatch.setattr(progress,'_clock',lambda:clock[0])
    async def llm(request):
        clock[0]=2.0
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text='{"route":"skip"}'),)))
    context=SimpleNamespace(attempt=1,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{},'llm_timeout':1})
    with pytest.raises(TimeoutError,match='LLM call deadline expired'):
        asyncio.run(produce({'previous':None,'feedback':None,'upstream':None,'user_input':'input'},context))


@pytest.mark.parametrize('timeout',[True,0,-1,float('nan'),float('inf'),'2'])
def test_llm_timeout_rejected_before_inference(timeout):
    calls=[]
    async def llm(request):
        calls.append(request)
    context=SimpleNamespace(attempt=1,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{},'llm_timeout':timeout})
    with pytest.raises(ValueError,match='component timeout'):
        asyncio.run(produce({'previous':None,'feedback':None,'upstream':None,'user_input':'input'},context))
    assert calls==[]


def test_configured_typed_limits_clip_all_workflow_scopes():
    from pathlib import Path
    from api_agent.workflows.contrived import build
    events=[]
    async def llm(request):
        text=request.payload.messages[1].content[0].text
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text=json.dumps({'route':'skip','original_input':text,'ready':True,'final_output':text})),)))
    settings={'WORKFLOW_LLM_TIMEOUT_SECONDS':7.5,'WORKFLOW_NODE_TIMEOUT_SECONDS':8.0,'WORKFLOW_COMPONENT_TIMEOUT_SECONDS':9.0,'WORKFLOW_GRAPH_TIMEOUT_SECONDS':2.0}
    workflow=build(Path(__file__).resolve().parents[2],llm=llm,selection='synthetic',trial='id',config=settings,observe=events.append)
    asyncio.run(workflow.arun({'user_input':'synthetic'}))
    starts=[event for event in events if event['event']=='scope_start']
    assert starts
    assert all(0 < event['effective_timeout'] <= 2 for event in starts)
    assert workflow.services['llm_timeout']==7.5


def test_native_cancelled_llm_cannot_swallow_timeout_and_return_success():
    cancellations=[]
    async def llm(request):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancellations.append(True)
        return SimpleNamespace(outcome='completed',payload=SimpleNamespace(content=(SimpleNamespace(text='{"route":"skip"}'),)))
    context=SimpleNamespace(attempt=1,max_passes=2,prompts=('prompt',),services={'llm':llm,'selection':'synthetic','trial':'id','output_schema':{},'llm_timeout':.001})
    with pytest.raises(TimeoutError,match='LLM call deadline expired'):
        asyncio.run(produce({'previous':None,'feedback':None,'upstream':None,'user_input':'input'},context))
    assert cancellations==[True]
