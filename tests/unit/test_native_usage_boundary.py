"""Account counters remain separate from measured calls and content capture."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest


def boundary():
    tokens = dict(input_tokens=40, output_tokens=10, cached_input_tokens=3,
                  cache_write_tokens=0, reasoning_tokens=2, total_tokens=50)
    before = dict(available=True, captured_at=1.0, pct_used=10.0,
                  reset_at='2026-11-09T00:00:00Z', categories=tokens)
    after = deepcopy(before)
    after.update(captured_at=2.0, pct_used=10.2, categories={k: v * 2 for k, v in tokens.items()})
    return dict(version=1, scope='account_boundary', status='complete',
                graph_invocations=1, before=before, after=after)


def test_boundary_event_is_content_free_and_survives_disabled_capture(tmp_path, monkeypatch):
    from api_agent.testing.native_usage import publish, validate_packet, reconciliation
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_native.py::test_call (call)')
    owner = dict(kind='turn', conversation_id=str(uuid4()), turn_id=str(uuid4()))
    assert publish(boundary(), owner=owner)
    event = json.loads(path.read_text())
    assert event['kind'] == 'native_usage_boundary'
    packet = validate_packet(event['data'])
    assert packet['owner'] == owner
    result = reconciliation(packet['boundary'], {'usage_observations': [dict(
        session_id='test', llm_calls=2, input_tokens=8, output_tokens=3,
        cache_read_tokens=None, cache_creation_tokens=0, reasoning_tokens=None,
        unknown_fields=['cache_read_tokens', 'reasoning_tokens'])],
        'measured_calls': {'test': 2}, 'unmeasured_model_calls': {'test': 1}}, 'test')
    assert result['account_boundary']['category_deltas']['input_tokens'] == 40
    assert result['measured_model_calls']['count'] == 2
    assert result['measured_model_calls']['categories']['input_tokens'] == 8
    assert result['measured_model_calls']['categories']['cached_input_tokens'] is None
    assert result['measured_model_calls']['categories']['cache_write_tokens'] == 0
    assert result['unmeasured_model_calls'] == 1
    assert result['status'] == 'partial'
    assert result['account_boundary']['pct_used_delta'] == pytest.approx(.2)


@pytest.mark.parametrize('content', [False, True])
def test_recorder_attaches_boundary_and_reconciliation_without_adding_tokens(tmp_path, content):
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from test_recorded_integration import fixture, Storage
    context, selected, config = fixture(tmp_path)
    config.update(LOG_CONTENT=content, TRACELOOP_TRACE_CONTENT=content)
    result = TestResult(selected, 'passed', .01)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('native_usage_boundary', selected.repo, selected,
            text=json.dumps(dict(boundary=boundary(), owner={'kind': 'test'}))))
        hooks['on_result'](result)
        run = IntegrationRunResult(0, (result,), '')
        hooks['after_run'](run)
        return run
    run = run_recorded_integration(context, [selected], config, accepted=True,
                                   runner=runner, storage=Storage())
    assert run.persisted and run.exit_code == 0
    row = json.loads(next(run.directory.glob('*-result.json')).read_text())
    attachments = {r['name']: r for r in row['attachments']}
    native = json.loads((run.directory / attachments['Native model usage boundary']['source']).read_text())
    assert native['scope'] == 'account_boundary'
    assert native['before']['categories']['input_tokens'] == 40
    recon = json.loads((run.directory / attachments['Usage reconciliation']['source']).read_text())
    assert recon['status'] == 'unavailable'
    assert recon['account_boundary']['category_deltas'] == dict(
        input_tokens=40, output_tokens=10, cached_input_tokens=3,
        cache_write_tokens=0, reasoning_tokens=2, total_tokens=50)
    suite = json.loads((run.directory / attachments['Suite usage']['source']).read_text())
    assert suite['usage_rows'] == []


@pytest.mark.parametrize('field,value', [
    ('input_tokens', None), ('input_tokens', 1),
])
def test_missing_or_decreasing_boundary_categories_do_not_claim_complete(field, value):
    from api_agent.testing.native_usage import reconciliation
    data = boundary()
    data['after']['categories'][field] = value
    report = {'measured_calls': {'t': 1}, 'usage_observations': [dict(session_id='t', llm_calls=1,
        input_tokens=1, output_tokens=1, cache_read_tokens=0,
        cache_creation_tokens=0, reasoning_tokens=0, total_tokens=2)]}
    result = reconciliation(data, report, 't')
    assert result['status'] == 'partial'
    assert result['account_boundary']['category_deltas'][field] is None


@pytest.mark.parametrize('change', [
    lambda p: p.update(extra='secret'),
    lambda p: p.update(owner={'kind': 'test', 'prompt': 'secret'}),
    lambda p: p['boundary'].update(extra='secret'),
    lambda p: p['boundary'].update(graph_invocations=True),
    lambda p: p['boundary'].update(graph_invocations=None),
    lambda p: p['boundary'].update(version=True),
    lambda p: p['boundary'].update(scope='call'),
    lambda p: p['boundary'].update(status='secret'),
    lambda p: p['boundary']['before'].update(extra='secret'),
    lambda p: p['boundary']['before'].update(available=1),
    lambda p: p['boundary']['before'].update(captured_at=float('nan')),
    lambda p: p['boundary']['before'].update(pct_used=101),
    lambda p: p['boundary']['before'].update(reset_at='secret'),
    lambda p: p['boundary']['before'].update(reset_at=1),
    lambda p: p['boundary']['before'].update(reset_at='2026-01-01'),
    lambda p: p['boundary']['before']['categories'].update(input_tokens=-1),
    lambda p: p['boundary']['before']['categories'].update(input_tokens=10**1000),
    lambda p: p['boundary']['before']['categories'].update(input_tokens=True),
    lambda p: p['boundary']['before']['categories'].update(input_tokens='secret'),
])
def test_invalid_boundary_cannot_be_used_as_content_bypass(change):
    from api_agent.testing.native_usage import validate_packet
    packet = dict(boundary=boundary(), owner={'kind': 'test'})
    change(packet)
    with pytest.raises(ValueError):
        validate_packet(packet)


@pytest.mark.parametrize('reason', ['missing_start', 'interrupted', 'reset', 'pct_reset', 'reversed'])
def test_reconciliation_marks_incomplete_or_reset_boundaries_unavailable(reason):
    from api_agent.testing.native_usage import reconciliation
    data = boundary()
    if reason == 'missing_start':
        data['before']['available'] = False
    elif reason == 'interrupted':
        data['status'] = 'interrupted'
    elif reason == 'reset':
        data['after']['reset_at'] = '2026-11-10T00:00:00Z'
    elif reason == 'pct_reset':
        data['after']['pct_used'] = 1
    else:
        data['after']['captured_at'] = 0
    result = reconciliation(data, {}, 'test')
    assert result['account_boundary']['pct_used_delta'] is None
    assert set(result['account_boundary']['category_deltas'].values()) == {None}


def test_no_sink_or_forbidden_test_evidence_is_inert(monkeypatch):
    from api_agent.testing.native_usage import publish
    from api_agent.workflows.run_context import test_evidence
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    assert publish(boundary()) is False
    with test_evidence(False):
        assert publish(boundary()) is False


def test_boundary_allows_explicitly_unknown_reset_and_categories():
    from api_agent.testing.native_usage import validate_packet
    data = boundary()
    data['before'].update(reset_at=None, categories={key: None for key in data['before']['categories']})
    packet = dict(boundary=data, owner={'kind': 'test'})
    assert validate_packet(packet) == packet


def test_complete_reconciliation_keeps_scopes_and_reported_lower_bounds_separate():
    from api_agent.testing.native_usage import reconciliation
    row = dict(session_id='t', llm_calls=2, input_tokens=8, output_tokens=2,
               cache_read_tokens=0, cache_creation_tokens=1, reasoning_tokens=0, total_tokens=10)
    report = {'measured_calls': {'t': 2}, 'usage_observations': [row]}
    result = reconciliation(boundary(), report, 't')
    assert result['status'] == 'complete'
    assert result['measured_model_calls']['scope'] == 'test'
    assert result['measured_model_calls']['known_subtotals']['input_tokens'] == 8
    assert result['account_boundary']['category_deltas']['input_tokens'] == 40
    row['unknown_fields'] = ['input_tokens']
    report['unmeasured_native_invocations'] = {'t': 1}
    result = reconciliation(boundary(), report, 't')
    assert result['status'] == 'partial'
    assert result['measured_model_calls']['categories']['input_tokens'] is None
    assert result['measured_model_calls']['known_subtotals']['input_tokens'] == 8
    assert result['unmeasured_native_invocations'] == 1


def test_native_usage_attachment_budget_is_bounded(tmp_path):
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.testing.contracts import RunnerEvent
    from test_recorded_integration import fixture, Storage
    context, selected, config = fixture(tmp_path)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for _ in range(65):
            hooks['on_event'](RunnerEvent('native_usage_boundary', selected.repo, selected,
                text=json.dumps(dict(boundary=boundary(), owner={'kind': 'test'}))))
    with pytest.raises(ValueError, match='recording limit'):
        run_recorded_integration(context, [selected], config, accepted=True,
                                 runner=runner, storage=Storage())
