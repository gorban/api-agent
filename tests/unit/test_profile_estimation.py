"""Profile predictions and immutable accounting retain their effective context."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.accounting import build_accounting, compatibility
from api_agent.telemetry.estimation import EstimateSnapshot
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.contracts import TestIdentity
from api_agent.testing.cost_history import estimator_for
from api_agent.testing.profile_estimation import ProfileEstimator
from test_historical_estimation import compat, records


def test_profile_predictions_match_each_actual_configuration(tmp_path, monkeypatch):
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    tests = [TestIdentity('t', tmp_path.name, 'test.py', 'test.py::test[one]'),
             TestIdentity('other', tmp_path.name, 'test.py', 'test.py::test[two]')]
    configurations = {'t': dict(compat(), LLM_DEFAULT_MODEL='first', TEST_COST_HISTORY_DAYS=30),
                      'other': dict(compat(), LLM_DEFAULT_MODEL='second', TEST_COST_HISTORY_DAYS=30)}
    history = records()
    for _, artifact in history:
        second = deepcopy(artifact['tests'][0])
        second.update(test_id='other', node_id=tests[1].node_id)
        artifact['tests'].append(second)
        for row in artifact['tests']:
            settings = compatibility(configurations[row['test_id']])
            row['compatibility'] = settings
            row['calls'][0]['quote']['accounting']['compatibility'] = settings
    class Cache:
        def records(self, origin):
            return history
    monkeypatch.setattr('api_agent.testing.cost_history.cost_config', lambda context, config: dict(config))
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    before = deepcopy(configurations)
    picker = estimator_for(context, tests, {}, cache=Cache(), rate_card=bundled_rate_card(),
                           test_configs=configurations, now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert isinstance(picker, ProfileEstimator)
    assert all(row['sample_count'] == 3 for row in picker.predictions.values())
    assert all(row['estimated_cost_usd'] > 0 for row in picker.predictions.values())
    assert len(picker.records) == 3
    assert configurations == before


def test_accounting_uses_case_configuration_without_changing_application(tmp_path):
    test = TestIdentity('t', tmp_path.name, 'test.py', 'test.py::test')
    application = dict(compat(), LLM_DEFAULT_MODEL='application', _COST_PACK_IDENTITY='b' * 64)
    specialized = dict(application, LLM_DEFAULT_MODEL='workflow')
    identity = SimpleNamespace(run_id=str(uuid4()))
    usage = SuiteUsage(identity.run_id)
    artifact = build_accounting(identity, (test,), application, usage, (),
                                test_configs={'t': specialized})
    assert artifact['tests'][0]['compatibility']['LLM_DEFAULT_MODEL'] == 'workflow'
    assert application['LLM_DEFAULT_MODEL'] == 'application'


def test_profile_snapshot_combines_histories_and_forwards_owned_operations():
    operations = []
    class Child(EstimateSnapshot):
        def synchronize(self):
            operations.append('sync')
            return self
        def cancel_synchronization(self):
            operations.append('cancel')
        def ingest(self, directory, manifest):
            operations.append((directory, manifest))
        def close(self):
            operations.append('close')
    first = Child({'one': {'estimated_cost_usd': 1}}, records=[({'run': 1}, {})],
                  card={'revision': 1}, overlap_records=[({'run': 2}, {})])
    second = EstimateSnapshot({'two': {'estimated_cost_usd': 2}}, records=[({'run': 1}, {})],
                              card={'revision': 2})
    picker = ProfileEstimator((first, second))
    assert picker.card is None
    assert picker.total(('one', 'two'))['estimated_cost_usd'] == 3
    assert len(picker.records) == 1 and len(picker.overlap_records) == 1
    refreshed = picker.synchronize()
    assert refreshed.predictions == picker.predictions
    picker.cancel_synchronization()
    picker.ingest('directory', {'run': 3})
    ProfileEstimator((second,)).ingest('directory', {})
    picker.close()
    assert operations == ['sync', 'cancel', ('directory', {'run': 3}), 'close']


def test_dormant_cap_picker_and_recorded_preflight_share_exact_per_case_cost_maps(tmp_path, monkeypatch):
    import pytest
    from test_test_profiles import configuration, CASES, ROOT
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.tui.test_configuration import profile_cost_configs
    from api_agent.testing import recorded
    from api_agent.providers import rates
    from api_agent.testing import cost_history, workflow_admission
    from test_bedrock_history_entrypoint import history_context
    context, _, _, _, _ = history_context.__wrapped__(tmp_path)
    from dataclasses import replace
    cases = [replace(case, file=case.node_id.split('::', 1)[0], root=None) for case in (CASES[0], CASES[-1])]
    captured = []
    class BeforeProviderSetup(Exception):
        pass
    def estimator(context, tests, config, **kwargs):
        captured.append(deepcopy(kwargs['test_configs']))
        raise BeforeProviderSetup
    monkeypatch.setattr(cost_history, 'estimator_for', estimator)
    monkeypatch.setattr(rates, 'freeze_pricing', lambda *args: bundled_rate_card())
    # This witness stops at estimate construction, before execution admission,
    # artifact creation, provider setup, storage or quota acquisition.
    monkeypatch.setattr(workflow_admission, 'capture_workflow_admissions', lambda *args, **kwargs: {})
    picker = []
    applications = []
    for cap in (256, 512):
        result = configuration(LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=cap)
        before = deepcopy(result.values), deepcopy(result.fields)
        picker.append(profile_cost_configs(cases, result, context))
        application = dict(result.values, SKIP_ALLURE_REPORTING=False,
                           ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path / str(cap))
        applications.append(deepcopy(application))
        with pytest.raises(BeforeProviderSetup):
            recorded.run_recorded_integration.__wrapped__(context, cases, application,
                accepted=True, config_fields=result.fields)
        assert application == applications[-1]
        assert (result.values, result.fields) == before
        assert not (tmp_path / str(cap)).exists()
    ordinary, workflow = (case.id for case in cases)
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    for index in range(2):
        for case in cases:
            assert captured[index][case.id][key] == picker[index][case.id][key]
            assert captured[index][case.id]['_COST_REQUEST_SETTINGS'] == picker[index][case.id]['_COST_REQUEST_SETTINGS']
    assert picker[0][ordinary][key] is picker[1][ordinary][key] is None
    assert picker[0][ordinary]['_COST_REQUEST_SETTINGS'] == picker[1][ordinary]['_COST_REQUEST_SETTINGS']
    assert picker[0][workflow][key] == 256 and picker[1][workflow][key] == 512
    assert picker[0][workflow]['_COST_REQUEST_SETTINGS'][key] != picker[1][workflow]['_COST_REQUEST_SETTINGS'][key]


def test_existing_history_prediction_survives_dormant_cap_change_with_real_source_identity(tmp_path):
    from dataclasses import replace
    from test_test_profiles import CASES
    from test_bedrock_history_entrypoint import history_context
    from api_agent.tui.test_configuration import profile_cost_configs
    from api_agent.telemetry.accounting import source_fingerprint
    context, config, _, history, cache = history_context.__wrapped__(tmp_path)
    test = replace(CASES[0], file=CASES[0].node_id.split('::', 1)[0], root=None)
    settings = []
    for cap in (256, 512):
        effective = profile_cost_configs([test], SimpleNamespace(values=dict(config,
            LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS=cap)), context)[test.id]
        settings.append(effective)
    original = history[0][1]['tests'][0]
    original.update(test_id=test.id, node_id=test.node_id,
                    source_fingerprint=source_fingerprint(test, context=context),
                    compatibility=compatibility(settings[0]))
    assert original['source_fingerprint'] is not None
    original['calls'][0]['quote']['accounting']['compatibility'] = compatibility(settings[0])
    before = deepcopy(history)
    predictions = [estimator_for(context, [test], values, cache=cache,
        now=datetime(2026, 10, 25, 1, tzinfo=timezone.utc)).predictions[test.id]
        for values in settings]
    assert predictions[0]['sample_count'] == predictions[1]['sample_count'] == 1
    assert predictions[0]['estimated_cost_usd'] == predictions[1]['estimated_cost_usd']
    assert predictions[0]['estimated_cost_usd'] > 0
    assert history == before
