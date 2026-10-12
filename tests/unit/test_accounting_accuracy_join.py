"""Integrated calibration selection stays scoped to the displayed test."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.accounting import compatibility
from api_agent.testing.cost_history import HistoricalEstimator
from test_historical_estimation import compat, records


def test_unrelated_qualified_models_do_not_hide_the_target_calibration(monkeypatch):
    history = records()
    config = dict(compat(), TEST_COST_HISTORY_DAYS=30,
                  _COST_QUOTA_ALLOCATION_ID='c' * 64)
    settings = compatibility(config)
    for _, artifact in history:
        row = artifact['tests'][0]
        row['compatibility'] = settings
        row['calls'][0]['quote']['accounting']['compatibility'] = settings
    unrelated = deepcopy(history)
    for _, artifact in unrelated:
        artifact['run_id'] = str(uuid4())
        row = artifact['tests'][0]
        row['node_id'] = 'other.py::test_other'
        row['calls'][0]['quote']['accounting']['actual_model'] = 'gpt-4.1-mini'
        for phase in ('before', 'after'):
            artifact['quota'][phase]['allocation_id'] = 'd' * 64
    class Cache:
        def records(self, origin):
            return history + unrelated
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    target = SimpleNamespace(id='t', node_id='test.py::test[one]')

    picker = HistoricalEstimator((target,), config, Cache(), 'origin', 'repo',
                                 bundled_rate_card(), now=datetime(2026, 10, 10, tzinfo=timezone.utc))

    assert picker.predictions['t']['basis'] == 'historical_quota'
    assert picker.predictions['t']['calibration']['sample_count'] == 3
    assert set(picker.predictions['t']['calibration']['allocations']) == {'c' * 64}


def test_out_of_window_overlap_is_evidence_without_an_old_cost_sample(monkeypatch):
    history = records()
    config = dict(compat(), TEST_COST_HISTORY_DAYS=7,
                  _COST_QUOTA_ALLOCATION_ID='c' * 64)
    settings = compatibility(config)
    for _, artifact in history:
        row = artifact['tests'][0]
        row['compatibility'] = settings
        row['calls'][0]['quote']['accounting']['compatibility'] = settings
    older = deepcopy(history[0])
    older[1]['run_id'] = str(uuid4())
    history[0][1]['quota']['before']['captured_at'] = '2026-09-29T00:00:00Z'
    older[0]['finished_at'] = '2026-09-30T01:00:00Z'
    older[1]['quota']['before']['captured_at'] = '2026-09-30T00:00:00Z'
    older[1]['quota']['after']['captured_at'] = '2026-09-30T01:00:00Z'
    class Cache:
        def records(self, origin):
            return history + [older]
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    target = SimpleNamespace(id='t', node_id='test.py::test[one]')

    picker = HistoricalEstimator((target,), config, Cache(), 'origin', 'repo',
                                 bundled_rate_card(), now=datetime(2026, 10, 8, tzinfo=timezone.utc))

    assert len(picker.records) == 3 and len(picker.overlap_records) == 4
    assert picker.predictions['t']['basis'] == 'historical_list_price'


def test_native_cache_is_queried_with_time_bounds_and_missing_cache_is_inert(tmp_path, monkeypatch):
    from api_agent.telemetry.history import HistoryCache
    import api_agent.testing.cost_history as module
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    # The full-suite isolation fixture substitutes a cache factory. Use this
    # actual temporary cache to exercise the native indexed-query contract.
    monkeypatch.setattr(module, 'HistoryCache', HistoryCache)
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    try:
        picker = HistoricalEstimator((), {}, cache, 'origin', 'repo', None, now=now)
        assert picker.records == [] and picker.overlap_records == []
        assert picker.total(())['estimated_cost_usd'] is None
        absent = HistoricalEstimator((), {}, None, 'origin', 'repo', None, now=now)
        assert absent.records == [] and absent.synchronize() is absent
    finally:
        cache.close()
