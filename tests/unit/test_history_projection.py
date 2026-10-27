"""Estimate-local query reuse preserves fresh native evidence and clock policy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.estimation import EstimateSnapshot, reported_charge
from api_agent.telemetry.history import HistoryCache
from api_agent.testing.cost_history import HistoricalEstimator
from api_agent.testing.history_projection import HistoryProjection, encode_history
from api_agent.testing.profile_estimation import ProfileEstimator
from test_transport_cost_pooling import IGNORED, histories


def test_query_memo_is_scoped_complete_and_returns_independent_native_rows():
    original = [({'run_id': 'one'}, {'tests': [{'calls': [], 'cost': None}]})]
    cache = SimpleNamespace(records=Mock(return_value=original), ingest=Mock(),
                            bound_window=Mock(return_value='window'))
    projection = HistoryProjection(cache)
    assert projection.bound_window('origin', 'scope') == 'window'
    cache.bound_window.assert_called_once_with('origin', 'scope')
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    options = dict(since=now - timedelta(days=1), until=now, run_id='one')
    with projection.read():
        first = projection.records('origin', **options)
        first[0][1]['tests'][0]['calls'].append('changed')
        original[0][1]['tests'][0]['cost'] = 42
        second = projection.records('origin', **options)
        assert second[0][1]['tests'][0] == {'calls': [], 'cost': None}
        assert type(second) is list and type(second[0]) is tuple
        assert type(second[0][1]) is dict and type(second[0][1]['tests']) is list
        cache.records.assert_called_once_with('origin', **options)
        for changed in ({'origin': 'other'}, {'since': None}, {'until': None}, {'run_id': None}):
            query = dict(origin='origin', **options)
            query.update(changed)
            projection.records(**query)
        assert cache.records.call_count == 5
        with projection.read():
            assert projection.records('origin', **options) == second
        projection.ingest('new evidence')
        assert projection.records('origin', **options)[0][1]['tests'][0]['cost'] == 42
        assert cache.records.call_count == 6
    assert projection.now is None and projection._queries is None
    with projection.read():
        projection.records('origin', **options)
    assert cache.records.call_count == 7
    projection.records('origin', **options)
    projection.records('origin', **options)
    assert cache.records.call_count == 9
    cache.ingest.assert_called_once_with('new evidence')


def test_failed_query_is_not_cached_and_scope_closes_after_failure():
    cache = SimpleNamespace(records=Mock(side_effect=[OSError('unavailable'), []]))
    projection = HistoryProjection(cache)
    with pytest.raises(RuntimeError):
        with projection.read():
            with pytest.raises(OSError):
                projection.records()
            assert projection.records() == []
            assert projection.records() == []
            assert cache.records.call_count == 2
            raise RuntimeError('leave scope')
    assert projection._queries is None and projection.now is None


@pytest.mark.parametrize('billing', ['subscription', 'api_key'])
def test_owned_json_path_matches_native_math_without_aliases(tmp_path, monkeypatch, billing):
    history = histories(billing)
    if billing == 'api_key':
        for _, artifact in history:
            call = artifact['tests'][0]['calls'][0]
            call['quote']['accounting']['billing'] = {'usd': .125, 'request_id': 'request', 'generation_id': None}
    original = deepcopy(history)
    monkeypatch.setattr('api_agent.testing.cost_history.HistoryCache', HistoryCache)
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    monkeypatch.setattr(cache, 'records', Mock(side_effect=lambda *args, **kwargs: deepcopy(history)))
    monkeypatch.setattr('api_agent.telemetry.accounting.compatibility', lambda config: history[0][1]['tests'][0]['compatibility'])
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    config = {'TEST_COST_HISTORY_DAYS': 40, '_COST_IGNORED_SETTINGS': IGNORED}
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    native = HistoricalEstimator([test], config, cache, 'origin', 'repo', bundled_rate_card(), now=now)
    projection = HistoryProjection(cache)
    with projection.read():
        first = HistoricalEstimator([test], config, projection, 'origin', 'repo', bundled_rate_card(), now=now)
        second = HistoricalEstimator([test], config, projection, 'origin', 'repo', bundled_rate_card(), now=now)
        combined = ProfileEstimator([first, second])
    assert first.predictions == native.predictions == combined.predictions
    assert first.complete(history[0][1]) == native.complete(history[0][1])
    assert first.records == native.records and type(first.records[0]) is tuple
    assert first.predictions['t']['estimated_cost_usd'] is not None
    if billing == 'api_key':
        assert reported_charge(first.records[0][1]['tests'][0]) == .125
    first.records[0][1]['tests'][0]['calls'].clear()
    assert second.records == combined.records == original
    assert history == original
    ordinary = EstimateSnapshot({}, records=history)
    history[0][1]['tests'].clear()
    assert ordinary.records == original
    clone = EstimateSnapshot({})
    encoded = encode_history(original)
    clone._set_history_json(encoded, encoded)
    clone.records[0][1]['tests'].clear()
    assert clone.overlap_records == original
    cache.close()


@pytest.mark.parametrize('explicit', [False, True])
def test_profile_refresh_has_new_query_scope_and_preserves_clock_policy(tmp_path, monkeypatch, explicit):
    first_now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    next_now = first_now + timedelta(days=1)
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    clock = Mock(return_value=first_now)
    monkeypatch.setattr('api_agent.testing.history_projection.datetime', SimpleNamespace(now=clock))
    history = histories()
    monkeypatch.setattr('api_agent.testing.cost_history.HistoryCache', HistoryCache)
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    query = Mock(side_effect=lambda *args, **kwargs: deepcopy(history))
    monkeypatch.setattr(cache, 'records', query)
    projection = HistoryProjection(cache)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    storage = SimpleNamespace(accounting_history=lambda *args, **kwargs: ())
    with projection.read():
        children = [HistoricalEstimator([test], {'TEST_COST_HISTORY_DAYS': 40}, projection,
            'origin', 'repo', bundled_rate_card(), storage=storage,
            now=first_now if explicit else None) for _ in range(2)]
        snapshot = ProfileEstimator(children)
    assert query.call_count == 2
    assert all(child.now == first_now and child._fixed_now is explicit for child in children)
    clock.return_value = next_now
    refreshed = snapshot.synchronize()
    assert query.call_count == 4
    assert projection._queries is None
    assert all(child.now == (first_now if explicit else next_now) for child in refreshed.children)
    assert all(child.now == first_now for child in children)
    cache.close()


def test_real_profile_factory_validates_shared_queries_once_per_build(tmp_path, monkeypatch):
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.cost_history import estimator_for
    monkeypatch.setattr('api_agent.testing.cost_history.HistoryCache', HistoryCache)
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    query = Mock(return_value=[])
    monkeypatch.setattr(cache, 'records', query)
    monkeypatch.setattr('api_agent.testing.cost_history.cost_config', lambda context, values: dict(values))
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    tests = [TestIdentity(name, 'base', 'test.py', name) for name in ('first', 'second')]
    configs = {test.id: {'LLM_DEFAULT_MODEL': test.id} for test in tests}
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    first = estimator_for(context, tests, {}, cache=cache, rate_card=bundled_rate_card(),
                          test_configs=configs, now=now)
    assert isinstance(first, ProfileEstimator)
    assert query.call_count == 2, query.call_args_list
    assert first.children[0].cache is first.children[1].cache
    assert first.children[0].cache._queries is None
    second = estimator_for(context, tests, {}, cache=cache, rate_card=bundled_rate_card(),
                           test_configs=configs, now=now)
    assert query.call_count == 4
    assert first.children[0].cache is not second.children[0].cache
    assert all(row['estimated_cost_usd'] is None for row in second.predictions.values())
    cache.close()


def test_projection_owner_excludes_outside_reads_and_ingests():
    import threading
    events = []
    rows = [({}, {'value': 0})]
    cache = SimpleNamespace(records=Mock(side_effect=lambda *args, **kwargs: deepcopy(rows)),
                            ingest=lambda: events.append('ingest'))
    projection = HistoryProjection(cache)
    started, finished = threading.Event(), threading.Event()
    def outsider():
        started.set()
        projection.ingest()
        projection.records()
        finished.set()
    with projection.read():
        projection.records()
        thread = threading.Thread(target=outsider)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(.05)
        assert events == []
        assert cache.records.call_count == 1
    thread.join(1)
    assert finished.is_set() and events == ['ingest']
    assert cache.records.call_count == 2


def test_prediction_matching_skips_unrelated_rows_before_normalizing(monkeypatch):
    import api_agent.telemetry.estimation as estimation
    history = histories()
    for _, artifact in history:
        unrelated = deepcopy(artifact['tests'][0])
        unrelated['node_id'] = 'other.py::other'
        artifact['tests'].extend(deepcopy(unrelated) for _ in range(10))
    normalize = Mock(wraps=estimation.matching_compatibility)
    monkeypatch.setattr(estimation, 'matching_compatibility', normalize)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    expected = history[0][1]['tests'][0]['compatibility']
    result = estimation.predictions_for([test], history, bundled_rate_card(), {'t': 'hash'},
                                        expected, ignored_settings=IGNORED)['t']
    assert result['sample_count'] == 3
    assert result['estimated_cost_usd'] == pytest.approx(sum(
        estimation.priced(artifact['tests'][0], bundled_rate_card()) for _, artifact in history) / 3)
    assert normalize.call_count == 4  # one target plus three matching source/node rows


@pytest.mark.parametrize('allocated', [False, True])
@pytest.mark.parametrize('matches', [False, True])
def test_quota_policy_matching_only_runs_for_an_allocated_account(tmp_path, monkeypatch, allocated, matches):
    import api_agent.testing.cost_history as cost_history
    history = histories()
    monkeypatch.setattr('api_agent.testing.cost_history.HistoryCache', HistoryCache)
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    monkeypatch.setattr(cache, 'records', Mock(side_effect=lambda *args, **kwargs: deepcopy(history)))
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    settings = deepcopy(history[0][1]['tests'][0]['compatibility'])
    if not matches:
        settings['LLM_DEFAULT_MODEL'] = 'other'
    monkeypatch.setattr('api_agent.telemetry.accounting.compatibility', lambda config: settings)
    normalize = Mock(wraps=cost_history.matching_compatibility)
    monkeypatch.setattr(cost_history, 'matching_compatibility', normalize)
    config = {'TEST_COST_HISTORY_DAYS': 40, '_COST_IGNORED_SETTINGS': IGNORED}
    if allocated:
        config['_COST_QUOTA_ALLOCATION_ID'] = 'c' * 64
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    snapshot = HistoricalEstimator([test], config, cache, 'origin', 'repo', bundled_rate_card(),
                                   now=datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert (snapshot.predictions['t']['estimated_cost_usd'] is not None) is matches
    assert (normalize.call_count > 0) is allocated
    cache.close()


def test_cooperative_query_cancel_is_observed_between_validated_records(tmp_path):
    import threading
    from api_agent.testing.history_projection import ProjectionCancelled, cancellable_projection, check_projection
    from test_historical_cache import fixture
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    first, payload = fixture()
    cache.ingest('origin', first, payload)
    cancel = threading.Event()
    checks = []
    def stop():
        checks.append('row')
        cancel.set()
        check_projection()
    with cancellable_projection(cancel):
        with pytest.raises(ProjectionCancelled):
            cache.records('origin', _check=stop)
    assert checks == ['row']
    assert len(cache.records('origin')) == 1
    cache.close()


def test_cancelled_profile_build_is_not_an_unavailable_estimate(tmp_path, monkeypatch):
    import threading
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.cost_history import estimator_for
    from api_agent.testing.history_projection import ProjectionCancelled, cancellable_projection, check_projection
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    monkeypatch.setattr('api_agent.testing.cost_history.HistoryCache', HistoryCache)
    monkeypatch.setattr('api_agent.testing.cost_history.cost_config', lambda context, values: dict(values))
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    cancel = threading.Event()
    def stop(*args, **kwargs):
        assert callable(kwargs['_check'])
        cancel.set()
        check_projection()
    monkeypatch.setattr(cache, 'records', stop)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    test = TestIdentity('t', 'base', 'test.py', 'test.py::test')
    with cancellable_projection(cancel):
        with pytest.raises(ProjectionCancelled):
            estimator_for(context, [test], {}, cache=cache, rate_card=bundled_rate_card(),
                          test_configs={'t': {}}, now=datetime(2026, 10, 4, tzinfo=timezone.utc))
    cache.close()


def test_cancelled_composite_disposes_already_constructed_children():
    import threading
    from api_agent.testing.history_projection import ProjectionCancelled, cancellable_projection
    first = EstimateSnapshot({})
    first.close = Mock()
    cancel = threading.Event()
    def children():
        yield first
        cancel.set()
        yield EstimateSnapshot({})
    with cancellable_projection(cancel):
        with pytest.raises(ProjectionCancelled):
            ProfileEstimator(children())
    first.close.assert_called_once_with()
