import asyncio
from pathlib import Path
import json
import pytest
from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.workflows.contrived import build
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.layout import load_layout, LayoutError
from api_agent.workflows.sampling import lower_probability, calibration, cover

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('route', ['skip', 'next_a', 'next_b'])
@pytest.mark.parametrize('retry_first,retry_second', [(False,False),(True,False),(False,True),(True,True)])
def test_compiled_langgraph_preserves_input_and_independent_pass_budgets(route, retry_first, retry_second):
    original = '  Verbatim input\nUnicode: café | [data]  '
    requests, events = [], []
    async def complete(request):
        requests.append(request)
        tail = json.loads(request.payload.messages[-1].content[0].text)
        component = 'first' if len([event for event in events if event.get('component')=='second']) == 0 else 'second'
        rejected = retry_first if component == 'first' else retry_second
        value = dict(original_input=original, route=route, ready=not rejected or tail['attempt']>1, final_output=original)
        return LLMResult(Message('assistant',(TextContent(json.dumps(value)),)))
    workflow = build(ROOT,llm=complete,selection='gpt-example',trial='synthetic',observe=events.append)
    values = asyncio.run(workflow.arun({'user_input':original}))
    assert values['first_draft']['original_input'] == original
    assert ('second_draft' in values) == (route != 'skip')
    assert sum(event['event']=='node' for event in events) == (3*(1+retry_first)+(3*(1+retry_second) if route!='skip' else 0))
    assert requests[0].payload.messages[1].content[0].text == original
    if retry_first:
        assert requests[0].payload.messages[:2] == requests[1].payload.messages[:2]
        retry = json.loads(requests[1].payload.messages[-1].content[0].text)
        assert retry['previous']['ready'] is False and retry['feedback']['reason'].startswith('Repair')
    assert 'flowchart TD' in workflow.diagram()


@pytest.mark.parametrize('response', ['not json', '{}'])
def test_contract_failure_is_not_a_guard_retry(response):
    calls=[]
    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant',(TextContent(response),)))
    with pytest.raises(WorkflowError):
        asyncio.run(build(ROOT,llm=complete,selection='gpt-example',trial='x').arun({'user_input':'user'}))
    assert len(calls)==1


def test_guard_exhaustion_is_failure_without_final_pass_relaxation():
    async def complete(request):
        return LLMResult(Message('assistant',(TextContent(json.dumps(dict(original_input='user',route='skip',ready=False,final_output='user'))),)))
    with pytest.raises(WorkflowError,match='exhausted 2 passes'):
        asyncio.run(build(ROOT,llm=complete,selection='gpt-example',trial='x').arun({'user_input':'user'}))


def test_exact_bound_and_node_coverage_cap():
    assert lower_probability(0,60,.05)==0
    assert lower_probability(60,60,.05)==pytest.approx(.05**(1/60))
    report=calibration([{'a','b'}]*40+[{'a'}]*20,{'a','b'})
    minimum=report['minimum_runs']
    assert 1<minimum<60
    assert report['max_runs']<60
    assert report['safety_margin']==4 and report['max_runs']==minimum+4
    assert report['minimum_missing_node_bound']==sum((1-p)**minimum for p in report['bounds'].values())
    assert report['minimum_missing_node_bound']<=.001
    assert sum((1-p)**(minimum-1) for p in report['bounds'].values())>.001
    assert report['missing_node_bound']==sum((1-p)**report['max_runs'] for p in report['bounds'].values())
    assert report['missing_node_bound']<report['minimum_missing_node_bound']


def test_coverage_stops_immediately_without_extra_graph_calls():
    trials=[]
    events=[]
    async def graph(trial):
        trials.append(trial)
        return {'output':trial}, {'a'} if trial==1 else {'b'}
    report,result=asyncio.run(cover(graph,{'a','b'},60,events.append))
    assert trials==[1,2] and report['stopping_reason']=='all_nodes_hit'
    assert result=={'output':2} and events[-1]['complete'] is True


def test_coverage_and_calibration_fail_rather_than_skip_or_retry():
    async def graph(trial): return {},{'a'}
    with pytest.raises(AssertionError,match='Incomplete'):
        asyncio.run(cover(graph,{'a','b'},2))
    with pytest.raises(ValueError,match='operating ceiling'):
        calibration([{'a'}]*60,{'a','b'})


@pytest.mark.parametrize('hits,trials,alpha',[(True,1,.05),(2,1,.05),(0,0,.05),(1,2,0),(1,2,1)])
def test_invalid_probability_inputs_rejected(hits,trials,alpha):
    with pytest.raises(ValueError): lower_probability(hits,trials,alpha)


from test_workflow_observations import recording_policy


@pytest.mark.parametrize('authority',[False,True])
def test_recorded_without_observation_factory_requires_resolved_content_authority(
        recording_policy,tmp_path,monkeypatch,authority):
    from types import SimpleNamespace
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.evidence import attachment
    resolved,allowed = recording_policy
    marker = 'recorded-private-sentinel'
    async def complete(request):
        return LLMResult(Message('assistant',(TextContent(json.dumps(dict(
            original_input=marker,route='skip',ready=True,final_output=marker))),)))
    events = tmp_path/'events.jsonl'
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents',lambda *a,**k:None)
    workflow = build(ROOT,llm=complete,selection='example',trial='synthetic',config={'LOG_CONTENT':True})
    workflow.services.update(emissions=EmissionBoundary(ContentPolicy(True)),
        content_policy=ContentPolicy.from_config(resolved) if authority else None,
        plain_io_fields={'first_draft':{'input':['user_input'],'output':['final_output']}})
    result = asyncio.run(workflow.arun({'user_input':marker}))
    assert result['first_draft']['final_output']==marker
    payload = next(json.loads(line)['data'] for line in events.read_text().splitlines()
        if json.loads(line)['kind']=='workflow_evidence')
    _,data,_ = attachment(SimpleNamespace(text=json.dumps(payload)),None)
    permitted = allowed and authority
    assert (marker in data.decode()) is permitted
    assert payload['content']==('permitted_scrubbed' if permitted else 'withheld')
    captures = [row for row in payload['events'] if row['event']=='capture']
    assert any(row.get('values') for row in captures) is permitted
