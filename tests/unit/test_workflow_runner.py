"""Real pytest workers forward workflow evidence only for selected identities."""
import json
import pytest
from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from test_integration_runner import fixture_repo, context


@pytest.mark.parametrize('jobs',[0,2])
def test_real_workers_forward_selected_workflow_evidence(tmp_path,jobs):
    base=fixture_repo(tmp_path/'base','''
import json
import os
import pytest
from api_agent.workflows.evidence import publish

@pytest.mark.parametrize('trial',[0,1])
def test_trace(trial):
    publish({'trial':trial,'events':[{'event':'guard','component':'first','attempt':1,'accepted':False}]})
    with open(os.environ['API_AGENT_INTEGRATION_EVENTS'],'a',encoding='utf-8') as stream:
        stream.write(json.dumps({'kind':'workflow_evidence','node':'tests/integration/test_sample.py::test_unselected','data':{'trial':999}})+'\\n')
''')
    ctx=context(base)
    selected=collect_tests(ctx,'integration')
    events=[]
    result=run_integration_tests(ctx,selected,accepted=True,parallel_jobs=jobs,on_event=events.append)
    traces=[event for event in events if event.kind=='workflow_evidence']
    assert result.exit_code==0
    assert [row.status for row in result.tests]==['passed','passed']
    assert len(traces)==2
    assert {event.identity for event in traces}==set(selected)
    assert {event.repo for event in traces}=={'base'}
    assert {event.identity.node_id:json.loads(event.text) for event in traces}=={
        'tests/integration/test_sample.py::test_trace[0]':{'trial':0,'events':[{'event':'guard','component':'first','attempt':1,'accepted':False}]},
        'tests/integration/test_sample.py::test_trace[1]':{'trial':1,'events':[{'event':'guard','component':'first','attempt':1,'accepted':False}]},
    }
