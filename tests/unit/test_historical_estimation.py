from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.estimation import EstimateSnapshot, calibration, payment_per_point, predictions_for, priced


def compat():
    return {key: value for key, value in zip(('_COST_PROVIDER_IDENTITY', 'LLM_DEFAULT_MODEL', 'LLM_CACHE_POLICY', 'GPT_AUTH_MODE', 'GPT_API_MODE', 'GPT_BASE_URL', 'E2E_RERUNS', '_COST_PACK_IDENTITY', '_COST_REQUEST_SETTINGS'), ('a' * 64, 'model', 'fresh', 'subscription', 'responses', 'route', 0, 'pack', {}))}


def row(cost=1000):
    return dict(calls_complete=True, test_id='t', node_id='test.py::test[one]', source_fingerprint='hash', compatibility=compat(), outcomes=['failed', 'passed'], calls=[{'quote': {'accounting': {'billing_mode': 'subscription', 'service_tier': 'standard', 'actual_model': 'gpt-4.1-nano', 'compatibility': compat()}}, 'usage': {'input_tokens': cost, 'output_tokens': 0, 'cached_input_tokens': 0, 'reasoning_tokens': 0}}])


def records(delta=1, step=.1):
    card = bundled_rate_card()
    return [(dict(status='completed', started_at=f'2026-10-{day:02d}T00:00:00Z', finished_at=f'2026-10-{day:02d}T01:00:00Z'),
             dict(rate_card=card, quota=dict(before={'pct_step': step, 'captured_at': f'2026-10-{day:02d}T00:00:00Z', 'window_id': 'window', 'reset_at': '2026-11-01T00:00:00Z'}, after={'pct_step': step, 'captured_at': f'2026-10-{day:02d}T01:00:00Z', 'window_id': 'window', 'reset_at': '2026-11-01T00:00:00Z'}, pct_point_delta=delta,
                  payment_policy={'usd': 100, 'quota_seconds': 604800, 'period': 'quota'}, quota_unavailable_reason=None), tests=[row()])) for day in (1, 2, 3)]


def test_prices_mean_retries_and_missing():
    card = bundled_rate_card()
    value = row()
    amount = priced(value, card)
    value['calls'] *= 2
    assert priced(value, card) == amount * 2
    test = SimpleNamespace(id='t', node_id=value['node_id'])
    history = [({}, {'tests': [row(1000), row(3000)]})]
    prediction = predictions_for([test], history, card, {'t': 'hash'}, compat())['t']
    assert prediction['estimated_cost_usd'] == amount * 2
    assert prediction['sample_count'] == 2
    for fingerprint, settings, node in [('other', compat(), test.node_id), ('hash', None, test.node_id), ('hash', dict(compat(), LLM_DEFAULT_MODEL='y'), test.node_id), ('hash', compat(), 'test.py::test[two]')]:
        changed = SimpleNamespace(id='t', node_id=node)
        assert predictions_for([changed], history, card, {'t': fingerprint}, settings)['t']['estimated_cost_usd'] is None
    assert priced({'calls': None}, card) is None
    assert priced({'calls': [], 'calls_complete': True}, card) == 0
    assert priced({'calls': [{'quote': None}]}, card) is None
    bad = row(); bad['calls'][0]['usage']['input_tokens'] = 'bad'
    assert priced(bad, card) is None
    bad = row(); bad['calls'][0]['quote']['accounting']['actual_model'] = 'unknown'
    assert priced(bad, card) is None


def test_each_new_small_run_reduces_arithmetic_prediction():
    card = bundled_rate_card()
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    history = [({}, {'tests': [row(10000)]}), ({}, {'tests': [row(1000)]}),
               ({}, {'tests': [row(1000)]})]
    unit_cost = priced(row(1000), card)
    previous = None
    for count in (3, 4, 5):
        prediction = predictions_for([test], history, card, {'t': 'hash'}, compat())['t']
        expected = unit_cost * (10 + count - 1) / count
        assert prediction['estimated_cost_usd'] == pytest.approx(expected)
        assert prediction['sample_count'] == count
        assert prediction['variation'] == pytest.approx(unit_cost * 9)
        if previous is not None:
            assert prediction['estimated_cost_usd'] < previous
        previous = prediction['estimated_cost_usd']
        history.append(({}, {'tests': [row(1000)]}))


def test_calibration_zero_and_spike_and_bounds():
    card = bundled_rate_card()
    history = records()
    assert len(calibration(history, card)) == 1
    assert calibration(history[:2], card) == {}
    assert calibration(records(delta=0), card) == {}
    assert calibration(records(step=.26), card) == {}
    assert calibration(records(step=.25), card)
    history[0][1]['quota']['pct_point_delta'] = 0
    history[1][1]['quota']['pct_point_delta'] = 0
    history[2][1]['quota']['pct_point_delta'] = 3
    assert calibration(history, card)
    history[1][1]['quota']['before']['captured_at'] = history[0][1]['quota']['before']['captured_at']
    assert calibration(history, card) == {}


