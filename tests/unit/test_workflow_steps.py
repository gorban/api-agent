from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from api_agent.testing.workflow_steps import scope_steps, _identifier, _time
from api_agent.testing.recorded import run_recorded_integration
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult, RunnerEvent
from api_agent.testing.integration import IntegrationRunResult
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy


def trace():
    root, child = str(uuid4()), str(uuid4())
    start = 1791800000000
    outer = dict(event='scope_start',scope_id=root,parent_id=None,owner='GRAPH',kind='graph',started_ms=start,attempt=1,max_passes=1)
    inner = dict(event='scope_start',scope_id=child,parent_id=root,owner='first',kind='component',started_ms=start+1,attempt=1,max_passes=2,effective_timeout=60,declared_timeout=120,timeout_key='TEST_TIMEOUT')
    return [outer,inner,dict(event='attempt',scope_id=child,attempt=1),dict(event='guard',scope_id=child,accepted=False),
            dict(event='attempt',scope_id=child,attempt=2),dict(inner,event='scope_end',outcome='passed',stopped_ms=start+5),
            dict(outer,event='scope_end',outcome='passed',stopped_ms=start+6)]


def test_scope_steps_preserve_nested_intervals_explicit_outcomes_and_attempts():
    rows=scope_steps(trace())
    assert len(rows)==1 and rows[0]['name']=='graph GRAPH' and rows[0]['status']=='passed'
    child=rows[0]['steps'][0]
    assert child['name']=='component first' and child['stop']-child['start']==4
    assert {'name':'attempts_reached','value':'2'} in child['parameters']
    assert {'name':'guard_rejections','value':'1'} in child['parameters']
    assert {'name':'effective_timeout','value':'60'} in child['parameters']


@pytest.mark.parametrize('outcome,status',[('failed','failed'),('cancelled','broken')])
def test_scope_status_is_not_inferred_from_progress_stopping(outcome,status):
    events=trace();events[-1]['outcome']=outcome
    assert scope_steps(events)[0]['status']==status


@pytest.mark.parametrize('mutation',[
    lambda rows: rows.insert(0,'not an event'),
    lambda rows: rows[0].update(scope_id='bad'),
    lambda rows: rows[0].update(parent_id=rows[0]['scope_id']),
    lambda rows: rows[0].update(kind='unknown'),
    lambda rows: rows[0].update(owner='https://private.invalid'),
    lambda rows: rows[0].update(owner=1),
    lambda rows: rows[0].update(started_ms=True),
    lambda rows: rows.insert(1,deepcopy(rows[0])),
    lambda rows: rows[-1].update(started_ms=0),
    lambda rows: rows[-1].update(outcome='unknown'),
    lambda rows: rows[-1].update(stopped_ms=-1),
    lambda rows: rows.append(deepcopy(rows[-1])),
    lambda rows: rows.pop(),
    lambda rows: rows[1].update(started_ms=rows[0]['started_ms']-1),
])
def test_invalid_lifecycle_cannot_claim_success(mutation):
    events=trace();mutation(events)
    with pytest.raises(ValueError):scope_steps(events)


def test_nonworkflow_heartbeats_never_claim_steps():
    assert scope_steps([{'event':'node','node':'first'},{'event':'guard','scope_id':'absent','accepted':False}])==[]
    assert not _identifier(None) and not _identifier('invalid')
    assert not _time(float('inf'))


def test_unknown_external_test_parent_keeps_workflow_as_root():
    events=trace();parent=str(uuid4())
    events[0]['parent_id']=parent;events[-1]['parent_id']=parent
    assert len(scope_steps(events))==1


def test_accepted_guard_is_not_a_rejection():
    events=trace();events[3]['accepted']=True
    assert not any(p['name']=='guard_rejections' for p in scope_steps(events)[0]['steps'][0]['parameters'])


def test_consistent_child_interval_still_must_fit_parent():
    events=trace()
    events[1]['started_ms']-=10;events[5]['started_ms']-=10
    with pytest.raises(ValueError,match='outside its owner'):scope_steps(events)


def test_cyclic_ownership_cannot_silently_discard_completed_scopes():
    events=trace()
    events[0]['parent_id']=events[1]['scope_id']
    events[-1]['parent_id']=events[1]['scope_id']
    with pytest.raises(ValueError,match='cyclic'):scope_steps(events)


def test_recorded_integration_publishes_workflow_steps_with_content_disabled(tmp_path):
    context=SimpleNamespace(base_root=tmp_path/'base',owner_root=tmp_path/'base')
    test=TestIdentity('base::test.py::test_graph','base','test.py','test.py::test_graph')
    result=TestResult(test,'passed',.01)
    class Storage:
        def persist(self,directory,manifest): pass
    def runner(context,tests,**hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('workflow_evidence','base',test,text=json.dumps(dict(trial=1,events=trace(),output='secret prompt'))))
        hooks['on_result'](result)
        execution=IntegrationRunResult(0,(result,),'')
        hooks['after_run'](execution)
        return execution
    recorded=run_recorded_integration(context,[test],{'TUI_MODE':'integration','ALLURE_S3_BUCKET':'synthetic',
        'ALLURE_RESULTS_DIR':tmp_path/'results','LOG_CONTENT':False},accepted=True,storage=Storage(),runner=runner,
        evidence_boundary=EmissionBoundary(ContentPolicy(False)))
    assert recorded.persisted and recorded.exit_code==0
    row=json.loads(next(recorded.directory.glob('*-result.json')).read_text())
    assert row['steps'][0]['steps'][0]['name']=='component first'
    contents=''.join(p.read_text() for p in recorded.directory.iterdir())
    assert 'secret prompt' not in contents
    assert any('scope_start' in p.read_text() for p in recorded.directory.glob('*-attachment'))
