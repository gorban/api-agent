"""Completion accounting exposes identifiers and token totals, never prompt/output text."""
import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock
import pytest
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.inference_usage import publish,capture
from test_inference_shared_join import REQUEST,RESULT


def event(row):
    return SimpleNamespace(text=json.dumps(row),identity=SimpleNamespace(id='test-id'))


def row():
    return {'call_id':str(uuid4()),'selection':'gpt-example','usage':None}


def test_publish_inert_without_operation_event_file(monkeypatch):
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS',raising=False)
    assert publish(REQUEST) is None
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS','does-not-exist')
    monkeypatch.delenv('PYTEST_CURRENT_TEST',raising=False)
    assert publish(REQUEST) is None


def test_prompt_and_token_events_are_content_free(tmp_path,monkeypatch):
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','tests/test_request.py::test_one (call)')
    identifier=publish(REQUEST)
    assert publish(REQUEST,RESULT,call_id=identifier)==identifier
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    assert [r['node'] for r in rows]==['tests/test_request.py::test_one']*2
    assert rows[0]['data']['call_id']==rows[1]['data']['call_id']
    tracker=SuiteUsage('run')
    for data in rows:
        capture(event(data['data']),tracker)
    assert tracker.report()['prompt_count']==1
    assert tracker.rows()[0]['total_tokens']==3 and tracker.rows()[0]['llm_calls']==1
    assert 'Ok' not in path.read_text()


@pytest.mark.parametrize('data',[None,[],{},row()|{'extra':1},row()|{'call_id':None},row()|{'call_id':'invalid'},row()|{'call_id':str(uuid4()).upper()},
    row()|{'selection':None},row()|{'selection':''},row()|{'selection':'\n'},row()|{'selection':'x'*201},row()|{'usage':{}}])
def test_malformed_usage_events_rejected(data):
    with pytest.raises(ValueError):
        capture(event(data),SuiteUsage('run'))


def test_multiple_turns_unknown_usage_and_failed_attempt_counts():
    tracker=SuiteUsage('run')
    first,second,failed=(row() for _ in range(3))
    for value in (first,second,failed):
        capture(event(value),tracker)
    unknown={key:None for key in ('input_tokens','output_tokens','cached_input_tokens','reasoning_tokens')}
    capture(event(first|{'usage':unknown}),tracker)
    capture(event(second|{'usage':dict(input_tokens=3,output_tokens=2,cached_input_tokens=1,reasoning_tokens=1)}),tracker)
    assert tracker.report()['prompt_count']==3
    assert tracker.rows()[0]['llm_calls']==2
    assert tracker.rows()[0]['input_tokens'] is None and tracker.rows()[0]['total_tokens'] is None
