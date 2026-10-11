"""Synthetic hooks join recording, usage and storage without live suites/S3."""
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from api_agent.telemetry.usage import QuotaSnapshot
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing import recorded


class Storage:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def persist(self, directory, manifest):
        self.calls.append((directory, manifest))
        if self.fail:
            raise RuntimeError('private-provider-error')


def fixture(tmp_path):
    context = SimpleNamespace(base_root=tmp_path / 'base', owner_root=tmp_path / 'pack')
    test = TestIdentity('pack::tests/integration/test_api.py::test_listing', 'pack',
                        'tests/integration/test_api.py', 'tests/integration/test_api.py::test_listing')
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'artifacts', 'TEST_PARALLEL_JOBS': 0}
    return context, test, config


def fake_runner(status='passed', *, deliver=True):
    def run(context, tests, **hooks):
        assert hooks['accepted'] is True and hooks['no_std_in'] is False
        outcomes = tuple(TestResult(test, status, .25) for test in tests)
        result = IntegrationRunResult(130 if status == 'cancelled' else int(status not in {'passed', 'skipped'}),
                                      outcomes, '', status == 'cancelled')
        try:
            hooks['before_run']()
            if deliver:
                for outcome in outcomes:
                    hooks['on_result'](outcome)
            hooks['after_run'](result)
        except Exception:
            result = IntegrationRunResult(result.exit_code or 1, outcomes, 'hook failed', result.cancelled)
        return result
    return run


@pytest.mark.parametrize('status,expected', [('passed', 'completed'), ('failed', 'failed'), ('cancelled', 'interrupted')])
@pytest.mark.parametrize('delta', [0, 1.5])
def test_hooks_preserve_outcomes_usage_and_finalization_after_cleanup(tmp_path, status, expected, delta):
    context, test, config = fixture(tmp_path)
    config['TOKEN_USAGE_CSV'] = tmp_path / 'external.csv'
    config['TOKEN_USAGE_CSV_ENABLED'] = True
    storage = Storage()
    instant = datetime(2028, 1, 1, tzinfo=timezone.utc)
    snapshots = iter([QuotaSnapshot(10, 'window', instant + timedelta(hours=1), instant),
                      QuotaSnapshot(10 + delta, 'window', instant + timedelta(hours=1), instant + timedelta(seconds=5))])
    def prepare(usage):
        usage.record_prompt(turn_id='turn', prompt_id='initial', kind='initial')
        usage.add_usage(provider='synthetic_provider', model='selected', session_id='session', input_tokens=2, output_tokens=3)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True, storage=storage,
                    quota_fetcher=lambda: next(snapshots), prepare=prepare, runner=fake_runner(status))
    assert result.exit_code == (130 if status == 'cancelled' else int(status != 'passed'))
    assert result.persisted and len(storage.calls) == 1
    assert result.manifest['status'] == expected
    assert result.usage['pct_point_delta'] == delta and result.usage['cost_per_prompt_usd'] is None
    summary = json.loads((result.directory / 'usage-summary.json').read_text())
    assert summary['prompt_count'] == 1 and summary['before']['pct_used'] == 10
    assert summary['after']['pct_used'] == 10 + delta
    assert summary['pct_point_delta'] == delta
    accounting = json.loads((result.directory / 'accounting.json').read_text())
    assert accounting['quota']['before']['pct_used'] == 10
    assert accounting['quota']['after']['pct_used'] == 10 + delta
    assert accounting['quota']['pct_point_delta'] == delta
    assert accounting['quota']['quota_unavailable_reason'] is None
    with (result.directory / 'token-usage.csv').open() as stream:
        row = list(csv.DictReader(stream))[0]
    assert row['total_tokens'] == '5'
    assert (tmp_path / 'external.csv').read_bytes() == (result.directory / 'token-usage.csv').read_bytes()
    assert {'usage-summary.json', 'token-usage.csv'} <= result.manifest['files'].keys()
    rows = list(result.directory.glob('*-result.json'))
    assert len(rows) == 1 and json.loads(rows[0].read_text())['name'] == 'test_listing'


@pytest.mark.parametrize('updates,kwargs', [({}, {}), ({}, {'no_std_in': True}),
       ({'NO_STD_IN': True}, {}), ({'COLLECT_ONLY': True}, {}), ({'TUI_MODE': 'unit'}, {}),
       ({'TUI_MODE': 'e2e'}, {}), ({}, {'accepted': 'yes'})])
