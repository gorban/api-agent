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


def test_cost_quote_published_with_tokens_is_joined_to_same_test(tmp_path,monkeypatch):
    path=tmp_path/'events';monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test_request (call)')
    call_id=publish(REQUEST)
    publish(REQUEST,RESULT,call_id=call_id,quote={'list_cost_usd':.25,'payment':None})
    tracker=SuiteUsage('run')
    for line in path.read_text().splitlines():capture(event(json.loads(line)['data']),tracker)
    report=tracker.report()
    assert report['test_costs']['test-id']['estimated_cost_usd']==.25
    assert report['cost_per_prompt_usd']==.25 and report['cost_basis']=='list_price'
    from api_agent.telemetry.usage_output import final_usage_text
    assert 'Cost basis: list_price' in final_usage_text(report)


@pytest.mark.parametrize('reported', [True, False])
def test_failed_provider_call_keeps_reported_tokens_and_terminal_status(tmp_path, monkeypatch, reported):
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import Usage
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_request (call)')
    identifier = publish(REQUEST)
    error = ProviderAccessError('authorization', 'Safe rejection', status=403,
        usage=Usage(11, 5, 2, 1) if reported else None)
    publish(REQUEST, call_id=identifier, failed=True, error=error)
    tracker = SuiteUsage('run')
    events = [json.loads(line)['data'] for line in path.read_text().splitlines()]
    for data in events:
        capture(event(data), tracker)
    capture(event(events[-1]), tracker)  # Re-delivery cannot double count.
    report = tracker.report()
    assert events[-1]['outcome'] == 'failed' and events[-1]['finished_at']
    assert tracker.inference_calls[identifier]['outcome'] == 'failed'
    assert report['prompt_count'] == 1
    if reported:
        assert tracker.rows()[0]['total_tokens'] == 16
        assert tracker.rows()[0]['cache_read_tokens'] == 2
        assert tracker.rows()[0]['reasoning_tokens'] == 1
        assert report['unmeasured_calls'] == {}
    else:
        assert tracker.rows() == [] and report['unmeasured_calls'] == {'test-id':1}
    assert 'Safe rejection' not in path.read_text() and 'Ok' not in path.read_text()


def test_central_terminal_receipt_passes_actual_model_and_effective_effort_to_visit(monkeypatch, tmp_path):
    from dataclasses import replace
    from api_agent.providers.llm import GenerationOptions
    from api_agent.workflows.attribution import bind_workflow_visit
    request = replace(REQUEST, payload=replace(REQUEST.payload,
        generation=GenerationOptions(reasoning_effort='high')))
    result = replace(RESULT, actual_model='actual-catalog-model')
    owner = Mock()
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_request (call)')
    with bind_workflow_visit(owner, 'visit', 'attempt'):
        identifier = publish(request)
        publish(request, result, call_id=identifier)
    owner.record_call.assert_called_once_with(call_id=identifier, visit_id='visit',
        attempt_id='attempt', usage={'input_tokens': 2, 'output_tokens': 1,
            'cached_input_tokens': None, 'reasoning_tokens': None},
        selection=request.selected_llm, outcome=result.outcome,
        actual_model='actual-catalog-model', reasoning_effort='high')


def test_explicit_conversation_evidence_retains_turn_and_rejects_identity_changes(tmp_path, monkeypatch):
    from api_agent.workflows.run_context import conversation, turn
    conversation_id, turn_id = str(uuid4()), str(uuid4())
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_request (call)')
    with conversation(conversation_id), turn(turn_id):
        identifier = publish(REQUEST)
        publish(REQUEST, RESULT, call_id=identifier)
    rows = [json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert all(data['conversation_id'] == conversation_id and data['turn_id'] == turn_id for data in rows)
    tracker = SuiteUsage('run')
    capture(event(rows[0]), tracker)
    with pytest.raises(ValueError, match='Conflicting inference identity'):
        capture(event(rows[0] | {'conversation_id': str(uuid4())}), tracker)
    with pytest.raises(ValueError, match='Conflicting inference identity'):
        capture(event({k: v for k, v in rows[0].items() if k != 'conversation_id'}), tracker)
    capture(event(rows[1]), tracker)
    capture(event(rows[1]), tracker)
    assert tracker.rows()[0]['llm_calls'] == 1


@pytest.mark.parametrize('conversation_id', [None, 'invalid', 1])
def test_optional_conversation_identity_is_strict(conversation_id):
    with pytest.raises(ValueError):
        capture(event(row() | {'conversation_id': conversation_id}), SuiteUsage('run'))


def test_trial_evidence_has_no_conversation_identity(tmp_path, monkeypatch):
    from api_agent.workflows.run_context import trial
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_request (call)')
    with trial(2):
        publish(REQUEST, RESULT)
    assert 'conversation_id' not in json.loads(path.read_text())['data']



def test_raw_validation_retains_reasoning_metadata_for_capture():
    from api_agent.testing.inference_usage import validate_event
    raw = row() | {'reasoning_effort': 'low'}
    parsed, evidence = validate_event(event(raw))
    assert parsed == raw
    assert evidence['reasoning_effort'] == 'low'
    tracker = SuiteUsage('run')
    capture(event(raw), tracker)
    assert tracker.inference_calls[raw['call_id']]['reasoning_effort'] == 'low'