@pytest.mark.parametrize('change', ['missing_price', 'missing_precision', 'reason', 'contaminated', 'negative', 'zero_use', 'bad_time', 'naive', 'reversed', 'policy_change', 'rate_change'])
def test_calibration_rejects(change):
    history = records()
    manifest, artifact = history[0]
    if change == 'missing_price': artifact['tests'][0]['calls'][0]['quote'] = None
    elif change == 'missing_precision': artifact['quota']['before'] = {}
    elif change == 'reason': artifact['quota']['quota_unavailable_reason'] = 'changed_window'
    elif change == 'contaminated': artifact['quota']['contaminated'] = True
    elif change == 'negative': artifact['quota']['pct_point_delta'] = -1
    elif change == 'zero_use': artifact['tests'][0]['calls'] = []
    elif change == 'bad_time': artifact['quota']['before']['captured_at'] = 'bad'
    elif change == 'naive': artifact['quota']['before']['captured_at'] = '2026-10-01T00:00:00'
    elif change == 'reversed': artifact['quota']['after']['captured_at'] = '2026-09-01T00:00:00Z'
    elif change == 'policy_change': artifact['quota']['payment_policy']['usd'] = 200
    else: artifact['rate_card'] = dict(artifact['rate_card'], revision='changed')
    assert calibration(history, bundled_rate_card()) == {}


def test_snapshot_frozen_completion_and_totals():
    card = bundled_rate_card()
    history = records()
    snap = EstimateSnapshot({'t': {'estimated_cost_usd': .2, 'basis': 'historical', 'sample_count': 3}, 'zero': {'estimated_cost_usd': 0, 'known_zero': True}}, records=history, card=card)
    assert snap.total(['t', 't', 'missing']) == dict(estimated_cost_usd=.2, incomplete=True, known_count=1, unknown_count=1, known_zero=False)
    assert snap.total(['zero'])['known_zero']
    assert snap.total(['missing'])['estimated_cost_usd'] is None
    assert snap.predictions['t']['sample_count'] == 3
    artifact = deepcopy(history[0][1])
    calibrated = snap.complete(artifact)['t']
    assert calibrated['basis'] == 'historical_quota'
    assert calibrated['estimated_cost_usd'] == pytest.approx(1)
    snap = EstimateSnapshot({}, card=card)
    assert snap.complete(artifact)['t']['basis'] == 'current_quota'
    artifact['quota']['before'] = {}
    assert snap.complete(artifact)['t']['basis'] == 'list_price'
    artifact['quota']['payment_policy'] = None
    artifact['tests'][0]['calls'][0]['quote'] = None
    assert snap.complete(artifact)['t']['estimated_cost_usd'] is None
    assert snap.synchronize() is snap
    assert snap.ingest(None, None) is None
    assert snap.close() is None


def test_payment_conversion():
    date = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert payment_per_point({'usd': 100, 'quota_seconds': 604800, 'period': 'calendar_month'}, now=date) == pytest.approx(7 / 31)
    assert payment_per_point({'usd': 100, 'quota_seconds': 86400, 'period': 'day'}, now=date) == 1