def test_rejected_modes_and_acceptance_have_no_recording_side_effects(tmp_path, updates, kwargs):
    context, test, config = fixture(tmp_path)
    config.update(updates)
    options = {'accepted': True, **kwargs}
    if updates == {} and kwargs == {}:
        options['accepted'] = False
    with pytest.raises(PermissionError):
        recorded.run_recorded_integration(context, [test], config, **options)
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.parametrize('empty', [True, False])
def test_empty_selection_or_missing_s3_rejected_before_artifacts(tmp_path, empty):
    context, test, config = fixture(tmp_path)
    if not empty:
        config['ALLURE_S3_BUCKET'] = ''
    with pytest.raises(ValueError):
        recorded.run_recorded_integration(context, [] if empty else [test], config, accepted=True)
    assert not (tmp_path / 'artifacts').exists()


def test_unavailable_quota_and_missing_callbacks_still_record_zero_prompt_run(tmp_path, monkeypatch):
    context, test, config = fixture(tmp_path)
    storage = Storage()
    options = []
    def factory(bucket, **kwargs):
        options.append((bucket, kwargs))
        return storage
    monkeypatch.setattr(recorded, 'RunStorage', factory)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True, runner=fake_runner(deliver=False))
    assert result.exit_code == 0 and result.persisted
    assert result.usage['prompt_count'] == 0 and result.usage['cost_per_prompt_usd'] is None
    assert result.usage['quota_unavailable_reason'] == 'missing_snapshot'
    assert len(list(result.directory.glob('*-result.json'))) == 1
    assert options[0][0] == 'synthetic' and options[0][1]['prefix'] == 'allure'


def test_quota_failure_is_unavailable_and_storage_failure_fails_operation(tmp_path):
    context, test, config = fixture(tmp_path)
    def fail():
        raise RuntimeError('private-quota-token')
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                   quota_fetcher=fail, storage=Storage(fail=True), runner=fake_runner())
    assert result.exit_code == 1 and not result.persisted
    assert result.usage['quota_unavailable_reason'] == 'missing_snapshot'
    assert 'private-quota-token' not in json.dumps(result.usage)
    assert 'private-provider-error' not in result.execution.output


def test_bad_optional_csv_keeps_primary_usage_and_records_finalization_failure(tmp_path):
    context, test, config = fixture(tmp_path)
    config['TOKEN_USAGE_CSV'] = tmp_path
    config['TOKEN_USAGE_CSV_ENABLED'] = True
    storage = Storage()
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                         storage=storage, runner=fake_runner())
    assert result.exit_code == 1 and result.persisted
    assert result.manifest['status'] == 'failed'
    assert 'token-usage.csv' in result.manifest['files']
    assert 'usage-summary.json' in result.manifest['files']
    assert len(storage.calls) == 1
    results = [json.loads(p.read_text()) for p in result.directory.glob('*-result.json')]
    assert any(row['name'] == 'usage_csv' and row['status'] == 'broken' for row in results)


def test_returned_usage_is_the_single_frozen_persisted_report(tmp_path, monkeypatch):
    context, test, config = fixture(tmp_path)
    calls = []
    original = recorded.SuiteUsage.report
    def report(self):
        calls.append(None)
        return {**original(self), 'reset_countdown_seconds': len(calls)}
    monkeypatch.setattr(recorded.SuiteUsage, 'report', report)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                        storage=Storage(), runner=fake_runner())
    assert len(calls) == 1
    assert result.usage == json.loads((result.directory / 'usage-summary.json').read_text())


def test_recorded_evidence_always_scrubs_configured_and_dynamic_credentials(tmp_path):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    context, test, config = fixture(tmp_path)
    config.update(PROVIDER_SECRET='provider-private', EXTENSION_SECRET='extension-private', LOG_CONTENT=False)
    def run(context, tests, **hooks):
        hooks['before_run']()
        outcome = TestResult(test, 'failed', .1, 'provider-private extension-private trace evidence')
        result = IntegrationRunResult(1, (outcome,), '', False)
        hooks['on_result'](outcome)
        hooks['after_run'](result)
        return result
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                                              storage=Storage(), runner=run, secret_values=(config['PROVIDER_SECRET'], config['EXTENSION_SECRET']))
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert row['statusDetails']['trace'] == '[REDACTED] [REDACTED] trace evidence'
    assert result.persisted and result.exit_code == 1
    boundary = EmissionBoundary(ContentPolicy(True))
    boundary.register_secret('trace evidence')
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                         storage=Storage(), runner=run, evidence_boundary=boundary)
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert '[REDACTED]' in row['statusDetails']['trace']
    assert 'trace evidence' not in row['statusDetails']['trace']


