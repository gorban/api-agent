"""Adversarial confirmation ownership and publication cancellation checks."""
from dataclasses import replace
import json
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.providers.inference import usage_to_wire
from api_agent.providers.llm import Usage
from api_agent.testing import integration, recorded
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.workflows.journal import WorkflowJournal


def selected_case():
    return TestIdentity('suite::test_case.py::test_case', 'suite',
        'test_case.py', 'test_case.py::test_case',
        tags=frozenset({'requires_ui_session'}))


def workflow_call():
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64,
        entry_root='base', resources=[], replacements=[], layout=SimpleNamespace(
            steps=[SimpleNamespace(id='Make', kind='node', parent=None)], tables={'Edges': []}))
    journal = WorkflowJournal(definition)
    graph, node = str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=graph, parent_id=None,
        kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=node, parent_id=graph,
        kind='node', owner='Make', started_ms=1))
    journal.record_call(call_id=str(uuid4()), visit_id=node)
    journal.observe(dict(event='node', scope_id=node, node='Make'))
    journal.observe(dict(event='scope_end', scope_id=node, parent_id=graph,
        kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Make', target='END',
        implicit=True, visit_id=node))
    journal.observe(dict(event='scope_end', scope_id=graph, parent_id=None,
        kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='passed'))
    return journal.finalize()


def test_recorded_confirmations_keep_admitted_workflow_and_call_ownership(tmp_path):
    selected = selected_case()
    journals = [workflow_call() for _ in range(3)]
    definition_id = journals[0]['projection']['definition_id']
    admissions = {(selected.id, definition_id): journals[0]['projection']}
    statuses = ['failed', 'passed', 'passed']
    calls = [next(event['call_id'] for event in journal['events']
                  if event['event'] == 'call') for journal in journals]
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    config = {'TUI_MODE': 'e2e', 'E2E_RERUNS': 2,
        'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': tmp_path / 'results',
        'TEST_PARALLEL_JOBS': 0}

    def runner(context, tests, **hooks):
        hooks['before_run']()
        outcomes = []
        for attempt, (status, journal, call_id) in enumerate(zip(statuses, journals, calls), 1):
            physical = replace(selected, id=(selected.id if attempt == 1 else
                selected.id + '::confirmation:' + str(attempt)))
            hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, physical,
                text=json.dumps(journal)))
            hooks['on_event'](RunnerEvent('inference_usage', selected.repo, physical,
                text=json.dumps({'call_id': call_id, 'selection': 'synthetic',
                    'usage': usage_to_wire(Usage(3, 2)), 'attempt': attempt,
                    'turn_id': journal['turn_id'], 'outcome': 'completed'})))
            outcome = TestResult(physical, status, attempt=attempt, logical_id=selected.id)
            outcomes.append(outcome)
            hooks['on_result'](outcome)
        final = TestResult(selected, 'passed', first_status='failed')
        execution = integration.IntegrationRunResult(0, (final,), '',
            attempts=tuple(outcomes), e2e_reruns=2)
        hooks['after_run'](execution)
        return execution

    result = recorded.run_recorded_integration(context, [selected], config, accepted=True,
        workflow_admissions=admissions, runner=runner,
        storage=SimpleNamespace(persist=lambda *args: None))

    assert result.persisted and result.exit_code == 0
    joined = json.loads((result.directory / 'workflow-accounting-join.json').read_text())
    accounting = json.loads((result.directory / 'accounting.json').read_text())
    assert len(joined['tests']) == len(accounting['tests']) == 3
    assert [row['attribution'] for row in joined['tests']] == ['complete'] * 3
    assert [row['routes'][0]['definition_binding'] for row in joined['tests']] == ['verified'] * 3
    assert [row['calls'][0]['call_id'] for row in accounting['tests']] == calls
    assert [row['calls'][0]['attempt'] for row in accounting['tests']] == [1, 2, 3]
    assert sum(row['calls'][0]['usage']['input_tokens'] for row in accounting['tests']) == 9
    assert sum(row['calls'][0]['usage']['output_tokens'] for row in accounting['tests']) == 6
    assert {row['session_id'] for row in result.usage['usage_observations']} == {selected.id}
    assert sum(row['total_tokens'] for row in result.usage['usage_observations']) == 15
    assert set(result.usage['test_costs']) == {selected.id}
    assert set(admissions) == {(selected.id, definition_id)}
    rows = sorted((json.loads(path.read_text()) for path in result.directory.glob('*-result.json')),
        key=lambda row: int(row['parameters'][0]['value']))
    assert [row['status'] for row in rows] == ['failed', 'passed', 'broken']
    assert len({row['historyId'] for row in rows}) == 1
    assert len({row['uuid'] for row in rows}) == 3


@pytest.mark.parametrize('boundary', ['test_finished', 'after_run', 'finished'])
def test_cancellation_at_final_callbacks_remains_authoritative(monkeypatch, boundary):
    selected = selected_case()
    cancel = Event()
    published = []
    ready = []

    def wave(context, tests, **options):
        outcome = TestResult(selected, 'passed')
        options['on_result'](outcome)
        return integration.IntegrationRunResult(0, (outcome,), '')

    def event(row):
        if row.kind == boundary:
            cancel.set()

    def publish(result):
        published.append(result)
        if boundary == 'after_run':
            cancel.set()

    monkeypatch.setattr(integration, '_run_integration_tests', wave)
    monkeypatch.setattr('api_agent.testing.inspection_gate.publish_manual_accounting_ready',
        lambda inspections: ready.append(tuple(inspections)))

    result = integration.run_integration_tests(object(), [selected], accepted=True,
        config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2}, cancel_event=cancel,
        on_event=event, after_run=publish)

    assert result.cancelled and result.exit_code == 130
    assert len(published) == 1
    if boundary == 'test_finished':
        assert published[0].cancelled and published[0].exit_code == 130
    if boundary != 'finished':
        assert ready == []


