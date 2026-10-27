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