def test_real_runner_setup_failure_persists_failed_run_without_starting_pytest(tmp_path, monkeypatch):
    from api_agent.testing import integration
    context, test, config = fixture(tmp_path)
    context.base_root = context.owner_root
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: [test])
    def no_process(*args, **kwargs):
        pytest.fail('Failed suite preparation must not start a test child')
    monkeypatch.setattr(integration.subprocess, 'Popen', no_process)
    def prepare(usage):
        raise RuntimeError('private-setup-password')
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                                               storage=Storage(), prepare=prepare)
    assert result.exit_code == 1 and result.persisted
    assert result.manifest['status'] == 'failed'
    assert result.execution.tests[0].status == 'error'
    assert 'private-setup-password' not in result.execution.output
    assert {'token-usage.csv', 'usage-summary.json'} <= result.manifest['files'].keys()
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert row['status'] == 'broken'
    assert 'private-setup-password' not in json.dumps(row)


def test_real_recorded_collision_and_parameter_name_join(tmp_path):
    from api_agent.testing.collection import collect_tests
    base, owner = tmp_path / 'a/same', tmp_path / 'b/same'
    base.mkdir(parents=True)
    suite = owner / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_param.py').write_text(
        "import pytest\n@pytest.mark.parametrize('value', [1], ids=['value::nested'])\n"
        "def test_value(value): assert value == 1\n")
    context = SimpleNamespace(base_root=base, owner_root=owner,
                              suite_paths=lambda mode: [suite])
    tests = collect_tests(context, 'integration')
    assert tests[0].repo == 'pack'
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'results', 'TEST_PARALLEL_JOBS': 0}
    storage = Storage()
    result = recorded.run_recorded_integration(context, tests, config, accepted=True, storage=storage)
    assert result.exit_code == 0 and result.persisted and len(storage.calls) == 1
    assert result.identity.repo == result.execution.tests[0].identity.repo == 'pack'
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert row['name'] == 'test_value[value::nested]'
    assert row['fullName'] == tests[0].node_id


def test_foreign_base_and_provider_results_record_without_identity_collisions(tmp_path):
    histories = []
    for checkout in ('original', 'relocated'):
        context, pack_test, config = fixture(tmp_path / checkout)
        provider_root = context.base_root / 'providers/gpt'
        base_test = TestIdentity('base::test.py::test_same', 'base', 'test.py', 'test.py::test_same')
        provider_test = TestIdentity('provider:test', 'provider:' + str(provider_root),
                                     'test.py', 'test.py::test_same', root=provider_root)
        result = recorded.run_recorded_integration(context, [pack_test, base_test, provider_test], config,
            accepted=True, storage=Storage(), runner=fake_runner('skipped'))
        assert result.exit_code == 0 and result.persisted and result.manifest['status'] == 'completed'
        rows = [json.loads(path.read_text()) for path in result.directory.glob('*-result.json')]
        assert len(rows) == 3
        assert {row['fullName'] for row in rows} == {
            pack_test.node_id, 'base::test.py::test_same', 'provider:providers/gpt::test.py::test_same'}
        assert len({row['historyId'] for row in rows}) == 3
        assert all(row['status'] == 'skipped' and row['statusDetails']['message'] == 'Test skipped' for row in rows)
        assert str(tmp_path) not in json.dumps(rows)
        histories.append({row['fullName']: row['historyId'] for row in rows})
    assert histories[0] == histories[1]


def test_debug_without_start_disables_parallel_workers(tmp_path):
    context, test, config = fixture(tmp_path)
    config.update(DEBUG=True, NO_STD_IN=True, TEST_PARALLEL_JOBS=8)
    base_runner = fake_runner()
    def runner(*args, **kwargs):
        assert kwargs['parallel_jobs'] == 0
        return base_runner(*args, **kwargs)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
        no_std_in=True, storage=Storage(), runner=runner)
    assert result.exit_code == 0 and result.persisted
    for overrides in ({'TUI_MODE': 'e2e'}, {'COLLECT_ONLY': True}):
        with pytest.raises(PermissionError):
            recorded.validate_recorded_integration([test], config | overrides, accepted=True)
    with pytest.raises(PermissionError):
        recorded.validate_recorded_integration([test], config, accepted=False)


@pytest.mark.parametrize('debug', [False, True])
@pytest.mark.parametrize('start', [None, '', 'False', 'True', 'E2E'])
def test_terminal_recording_accepts_start_or_debug(tmp_path, debug, start):
    _, test, config = fixture(tmp_path)
    config.update(DEBUG=debug, NO_STD_IN=True)
    if start is not None:
        config['START'] = start
    if debug or start in ('True', 'E2E'):
        assert recorded.validate_recorded_integration([test], config, accepted=True) == (test,)
    else:
        with pytest.raises(PermissionError, match='Start acceptance'):
            recorded.validate_recorded_integration([test], config, accepted=True)