def test_confirmation_interruption_finalizes_completed_attempts_before_reraise(monkeypatch):
    selected = selected_case()
    published = []
    interruption = KeyboardInterrupt()

    def wave(context, tests, **options):
        options['before_run']()
        if options['config']['_E2E_ATTEMPT'] == 2:
            raise interruption
        outcome = TestResult(selected, 'failed')
        options['on_result'](outcome)
        return integration.IntegrationRunResult(1, (outcome,), '')

    monkeypatch.setattr(integration, '_run_integration_tests', wave)

    with pytest.raises(KeyboardInterrupt) as caught:
        integration.run_integration_tests(object(), [selected], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2}, after_run=published.append)

    assert caught.value is interruption
    assert len(published) == 1
    assert published[0].cancelled and published[0].exit_code == 130
    assert [row.status for row in published[0].attempts] == ['failed']


def test_confirmation_interruption_retains_attempts_without_publisher(monkeypatch):
    selected = selected_case()
    interruption = KeyboardInterrupt()

    def wave(context, tests, **options):
        options['before_run']()
        options['on_result'](TestResult(selected, 'failed'))
        raise interruption

    monkeypatch.setattr(integration, '_run_integration_tests', wave)

    with pytest.raises(KeyboardInterrupt) as caught:
        integration.run_integration_tests(object(), [selected], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2})

    assert caught.value is interruption
    assert interruption.execution.cancelled and interruption.execution.exit_code == 130
    assert [row.status for row in interruption.execution.attempts] == ['failed']


def test_real_workers_prepare_and_dispose_ui_for_every_confirmation(tmp_path):
    from api_agent.testing.collection import collect_tests
    suite = tmp_path / 'tests/e2e'
    suite.mkdir(parents=True)
    (suite / 'test_case.py').write_text('''import os
from pathlib import Path
import pytest

@pytest.fixture
def ui_provider_session():
    attempt = os.environ['API_AGENT_TEST_ATTEMPT']
    with Path('ui-cycles').open('a') as stream:
        stream.write('prepare ' + attempt + '\\n')
    yield object()
    with Path('ui-cycles').open('a') as stream:
        stream.write('dispose ' + attempt + '\\n')

@pytest.mark.requires_ui_session
def test_case():
    assert int(os.environ['API_AGENT_TEST_ATTEMPT']) > 1
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: [suite])
    config = {'TUI_MODE': 'e2e', 'E2E_RERUNS': 2}
    selected = collect_tests(context, 'e2e', config=config)

    result = integration.run_integration_tests(context, selected, config=config, accepted=True)

    assert result.exit_code == 0
    assert [row.status for row in result.tests] == ['passed']
    assert [row.status for row in result.attempts] == ['failed', 'passed', 'passed']
    assert (tmp_path / 'ui-cycles').read_text().splitlines() == [
        'prepare 1', 'dispose 1', 'prepare 2', 'dispose 2', 'prepare 3', 'dispose 3']


def test_confirmation_waves_use_profiles_for_their_admitted_selection(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.test_profiles import resolve_test_profiles

    suite = tmp_path / 'tests/e2e'
    suite.mkdir(parents=True)
    (suite / 'test_cases.py').write_text('''import os
import pytest

@pytest.fixture
def ui_provider_session():
    yield object()

@pytest.mark.requires_ui_session
def test_pass():
    pass

@pytest.mark.requires_ui_session
def test_retry():
    assert int(os.environ['API_AGENT_TEST_ATTEMPT']) > 1
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: [suite])
    config = {'TUI_MODE': 'e2e', 'E2E_RERUNS': 2}
    selected = collect_tests(context, 'e2e', config=config)
    profiles = resolve_test_profiles(selected, config, owner_root=tmp_path, context=context)
    published = []

    result = integration.run_integration_tests(context, selected, config=config, accepted=True,
        resolved_test_profiles=profiles, after_run=published.append)

    assert result.exit_code == 0
    assert [row.status for row in result.tests] == ['passed', 'passed']
    assert [row.status for row in result.attempts] == ['passed', 'failed', 'passed', 'passed']
    assert published == [result]
    assert set(profiles) == {test.id for test in selected}


@pytest.mark.parametrize('damage', ['extra', 'missing', 'repo', 'node_id'])
def test_e2e_first_wave_rejects_invalid_frozen_profiles(tmp_path, monkeypatch, damage):
    from api_agent.testing.collection import CollectionError

    case = selected_case()
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: (case,))
    monkeypatch.setattr(integration.subprocess, 'Popen',
        lambda *args, **kwargs: pytest.fail('Invalid profiles started a worker'))
    profiles = {case.id: {'repo': case.repo, 'node_id': case.node_id, 'config': {}}}
    if damage == 'extra':
        profiles['extra'] = profiles[case.id].copy()
    elif damage == 'missing':
        profiles.clear()
    else:
        profiles[case.id][damage] = 'wrong'

    with pytest.raises(CollectionError, match='do not match'):
        integration.run_integration_tests(context, [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2}, resolved_test_profiles=profiles)
