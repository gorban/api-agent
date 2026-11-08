"""Delegated receipts cross the real recorder and native Allure exporter offline."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import run_recorded_integration
from api_agent.testing.inference_usage import publish_delegated_usage, publish_external_invocation
from api_agent.testing.native_usage import publish as publish_boundary, TOKENS
from api_agent.workflows.journal import WorkflowJournal
from api_agent.workflows.run_context import conversation
from test_recorded_integration import fixture, Storage


def params(step):
    return {row['name']: row['value'] for row in step['parameters']}


def definition():
    return SimpleNamespace(graph_id='Example', definition_id='a' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[SimpleNamespace(id='Make', kind='node', parent=None)],
            tables={'Edges': []}), resources=[], replacements=[])


@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('trace_content', [False, True])
@pytest.mark.parametrize('missing', [False, True])
def test_delegated_calls_export_exact_hierarchy_and_separate_boundary(tmp_path, monkeypatch,
        log_content, trace_content, missing):
    context, selected, config = fixture(tmp_path)
    config.update(LOG_CONTENT=log_content, TRACELOOP_TRACE_CONTENT=trace_content)
    events_path = tmp_path / 'worker-events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events_path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', selected.node_id + ' (call)')
    monkeypatch.setenv('LOG_CONTENT', str(log_content))
    monkeypatch.setenv('TRACELOOP_TRACE_CONTENT', str(trace_content))
    chat, turn, graph_id, visit_id = (str(uuid4()) for _ in range(4))
    calls = [str(uuid4()) for _ in range(3 if missing else 2)]
    secret = 'registered-provider-credential-must-not-export'
    hidden = 'private-hidden-reasoning-must-not-export'
    with conversation(chat):
        journal = WorkflowJournal(definition(), turn_id=turn)
    journal.observe(dict(event='scope_start', scope_id=graph_id, parent_id=None, kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=visit_id, parent_id=graph_id, kind='node', owner='Make', started_ms=1))
    publish_external_invocation(journal.invocation_id, turn_id=turn)
    usage = dict(input_tokens=10, output_tokens=3, cached_input_tokens=0,
                 cache_write_tokens=0, reasoning_tokens=None)
    for call in calls[:2]:
        publish_delegated_usage(call_id=call, selection='selected', actual_model='actual', usage=usage,
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00',
            outcome='completed', owner=(journal, visit_id, None))
    # The public schema rejects hidden reasoning before it can reach any exporter.
    with pytest.raises(ValueError):
        publish_delegated_usage(call_id=str(uuid4()), selection='selected',
            usage={**usage, 'hidden_reasoning': hidden}, owner=(journal, visit_id, None),
            started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00', outcome='completed')
    publish_external_invocation(journal.invocation_id, turn_id=turn, call_ids=calls, complete=True)
    journal.observe(dict(event='node', scope_id=visit_id, node='Make'))
    journal.observe(dict(event='scope_end', scope_id=visit_id, parent_id=graph_id, kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Make', target='END', implicit=True, visit_id=visit_id))
    journal.observe(dict(event='scope_end', scope_id=graph_id, parent_id=None, kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    retained = journal.finalize()
    before = dict(available=True, captured_at=1, pct_used=10, reset_at='2027-01-01T00:00:00+00:00', categories=dict.fromkeys(TOKENS, 0))
    after = dict(before, captured_at=2, pct_used=11, categories=dict.fromkeys(TOKENS, 900))
    publish_boundary(dict(version=1, scope='account_boundary', status='complete', graph_invocations=1, before=before, after=after))
    worker_events = [json.loads(line) for line in events_path.read_text().splitlines()]

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for event in worker_events:
            row = RunnerEvent(event['kind'], identity=selected, text=json.dumps(event['data']))
            hooks['on_event'](row)
            if event['kind'] == 'inference_usage':
                hooks['on_event'](row)  # duplicate transport delivery, not another call
        hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(retained)))
        outcome = TestResult(selected, 'failed', .1, secret + ' safe failure')
        hooks['on_result'](outcome)
        result = IntegrationRunResult(1, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [selected], config, accepted=True,
        runner=runner, storage=Storage(), secret_values=(secret,),
        workflow_admissions={retained['projection']['definition_id']: retained['projection']})
    assert result.persisted and result.exit_code == 1
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert row['statusDetails']['trace'] == '[REDACTED] safe failure'
    chat_step = next(step for step in row['steps'] if step['name'].startswith('Conversation '))
    assert params(chat_step)['conversation_id'] == chat
    turn_step = chat_step['steps'][0]
    assert turn_step['name'] == 'User turn 1' and params(turn_step)['turn_id'] == turn
    graph = next(step for step in turn_step['steps'] if step['name'] == 'graph GRAPH')
    assert params(graph)['invocation_id'] == journal.invocation_id
    node = next(step for step in graph['steps'] if step['name'] == 'node Make')
    leaves = [step for step in node['steps'] if step['name'].startswith('call ')]
    assert {step['name'] for step in leaves} == {'call ' + call for call in calls[:2]}
    assert len(leaves) == 2
    for leaf in leaves:
        values = params(leaf)
        assert values['input_tokens'] == '10' and values['output_tokens'] == '3'
        assert values['cached_input_tokens'] == '0' and values['cache_write_tokens'] == '0'
        assert values['reasoning_tokens'] == 'unknown'
        assert values['actual_model'] == 'actual'
    accounting = json.loads((result.directory / 'accounting.json').read_text())['tests'][0]
    assert len(accounting['calls']) == 2
    assert accounting['calls_complete'] is (not missing)
    assert accounting['observed_call_count'] == len(calls)
    assert all(call['usage']['reasoning_tokens'] is None for call in accounting['calls'])
    assert result.usage['prompt_count'] == 1
    assert result.usage['usage_rows'][0]['input_tokens'] == 20
    assert result.usage['usage_rows'][0]['output_tokens'] == 6
    assert result.usage['usage_rows'][0]['cache_read_tokens'] == 0
    assert result.usage['usage_rows'][0]['reasoning_tokens'] is None
    attachment = next(item for item in row['attachments'] if item['name'] == 'Native model usage boundary')
    boundary = json.loads((result.directory / attachment['source']).read_text())
    assert boundary['scope'] == 'account_boundary' and boundary['after']['categories']['input_tokens'] == 900
    artifacts = '\n'.join(path.read_text() for path in result.directory.rglob('*') if path.is_file())
    assert secret not in artifacts and hidden not in artifacts


def test_native_boundary_crosses_real_worker_event_parser(tmp_path):
    from api_agent.testing.collection import collect_tests
    from test_integration_runner import fixture_repo, context
    source = '''from api_agent.testing.native_usage import publish, TOKENS

def test_boundary():
    before = dict(available=True, captured_at=1, pct_used=1, reset_at=None,
                  categories=dict.fromkeys(TOKENS, 0))
    after = dict(before, captured_at=2, categories=dict.fromkeys(TOKENS, 7))
    assert publish(dict(version=1, scope='account_boundary', status='complete',
                        graph_invocations=1, before=before, after=after))
'''
    base = fixture_repo(tmp_path / 'base', source)
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    result = run_recorded_integration(ctx, selected,
        dict(TUI_MODE='integration', ALLURE_S3_BUCKET='synthetic',
            ALLURE_RESULTS_DIR=tmp_path / 'allure', TEST_PARALLEL_JOBS=0),
        accepted=True, storage=Storage())
    assert result.persisted and result.exit_code == 0
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    attachment = next(item for item in row['attachments'] if item['name'] == 'Native model usage boundary')
    boundary = json.loads((result.directory / attachment['source']).read_text())
    assert boundary['after']['categories']['input_tokens'] == 7
    assert result.usage['usage_rows'] == []