def test_worker_quota_observation_is_frozen_with_cost_shares_and_start_output(tmp_path):
    from api_agent.testing.contracts import RunnerEvent
    context,test,config=fixture(tmp_path);lines=[]
    now=datetime.now(timezone.utc);reset=now+timedelta(days=3)
    before=QuotaSnapshot(10,'weekly:opaque',reset,now)
    after=QuotaSnapshot(11,'weekly:opaque',reset,now+timedelta(seconds=2))
    def run(context,tests,**hooks):
        hooks['before_run']()
        usage_payload={'call_id':'00000000-0000-0000-0000-000000000001','selection':'gpt-example','usage':None}
        hooks['on_event'](RunnerEvent('inference_usage',identity=test,text=json.dumps(usage_payload)))
        usage_payload.update(usage={'input_tokens':2,'output_tokens':1,'cached_input_tokens':0,'reasoning_tokens':0},
            quote={'list_cost_usd':.25,'payment':{'usd':100,'quota_seconds':604800,'period':'calendar_month'}})
        hooks['on_event'](RunnerEvent('inference_usage',identity=test,text=json.dumps(usage_payload)))
        for phase,snapshot in [('before',before),('after',after)]:
            from api_agent.telemetry.usage import _snapshot_json
            hooks['on_event'](RunnerEvent('quota_snapshot',identity=test,text=json.dumps({'phase':phase,'snapshot':_snapshot_json(snapshot)})))
        result=IntegrationRunResult(0,(TestResult(test,'passed'),),'')
        hooks['on_result'](result.tests[0]);hooks['after_run'](result)
        return result
    result=recorded.run_recorded_integration(context,[test],config,accepted=True,storage=Storage(),runner=run,usage_output=lines.append)
    assert result.persisted and result.usage['cost_basis']=='list_price'
    assert result.usage['test_costs'][test.id]['estimated_cost_usd']==result.usage['estimated_cost_usd']
    assert result.usage['raw_estimated_cost_usd'] > 0
    assert result.usage['test_costs'][test.id]['list_cost_usd'] == .25
    assert result.usage['test_costs'][test.id]['raw_quota_cost_usd'] > 0
    assert lines[1]=='Starting quota used: 10.000%'
    row=json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    artifact=next(a for a in row['attachments'] if a['name']=='Test cost estimate')
    assert json.loads((result.directory/artifact['source']).read_bytes())['basis']=='list_price'
    assert result.elapsed>=0


def test_real_worker_quota_events_reach_recorded_suite(tmp_path):
    context,test,config=fixture(tmp_path)
    root=context.owner_root;root.mkdir()
    suite=root/'tests/integration';suite.mkdir(parents=True)
    context.suite_paths=lambda mode:(suite,)
    testfile=suite/'test_quota.py'
    testfile.write_text('''from datetime import datetime,timedelta,timezone
from api_agent.telemetry.usage import QuotaSnapshot
from api_agent.testing.quota_events import publish

def test_quota():
    now=datetime.now(timezone.utc)
    reset=now+timedelta(days=3)
    publish('before',QuotaSnapshot(20,'window:opaque',reset,now))
    publish('after',QuotaSnapshot(21,'window:opaque',reset,now+timedelta(seconds=1)))
''')
    from api_agent.testing.collection import collect_tests
    selected,=collect_tests(context,'integration',config=config)
    result=recorded.run_recorded_integration(context,[selected],config,accepted=True,storage=Storage())
    assert result.execution.exit_code==0 and result.persisted
    assert result.usage['pct_used_before']==20 and result.usage['pct_point_delta']==1


def test_all_accounting_exports_scrub_parameterized_identifiers(tmp_path):
    from dataclasses import replace
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    context, test, config = fixture(tmp_path)
    test = replace(test, id=test.id+'[registered-secret]', node_id=test.node_id+'[registered-secret]')
    config.update(TOKEN_USAGE_CSV_ENABLED=True, TOKEN_USAGE_CSV=tmp_path/'external.csv')
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('registered-secret',))
    def prepare(usage):
        usage.add_usage(provider='generic',model='model',session_id=test.id,input_tokens=1,output_tokens=1)
    result = recorded.run_recorded_integration(context,[test],config,accepted=True,storage=Storage(),runner=fake_runner(),prepare=prepare,evidence_boundary=boundary)
    assert result.persisted
    assert 'registered-secret' not in json.dumps(result.usage)
    for path in result.directory.iterdir():
        assert b'registered-secret' not in path.read_bytes(), path.name
    assert b'registered-secret' not in (tmp_path/'external.csv').read_bytes()
    artifact=json.loads((result.directory/'accounting.json').read_text())
    assert '[REDACTED]' in artifact['tests'][0]['test_id']
    assert len(next(csv.reader((result.directory/'token-usage.csv').read_text().splitlines()))) == 10