def test_identity_settings_revision_and_zero():
    card = bundled_rate_card()
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    history = [(dict(finished_at='2026-10-01'), {'tests': [row()]})]
    history[0][1]['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = {}
    assert predictions_for([test], history, card, {'t': 'hash'}, compat())['t']['estimated_cost_usd'] is None
    history = [(dict(finished_at='2026-10-01'), {'tests': [row(), row()]})]
    history[0][1]['tests'][1]['calls'][0]['quote']['accounting']['serving_revision'] = 'changed'
    assert predictions_for([test], history, card, {'t': 'hash'}, compat())['t']['estimated_cost_usd'] is None
    history = [(dict(finished_at='2026-10-01'), {'tests': [dict(row(), calls=[], observed_call_count=0)]})]
    assert predictions_for([test], history, card, {'t': 'hash'}, compat())['t']['known_zero']
    history[0][1]['tests'][0]['observed_call_count'] = 1
    assert predictions_for([test], history, card, {'t': 'hash'}, compat())['t']['estimated_cost_usd'] is None
    history[0][1]['tests'][0]['outcomes'] = ['failed']
    assert predictions_for([test], history, card, {'t': 'hash'}, compat())['t']['sample_count'] == 0


def test_calibration_window_outcomes_and_other_origin():
    card = bundled_rate_card()
    history = records()
    extra = deepcopy(history[0]); extra[1]['run_id'] = 'different'
    assert calibration(history, card, overlap_records=history + [extra]) == {}
    history[0][1]['quota']['before']['window_id'] = 'different'
    assert calibration(history, card) == {}
    history = records(); history[0][1]['tests'][0]['outcomes'] = ['failed']
    assert calibration(history, card) == {}
    history = records(); history[0][1]['tests'][0]['calls'][0]['quote']['accounting']['billing_mode'] = 'api_key'
    assert calibration(history, card) == {}
    snap = EstimateSnapshot({}, card=card)
    artifact = history[0][1]
    assert snap.complete(artifact)['t']['basis'] == 'list_price'
    artifact['quota']['payment_policy'] = None
    artifact['tests'][0] = dict(row(), calls=[], observed_call_count=1, list_cost_usd=.3)
    assert snap.complete(artifact)['t']['estimated_cost_usd'] == .3
    assert not snap.complete(artifact)['t']['known_zero']


def test_current_quota_overlap_and_legacy_missing():
    from api_agent.telemetry.estimation import overlapping_quota
    history = records()
    artifact = deepcopy(history[0][1])
    artifact['run_id'] = 'new'
    assert overlapping_quota(artifact, history)
    assert not overlapping_quota({}, history)
    artifact['quota']['before']['captured_at'] = '2026-10-04T00:00:00Z'
    artifact['quota']['after']['captured_at'] = '2026-10-04T01:00:00Z'
    assert not overlapping_quota(artifact, [(None, {}), *history])
    artifact['quota']['before']['captured_at'] = '2026-10-01T00:00:00Z'
    artifact['quota']['after']['captured_at'] = '2026-10-01T01:00:00Z'
    history.pop()
    snap = EstimateSnapshot({}, records=history, card=bundled_rate_card())
    assert snap.complete(artifact)['t']['basis'] == 'list_price'


def test_calibration_missing_rate_independent_of_identity_validation():
    history = records()
    history[0][1]['tests'][0]['calls'][0]['quote']['accounting']['actual_model'] = 'missing-rate'
    assert calibration(history, bundled_rate_card()) == {}


def test_interrupted_suites_and_legacy_quote_do_not_qualify():
    history = records()
    for manifest, _ in history: manifest['status'] = 'interrupted'
    assert calibration(history, bundled_rate_card()) == {}
    artifact = deepcopy(history[0][1])
    artifact['tests'][0].update(observed_call_count=2, list_cost_usd=.25)
    result = EstimateSnapshot({}, card=bundled_rate_card()).complete(artifact)['t']
    assert result['basis'] == 'list_price'
    assert result['estimated_cost_usd'] == .25
    artifact['tests'][0]['calls'][0]['quote']['accounting']['service_tier'] = 'flex'
    assert priced(artifact['tests'][0], bundled_rate_card()) is None
    history = records()
    history[0][1]['tests'][0]['calls'][0]['usage']['input_tokens'] = None
    assert calibration(history, bundled_rate_card()) == {}
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    assert predictions_for([test], [history[0]], bundled_rate_card(), {'t': 'hash'}, compat())['t']['sample_count'] == 0


def test_calibration_zero_priced_calls():
    history = records()
    history[0][1]['tests'] = [row(0)]
    assert calibration(history, bundled_rate_card()) == {}


def test_billing_mode_revision_and_details_age_calibration():
    from api_agent.telemetry.estimation import quota_allowed
    api = row(); api['calls'][0]['quote']['accounting']['billing_mode'] = 'api_key'
    assert not quota_allowed(api)
    history = records()
    history[0][1]['tests'][0]['calls'][0]['quote']['accounting']['serving_revision'] = 'different'
    assert calibration(history, bundled_rate_card()) == {}
    history = records()
    artifact = history[0][1]
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    snap = EstimateSnapshot({'t': {'freshness': '2026-10-09T00:00:00Z'}}, records=history, card=bundled_rate_card(), now=now)
    assert snap.predictions['t']['freshness'] == '2026-10-09T00:00:00Z'
    derived = snap.complete(artifact)['t']['calibration']
    assert derived['sample_count'] == 3
    assert derived['relative_uncertainty'] == pytest.approx(.1)
    assert derived['scale_factor'] > 0
    assert derived['provenance']['model_signatures']
    test = SimpleNamespace(id='t', node_id=api['node_id'])
    mixed = [(dict(finished_at='2026-10-01T00:00:00Z'), {'tests': [row(), api]})]
    assert predictions_for([test], mixed, bundled_rate_card(), {'t': 'hash'}, compat())['t']['estimated_cost_usd'] is None


def test_account_windows_never_pool_calibration():
    history = records()
    for index, (_, artifact) in enumerate(history):
        artifact['quota']['before']['window_id'] = artifact['quota']['after']['window_id'] = f'account-{index}'
    assert calibration(history, bundled_rate_card()) == {}
