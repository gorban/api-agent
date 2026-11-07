"""Synthetic confirmation execution and immutable native retry records."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.confirmations import confirmation_count, confirmation_outcome
from api_agent.testing import integration
from api_agent.testing.recording import RunIdentity, RunRecorder


@pytest.mark.parametrize('value,expected', [(None, 0), ('', 0), (0, 0), (2, 2)])
def test_confirmation_count(value, expected):
    assert confirmation_count(value) == expected


@pytest.mark.parametrize('value', [True, -1, '2', 2.0])
def test_confirmation_count_rejects_invalid(value):
    with pytest.raises(ValueError):
        confirmation_count(value)


@pytest.mark.parametrize('statuses,expected', [(['passed'], ('passed', False)),
    (['failed'], ('failed', True)), (['failed', 'passed'], ('failed', True)),
    (['failed', 'passed', 'passed'], ('passed', False)),
    (['failed', 'failed'], ('failed', False)),
    (['failed', 'passed', 'failed'], ('failed', False)),
    (['failed', 'skipped'], ('failed', False)),
    (['failed', 'cancelled'], ('cancelled', False)),
    (['error'], ('failed', True)), (['broken', 'passed', 'passed'], ('passed', False))])
def test_confirmation_protocol(statuses, expected):
    assert confirmation_outcome(statuses, 2) == expected


@pytest.mark.parametrize('statuses,count', [([], 2), (['failed', 'passed'], 0),
    (['passed', 'passed'], 2), (['skipped', 'passed'], 2),
    (['failed', 'failed', 'passed'], 2)])
def test_invalid_confirmation_history(statuses, count):
    with pytest.raises(ValueError):
        confirmation_outcome(statuses, count)


def identity():
    return TestIdentity('suite::test_case', 'suite', 'test_case.py', 'test_case.py::test_case',
                        tags=frozenset({'requires_ui_session'}))


@pytest.mark.parametrize('interrupted', [False, True])
def test_auth_storage_survives_confirmation_waves_but_not_suite(monkeypatch, interrupted):
    from pathlib import Path
    test = identity()
    directories = []

    def execute(context, tests, **options):
        directory = Path(options['suite_directory'])
        assert directory.is_dir()
        directories.append(directory)
        saved = directory / 'auth-marker'
        if len(directories) == 1:
            saved.write_text('suite-owned')
        else:
            assert saved.read_text() == 'suite-owned'
            if interrupted:
                raise KeyboardInterrupt
        options['on_result'](TestResult(test, 'failed' if len(directories) == 1 else 'passed'))
        return integration.IntegrationRunResult(0, (), '')

    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            integration.run_integration_tests(object(), [test], accepted=True,
                config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2})
    else:
        result = integration.run_integration_tests(object(), [test], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2})
        assert result.tests[0].status == 'passed'
    assert len(set(directories)) == 1
    assert len(directories) == (2 if interrupted else 3)
    assert not directories[0].exists()


@pytest.mark.parametrize('statuses,expected', [(['passed'], 'passed'),
    (['failed', 'passed', 'passed'], 'passed'), (['error', 'passed', 'passed'], 'passed'),
    (['broken', 'passed', 'passed'], 'passed'), (['failed', 'failed'], 'failed'),
    (['failed', 'passed', 'failed'], 'failed'), (['failed', 'skipped'], 'failed')])
def test_runner_confirmations_keep_worker_count_and_attempt_owners(monkeypatch, statuses, expected):
    test = identity()
    starts, observed, events, results = [], [], [], []
    statuses = iter(statuses)
    def execute(context, tests, **options):
        starts.append(tuple(tests))
        observed.append(options['parallel_jobs'])
        options['before_run']()
        options['on_event'](RunnerEvent('test_started', test.repo, test))
        outcome = TestResult(test, next(statuses))
        options['on_result'](outcome)
        return integration.IntegrationRunResult(int(outcome.status != 'passed'), (outcome,), '')
    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    before, after = Mock(), Mock()
    result = integration.run_integration_tests(object(), [test], accepted=True,
        config={'TUI_MODE':'e2e', 'E2E_RERUNS':2}, parallel_jobs=7,
        before_run=before, after_run=after, on_event=events.append, on_result=results.append)
    assert result.tests[0].status == expected
    assert result.tests[0].identity == test
    assert result.tests[0].first_status == results[0].status
    assert len({row.identity.id for row in results}) == len(results)
    assert all(row.identity.node_id == test.node_id for row in results)
    assert [row.attempt for row in results] == list(range(1, len(results) + 1))
    assert observed == [7] * len(results)
    assert result.exit_code == int(expected != 'passed')
    before.assert_called_once()
    after.assert_called_once_with(result)


def test_recorder_recovers_only_complete_e2e_protocol(tmp_path):
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', 'e2e'))
    names = [recorder.record_test(TestResult(identity(), status), attempt=index)
        for index, status in enumerate(['failed', 'passed', 'passed'], 1)]
    manifest = recorder.finalize(status='completed', e2e_reruns=2)
    rows = [json.loads((recorder.directory / name).read_text()) for name in names]
    assert len({row['uuid'] for row in rows}) == 3
    assert len({row['historyId'] for row in rows}) == 1
    assert [row['status'] for row in rows] == ['failed', 'passed', 'broken']
    assert manifest['metadata']['e2e_reruns'] == 2
    assert manifest['metadata']['e2e_outcomes'][0]['final_status'] == 'passed'
    assert manifest['status'] == 'completed'


def test_recorder_cannot_complete_incomplete_confirmations(tmp_path):
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', 'e2e'))
    recorder.record_test(TestResult(identity(), 'failed'))
    recorder.record_test(TestResult(identity(), 'passed'), attempt=2)
    with pytest.raises(ValueError, match='cannot finalize'):
        recorder.finalize(status='completed', e2e_reruns=2)


def test_integration_failure_guard_remains(tmp_path):
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', 'integration'))
    recorder.record_test(TestResult(identity(), 'failed'))
    with pytest.raises(ValueError, match='cannot finalize'):
        recorder.finalize(status='completed')


def test_recorded_recovery_retains_all_attempt_usage_and_evidence(tmp_path, monkeypatch):
    from uuid import uuid4
    from api_agent.testing import recorded
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    test = replace(test, tags=frozenset({'requires_ui_session'}))
    config.update(TUI_MODE='e2e', E2E_RERUNS=2)
    statuses = iter(['failed', 'passed', 'passed'])
    def execute(context, tests, **options):
        options['before_run']()
        call = dict(call_id=str(uuid4()), selection='synthetic-model', usage=None)
        options['on_event'](RunnerEvent('inference_usage', identity=test, text=json.dumps(call)))
        call.update(usage=dict(input_tokens=2, output_tokens=1, cached_input_tokens=0, reasoning_tokens=0),
                    quote={'list_cost_usd': .25, 'payment': None})
        options['on_event'](RunnerEvent('inference_usage', identity=test, text=json.dumps(call)))
        result = TestResult(test, next(statuses))
        options['on_result'](result)
        return integration.IntegrationRunResult(int(result.status != 'passed'), (result,), '')
    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True, storage=Storage())
    assert result.persisted and result.exit_code == 0
    assert result.usage['prompt_count'] == 3
    assert result.usage['estimated_cost_usd'] == .75
    accounting = json.loads((result.directory / 'accounting.json').read_text())
    assert len(accounting['tests']) == 3
    assert len({row['test_id'] for row in accounting['tests']}) == 3
    assert [len(row['calls']) for row in accounting['tests']] == [1, 1, 1]
    native = [json.loads(path.read_text()) for path in result.directory.glob('*-result.json')]
    assert sorted(row['status'] for row in native) == ['broken', 'failed', 'passed']
    assert all(any(item['name'] == 'Test cost estimate' for item in row['attachments']) for row in native)


def test_logical_usage_keeps_physical_artifacts_independent():
    from api_agent.testing.confirmations import logical_usage
    report = dict(usage_rows=[{'session_id':'attempt-2', 'input_tokens':2}],
        unmeasured_calls={'logical':1, 'attempt-2':2},
        test_costs={'logical':dict(estimated_cost_usd=.25, raw_quota_cost_usd=None, list_cost_usd=.25,
            prompt_count=1, category_costs_usd={'input_tokens':.1}),
            'attempt-2':dict(estimated_cost_usd=.5, raw_quota_cost_usd=None, list_cost_usd=.5,
            prompt_count=2, category_costs_usd={'input_tokens':.2})})
    result = logical_usage(report, {'attempt-2':'logical'})
    assert result['test_costs']['logical']['estimated_cost_usd'] == .75
    assert result['test_costs']['logical']['cost_per_prompt_usd'] == .25
    assert result['test_costs']['logical']['category_costs_usd']['input_tokens'] == pytest.approx(.3)
    assert result['unmeasured_calls'] == {'logical':3}
    assert result['usage_rows'][0]['session_id'] == 'logical'
    assert report['usage_rows'][0]['session_id'] == 'attempt-2'
    assert len(report['test_costs']) == 2


def test_logical_usage_combines_unmeasured_without_costs():
    from api_agent.testing.confirmations import logical_usage
    report = {'test_costs': {'first': {'prompt_count': 0}, 'second': {'prompt_count': 0}}}
    combined = logical_usage(report, {'first':'logical', 'second':'logical'})
    assert combined['test_costs']['logical']['estimated_cost_usd'] is None
    assert combined['test_costs']['logical']['cost_per_prompt_usd'] is None
    assert combined['test_costs']['logical']['category_costs_usd'] == {}


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_synthetic_suite_failure_has_complete_readable_ledger(tmp_path, status):
    from api_agent.testing.allure_counts import result_counts
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', 'e2e'))
    recorder.record_test(TestResult(identity(), 'passed'))
    manifest = recorder.finalize(status=status, e2e_reruns=2)
    files = {name: (recorder.directory / name).read_bytes()
             for name in manifest['files'] if name.endswith('-result.json')}
    counts = result_counts(manifest, files)
    assert counts == {'first':1, 'final':None if status == 'interrupted' else 1, 'total':2}


def test_logical_usage_single_attempt_retains_its_cost():
    from api_agent.testing.confirmations import logical_usage
    report = {'test_costs': {'physical': {'estimated_cost_usd': .25}}}
    assert logical_usage(report, {'physical':'logical'})['test_costs'] == {
        'logical': {'estimated_cost_usd': .25}}


@pytest.mark.parametrize('cancelled', [False, True])
def test_infrastructure_abort_keeps_unreported_selection_explicit(monkeypatch, cancelled):
    first = identity()
    second = replace(first, id='second', node_id=first.node_id + '_second')
    def execute(context, tests, **options):
        options['before_run']()
        options['on_result'](TestResult(first, 'failed'))
        return integration.IntegrationRunResult(130 if cancelled else 1, (), '', cancelled,
            infrastructure_failed=not cancelled)
    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    result = integration.run_integration_tests(object(), [first, second], accepted=True,
        config={'TUI_MODE':'e2e', 'E2E_RERUNS':2})
    assert result.tests[1].status == ('cancelled' if cancelled else 'error')
    assert result.exit_code == (130 if cancelled else 1)
    assert result.infrastructure_failed == (not cancelled)
    assert len(result.attempts) == 1


@pytest.mark.parametrize('boundary', ['after_run', 'finished'])
def test_late_cancellation_aborts_retained_inspection(monkeypatch, boundary):
    from threading import Event
    cancel = Event()
    inspection = Mock()
    test = identity()
    def execute(context, tests, **options):
        options['on_result'](TestResult(test, 'passed'))
        return integration.IntegrationRunResult(0, (), '', inspections=(inspection,))
    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    monkeypatch.setattr('api_agent.testing.inspection_gate.publish_manual_accounting_ready', lambda _: None)
    def event(row):
        if boundary == 'finished' and row.kind == 'finished':
            cancel.set()
    result = integration.run_integration_tests(object(), [test], accepted=True,
        config={'TUI_MODE':'e2e', 'E2E_RERUNS':2}, cancel_event=cancel,
        on_event=event, after_run=lambda _: cancel.set() if boundary == 'after_run' else None)
    assert result.exit_code == 130 and result.cancelled
    inspection.abort.assert_called()


@pytest.mark.parametrize('cleanup_failure,publication_failure', [(False, False), (True, False), (False, True)])
def test_exception_preserves_cleanup_and_publication_duties(monkeypatch, cleanup_failure, publication_failure):
    test = identity()
    error = RuntimeError('worker unavailable')
    inspection = Mock()
    if cleanup_failure:
        inspection.abort.side_effect = RuntimeError('cleanup pending')
    error.inspections = (inspection,)
    published = []
    def execute(context, tests, **options):
        options['before_run']()
        options['on_result'](TestResult(test, 'failed'))
        raise error
    def after(result):
        published.append(result)
        if publication_failure:
            raise RuntimeError('publication unavailable')
    monkeypatch.setattr(integration, '_run_integration_tests', execute)
    with pytest.raises(RuntimeError) as caught:
        integration.run_integration_tests(object(), [test], accepted=True,
            config={'TUI_MODE':'e2e', 'E2E_RERUNS':2}, after_run=after)
    assert caught.value is error
    inspection.abort.assert_called_once()
    assert len(published) == int(not cleanup_failure)
    assert hasattr(error, 'confirmation_cleanup_errors') == cleanup_failure
    assert hasattr(error, 'confirmation_publication_error') == publication_failure


@pytest.mark.parametrize('mode,attempt,reason', [('integration', 1, 'E2E mode'), ('e2e', 2, 'contiguous')])
def test_recorder_rejects_unauthorized_confirmation_identity(tmp_path, mode, attempt, reason):
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', mode))
    recorder.record_test(TestResult(identity(), 'failed'), attempt=attempt)
    with pytest.raises(ValueError, match=reason):
        recorder.finalize(e2e_reruns=2)


@pytest.mark.parametrize('synthetic', [False, True])
def test_recorder_projection_write_failure_blocks_publication(tmp_path, monkeypatch, synthetic):
    from pathlib import Path
    recorder = RunRecorder(tmp_path / 'results', RunIdentity.create('suite', 'e2e'))
    recorder.record_test(TestResult(identity(), 'passed' if synthetic else 'failed'))
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path.suffix == '.tmp' and len(recorder._results) == (2 if synthetic else 1):
            raise OSError('projection disk unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_file)
    with pytest.raises(OSError, match='projection disk unavailable'):
        recorder.finalize(status='failed', e2e_reruns=0)
    assert not (recorder.directory / 'manifest.json').exists()
    with pytest.raises(RuntimeError, match='incomplete artifact'):
        recorder.finalize(e2e_reruns=0)


def test_missing_profile_snapshot_cannot_publish_success(tmp_path):
    from api_agent.testing import recorded
    from test_recorded_integration import fixture, Storage, fake_runner
    context, test, config = fixture(tmp_path)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
        resolved_test_profiles={}, runner=fake_runner(), storage=Storage())
    assert result.exit_code == 1 and not result.persisted
    assert result.manifest is None


@pytest.mark.parametrize('raw,expected', [('', None), ('0', 0), ('2', 2)])
def test_e2e_confirmation_config_preserves_explicit_empty_and_zero(tmp_path, raw, expected):
    from api_agent.config import load_shared_schema, resolve_config
    from test_config_foundation import AUTH
    result = resolve_config(load_shared_schema(), environ={**AUTH, 'TUI_MODE':'e2e', 'E2E_RERUNS':'7'},
        argv=['--e2e-reruns=' + raw], anchors={'owner_root':tmp_path})
    result.raise_for_errors()
    assert result.values['E2E_RERUNS'] == expected
    assert confirmation_count(result.values['E2E_RERUNS']) == (expected or 0)


@pytest.mark.parametrize('amounts,bases,zeroes,incomplete,expected_basis,expected_zero', [
    ([0,1], ['list_price','reported_charge'], [True,False], [False,False], 'reported_charge', False),
    ([1,2], ['list_price','reported_charge'], [False,False], [False,False], 'mixed', False),
    ([1,2], ['reported_charge','reported_charge'], [False,False], [False,False], 'reported_charge', False),
    ([0,0], ['list_price','list_price'], [True,True], [False,False], 'list_price', True),
    ([0,0], ['list_price','reported_charge'], [True,False], [False,False], 'reported_charge', False),
    ([None,1], ['unavailable','reported_charge'], [False,False], [True,False], 'unavailable', False),
    ([1,2], [None,'reported_charge'], [False,False], [False,False], 'unavailable', False),
    ([0,1], ['list_price','reported_charge'], [True,False], [False,True], 'reported_charge', False),
])
def test_logical_cost_metadata_describes_all_attempts(amounts,bases,zeroes,incomplete,expected_basis,expected_zero):
    from copy import deepcopy
    from api_agent.testing.confirmations import logical_usage
    from api_agent.tui.cost_history import cost_cell
    report = {'test_costs': {str(index): {'estimated_cost_usd':amount,
        'known_zero':zero, 'basis':basis, 'incomplete':partial, 'prompt_count':0 if zero else 1}
        for index,(amount,basis,zero,partial) in enumerate(zip(amounts,bases,zeroes,incomplete))}}
    original = deepcopy(report)
    combined = logical_usage(report, {'0':'logical','1':'logical'})['test_costs']['logical']
    assert combined['known_zero'] is expected_zero
    assert combined['basis'] == expected_basis
    assert combined['incomplete'] is (any(incomplete) or None in amounts)
    assert report == original
    if amounts == [0,1] and not any(incomplete):
        assert combined['estimated_cost_usd'] == 1
        assert cost_cell(combined) == '1.000000'