def test_long_parameterized_id_does_not_fail_optional_accounting(tmp_path):
    from dataclasses import replace
    context, test, config = fixture(tmp_path)
    suffix = '[' + 'x' * 300 + ']'
    test = replace(test, id=test.id+suffix, node_id=test.node_id+suffix)
    result = recorded.run_recorded_integration(context,[test],config,accepted=True,storage=Storage(),runner=fake_runner())
    assert result.exit_code == 0 and result.persisted
    artifact = json.loads((result.directory/'accounting.json').read_text())
    assert artifact['tests'][0]['test_id'] == test.id
    assert artifact['tests'][0]['node_id'] == test.node_id


@pytest.mark.parametrize('scenario,reason', [('reset','changed_window'),
    ('reset_higher_used','changed_window'), ('decrease','negative_delta'), ('boundary','reset_boundary_crossed')])
def test_reset_allure_keeps_null_quota_cost_and_positive_token_estimates(tmp_path, scenario, reason):
    from api_agent.telemetry.accounting import compatibility
    from api_agent.telemetry.history import HistoryCache
    from api_agent.telemetry.estimation import predictions_for
    from api_agent.providers.rates import bundled_rate_card
    from test_historical_pricing import _evidence
    context, test, config = fixture(tmp_path)
    instant = datetime(2026, 10, 1, tzinfo=timezone.utc)
    reset = instant + timedelta(seconds=30)
    before = QuotaSnapshot(80, 'window', reset, instant, 1)
    after = QuotaSnapshot(90 if scenario == 'reset_higher_used' else 1, 'window',
        reset + timedelta(days=1) if scenario.startswith('reset') else reset,
        instant + timedelta(seconds=31 if scenario == 'boundary' else 5), 1)
    snapshots = iter((before, after))
    def prepare(usage):
        quote = {'list_cost_usd':.0001, 'payment':{'usd':100,'quota_seconds':86400,'period':'quota'}}
        evidence = _evidence()
        evidence.update(usage={'input_tokens':1000,'output_tokens':0,'cached_input_tokens':0,'reasoning_tokens':0}, quote=quote)
        usage.record_inference(evidence['call_id'], test.id, evidence)
        usage.add_usage(provider='synthetic', model='gpt-4.1-nano', session_id=test.id, input_tokens=1000, output_tokens=0)
        usage.add_cost(evidence['call_id'], test.id, quote)
        usage.record_prompt(turn_id=test.id, prompt_id='prompt', kind='initial')
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
        quota_fetcher=lambda:next(snapshots), prepare=prepare, runner=fake_runner(), storage=Storage())
    assert result.persisted and result.exit_code == 0
    summary = json.loads((result.directory / 'usage-summary.json').read_bytes())
    artifact = json.loads((result.directory / 'accounting.json').read_bytes())
    assert summary['pct_point_delta'] is None and summary['raw_quota_cost_usd'] is None
    assert summary['quota_unavailable_reason'] == reason
    assert summary['estimated_cost_usd'] == pytest.approx(.0001)
    assert summary['test_costs'][test.id]['estimated_cost_usd'] == pytest.approx(.0001)
    assert summary['test_costs'][test.id]['raw_quota_cost_usd'] is None
    assert artifact['quota']['pct_point_delta'] is None
    assert artifact['quota']['before']['pct_used'] == 80
    assert artifact['quota']['after']['pct_used'] == after.pct_used
    assert artifact['tests'][0]['raw_quota_cost_usd'] is None
    assert artifact['tests'][0]['stabilized_estimate']['estimated_cost_usd'] == pytest.approx(.0001)
    attached = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    attachment = next(a for a in attached['attachments'] if a['name'] == 'Test cost estimate')
    assert json.loads((result.directory / attachment['source']).read_bytes())['raw_quota_cost_usd'] is None
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    cache.ingest('origin', result.manifest, (result.directory / 'accounting.json').read_bytes())
    prediction = predictions_for([test], cache.records('origin'), bundled_rate_card(),
        {test.id: artifact['tests'][0]['source_fingerprint']}, compatibility(config))[test.id]
    assert prediction['sample_count'] == 0 and prediction['estimated_cost_usd'] is None
    cache.close()
