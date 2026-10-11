"""Transport-equivalent histories pool without changing original evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from api_agent.config.declarations import schema_from_dict
from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.accounting import compatibility
from api_agent.telemetry.estimation import EstimateSnapshot, calibration, predictions_for, priced, matching_compatibility
from api_agent.testing.cost_context import cost_config
from api_agent.testing.cost_history import HistoricalEstimator, allocation_scope
from test_cost_context import context, mocked_schema
from test_historical_estimation import records
from test_historical_cache import binding_settings, binding_run


def digest(value):
    return sha256(json.dumps(value).encode()).hexdigest()


IGNORED = ('TRANSPORT',)


def histories(billing='subscription'):
    history = records()
    for index, (_, artifact) in enumerate(history):
        settings = compatibility(dict(binding_settings(), _COST_REQUEST_SETTINGS={
            'TRANSPORT': digest('responses' if index % 2 == 0 else 'completions'),
            'ROUTE': digest('same-route'), 'AUTH_MODE': digest(billing)}))
        row = artifact['tests'][0]
        row['compatibility'] = settings
        row['calls'][0]['quote']['accounting'].update(compatibility=deepcopy(settings), billing_mode=billing)
        row['calls'][0]['usage']['input_tokens'] *= index + 1
    return history


def test_declared_equivalence_keeps_original_mode_fingerprints(tmp_path, monkeypatch):
    schema = schema_from_dict({'version': 1, 'fields': {'TRANSPORT': {
        'type': 'str', 'enum': ['responses', 'completions', 'other'],
        'cost_affecting': False}}})
    mocked_schema(monkeypatch, list(schema.fields.values()))
    ctx = context(tmp_path)
    first = cost_config(ctx, {'TRANSPORT': 'responses'})
    second = cost_config(ctx, {'TRANSPORT': 'completions'})
    assert first['_COST_REQUEST_SETTINGS'] != second['_COST_REQUEST_SETTINGS']
    assert first['_COST_IGNORED_SETTINGS'] == second['_COST_IGNORED_SETTINGS'] == IGNORED


@pytest.mark.parametrize('billing', ['subscription', 'api_key'])
def test_predictions_pool_modes_without_modifying_history(billing):
    history = histories(billing)
    original = deepcopy(history)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    settings = history[0][1]['tests'][0]['compatibility']
    result = predictions_for([test], history, bundled_rate_card(), {'t': 'hash'}, settings,
                             ignored_settings=IGNORED)['t']
    assert result['sample_count'] == 3
    assert result['ignored_cost_settings'] == IGNORED
    assert result['estimated_cost_usd'] == pytest.approx(sum(priced(a['tests'][0], bundled_rate_card()) for _, a in history) / 3)
    assert history == original
    # No provider declaration means exact matching, retaining two samples.
    assert predictions_for([test], history, bundled_rate_card(), {'t': 'hash'}, settings)['t']['sample_count'] == 2


@pytest.mark.parametrize('field,value', [('ROUTE', 'different'), ('AUTH_MODE', 'api_key'),
                                       ('UNKNOWN_SETTING', 'different')])
def test_other_settings_and_unknown_modes_stay_separate(field, value):
    history = histories()
    settings = deepcopy(history[0][1]['tests'][0]['compatibility'])
    settings['_COST_REQUEST_SETTINGS'][field] = None if value is None else digest(value)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    assert predictions_for([test], history, bundled_rate_card(), {'t': 'hash'}, settings,
                           ignored_settings=IGNORED)['t']['sample_count'] == 0


def test_quota_calibration_pools_modes_but_not_accounts():
    history = histories()
    card = bundled_rate_card()
    assert calibration(history, card) == {}
    qualified = calibration(history, card, ignored_settings=IGNORED)
    assert len(qualified) == 1
    fit = next(iter(qualified.values()))
    assert fit['sample_count'] == 3
    assert fit['pct_per_usd'] == pytest.approx(3 / sum(priced(a['tests'][0], card) for _, a in history))
    current = deepcopy(history[0][1])
    current['tests'][0]['compatibility'] = deepcopy(history[1][1]['tests'][0]['compatibility'])
    current['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = deepcopy(current['tests'][0]['compatibility'])
    snapshot = EstimateSnapshot({}, records=history, card=card, ignored_settings=IGNORED)
    # Snapshot retains its own declaration even if a caller changes the map.
    result = snapshot.complete(current)['t']
    assert result['basis'] == 'historical_quota'
    assert result['ignored_cost_settings'] == IGNORED
    assert result['estimated_cost_usd'] == pytest.approx(.5)
    history[1][1]['quota']['before']['window_id'] = history[1][1]['quota']['after']['window_id'] = 'other-account'
    assert calibration(history, card, ignored_settings=IGNORED) == {}


def test_picker_and_allocation_binding_use_the_same_mode_equivalence(monkeypatch):
    history = histories()
    settings = dict(binding_settings(), _COST_REQUEST_SETTINGS=history[0][1]['tests'][0]['compatibility']['_COST_REQUEST_SETTINGS'],
                    _COST_IGNORED_SETTINGS=IGNORED)
    other = deepcopy(settings)
    other['_COST_REQUEST_SETTINGS']['TRANSPORT'] = digest('completions')
    assert allocation_scope(settings) == allocation_scope(other)
    other['UNATTENDED_USER_INDEX'] = 1
    other['UNATTENDED_AUTH']['roles']['primary'].append({'vault': 'other', 'user': 'other'})
    assert allocation_scope(settings) != allocation_scope(other)
    class Cache:
        def records(self, origin): return history
        def bound_window(self, *args): return 'window'
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    estimator = HistoricalEstimator([test], settings, Cache(), 'origin', 'repo', bundled_rate_card(),
                                    now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert estimator.predictions['t']['sample_count'] == 3
    assert estimator.predictions['t']['basis'] == 'historical_quota'
    assert estimator.complete(history[0][1])['t']['basis'] == 'historical_quota'


@pytest.mark.parametrize('value', [None, {}, {'_COST_REQUEST_SETTINGS': None}])
def test_missing_compatibility_is_never_fabricated(value):
    assert matching_compatibility(value, IGNORED) == value


def test_missing_diagnostic_transport_does_not_split_older_history():
    history = histories()
    settings = deepcopy(history[0][1]['tests'][0]['compatibility'])
    del settings['_COST_REQUEST_SETTINGS']['TRANSPORT']
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    result = predictions_for([test], history, bundled_rate_card(), {'t': 'hash'}, settings,
                             ignored_settings=IGNORED)['t']
    assert result['sample_count'] == 3
    assert result['ignored_cost_settings'] == IGNORED


def test_equivalence_snapshot_is_frozen():
    declarations = list(IGNORED)
    history = histories()
    snapshot = EstimateSnapshot({}, records=history, card=bundled_rate_card(), ignored_settings=declarations)
    declarations.clear()
    assert snapshot.complete(history[0][1])['t']['basis'] == 'historical_quota'


def test_sqlite_reopen_pools_legacy_mode_hashes_without_rewriting_artifacts(tmp_path, monkeypatch):
    from api_agent.telemetry.history import HistoryCache
    cache_path = tmp_path / 'cache.sqlite3'
    original = []
    for day in (1, 2, 3):
        settings, manifest, payload = binding_run(day=day)
        artifact = json.loads(payload)
        settings['_COST_REQUEST_SETTINGS'] = {'TRANSPORT': digest('responses' if day % 2 else 'completions')}
        settings['_COST_IGNORED_SETTINGS'] = deepcopy(IGNORED)
        row = artifact['tests'][0]
        row['compatibility'] = compatibility(settings)
        row['calls'][0]['quote']['accounting']['compatibility'] = compatibility(settings)
        artifact['quota']['after']['pct_used'] = 2
        payload = json.dumps(artifact).encode()
        manifest['files']['accounting.json'] = sha256(payload).hexdigest()
        original.append((manifest, artifact))
        cache = HistoryCache(cache_path)
        cache.ingest('origin', manifest, payload)
        cache.bind('origin', allocation_scope(settings), manifest['run_id'])
        cache.close()
    # Another transport uses the same binding, but recorded mode hashes survive.
    settings['_COST_REQUEST_SETTINGS']['TRANSPORT'] = digest('completions')
    cache = HistoryCache(cache_path)
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'c' * 64)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    snapshot = HistoricalEstimator([test], settings, cache, 'origin', 'repo', bundled_rate_card(),
                                  now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions['t']['sample_count'] == 3
    assert snapshot.predictions['t']['basis'] == 'historical_quota'
    assert sorted(cache.records('origin'), key=lambda value: value[0]['finished_at']) == original
    cache.close()


def test_bundled_provider_declares_transport_equivalence():
    from pathlib import Path
    import tomllib
    from api_agent.llm_providers.gpt import factory
    declaration = tomllib.loads((Path(factory.__file__).parent / 'cfg/config.toml').read_text())
    assert declaration['fields']['GPT_API_MODE']['cost_affecting'] is False
    assert 'cost_equivalent_values' not in declaration['fields']['GPT_API_MODE']


def test_api_key_predictions_ignore_credential_index_but_keep_transport_metadata(monkeypatch):
    history = histories('api_key')
    settings = dict(binding_settings(), _COST_REQUEST_SETTINGS=history[0][1]['tests'][0]['compatibility']['_COST_REQUEST_SETTINGS'],
                    _COST_IGNORED_SETTINGS=IGNORED)
    settings['UNATTENDED_AUTH']['roles']['primary'].append({'vault': 'another', 'user': 'another'})
    class Cache:
        def records(self, origin): return history
        def bound_window(self, *args): return None
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    snapshots = []
    for index in (0, 1):
        config = deepcopy(settings)
        config['UNATTENDED_USER_INDEX'] = index
        snapshots.append(HistoricalEstimator([test], config, Cache(), 'origin', 'repo', bundled_rate_card(),
                                            now=datetime(2026, 10, 10, tzinfo=timezone.utc)))
    assert snapshots[0].predictions == snapshots[1].predictions
    assert snapshots[0].predictions['t']['sample_count'] == 3
    assert snapshots[0].predictions['t']['basis'] == 'historical_list_price'
