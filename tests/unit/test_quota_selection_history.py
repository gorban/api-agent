"""Quota-only history never claims a complete physical call inventory."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from test_historical_estimation import records, compat


def single_selection(attempts=('passed',)):
    manifest, artifact = records()[0]
    settings = dict(compat(), LLM_PAY_PERIOD='day', LLM_QUOTA_PERIOD='day', LLM_PAY_PERIOD_LIMIT=100.)
    test = SimpleNamespace(id='repo::test.py::test[one]', node_id='test.py::test[one]')
    rows = []
    for index, status in enumerate(attempts, 1):
        row = deepcopy(artifact['tests'][0])
        row.update(test_id=test.id if index == 1 else f'{test.id}::confirmation:{index}',
                   compatibility=settings, outcomes=[status], attempts=1,
                   calls_complete=False, observed_call_count=1, stabilized_estimate={'estimated_cost_usd': None})
        row['calls'][0]['quote'] = None
        rows.append(row)
    artifact['tests'] = rows
    artifact['quota']['payment_policy'] = dict(usd=100., quota_seconds=86400, period='day')
    manifest.update(repo='repo', mode='e2e', files={'accounting.json': 'a' * 64})
    files = [f'{index}-result.json' for index in range(len(rows))]
    manifest['files'].update({name: 'b' * 64 for name in files})
    manifest['metadata'] = {'e2e_reruns': 2, 'e2e_outcomes': [dict(
        history_id=sha256(f'repo:e2e:{test.node_id}'.encode()).hexdigest(),
        first_status=attempts[0], final_status='passed', complete=True,
        attempts=len(rows), attempt_statuses=list(attempts), result_files=files)]}
    return test, settings, manifest, artifact


@pytest.mark.parametrize('attempts', [('passed',), ('failed', 'passed', 'passed')])
def test_single_selection_uses_whole_boundary_including_confirmations(attempts):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection(attempts)
    prediction = quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings)[test.id]
    assert prediction['estimated_cost_usd'] == 1.
    assert prediction['sample_count'] == 1
    assert prediction['basis'] == 'historical_selected_quota'
    assert prediction['known_zero'] is False
    assert all(row['calls_complete'] is False for row in artifact['tests'])


@pytest.mark.parametrize('change', ['reset', 'decrease', 'zero', 'missing_boundary', 'other_policy',
    'other_source', 'other_config', 'other_node', 'other_owner', 'missing_attempt',
    'forged_history', 'missing_result', 'unfinished', 'multiple_selections', 'metered'])
def test_quota_history_rejects_unattributable_or_discontinuous_samples(change):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection(('failed', 'passed', 'passed'))
    quota = artifact['quota']
    ledger = manifest['metadata']['e2e_outcomes'][0]
    if change == 'reset': quota['after']['reset_at'] = '2026-11-02T00:00:00Z'
    elif change == 'decrease': quota['after']['pct_used'] = 0; quota['before']['pct_used'] = 1
    elif change == 'zero': quota['pct_point_delta'] = quota['after']['pct_used'] = 0
    elif change == 'missing_boundary': quota['before'] = None
    elif change == 'other_policy': quota['payment_policy']['usd'] = 200.
    elif change == 'other_source': artifact['tests'][1]['source_fingerprint'] = 'other'
    elif change == 'other_config': artifact['tests'][1]['compatibility'] = dict(settings, LLM_DEFAULT_MODEL='other')
    elif change == 'other_node': artifact['tests'][1]['node_id'] = 'other'
    elif change == 'other_owner': artifact['tests'][1]['test_id'] = 'other::confirmation:2'
    elif change == 'missing_attempt': artifact['tests'].pop(1)
    elif change == 'forged_history': ledger['history_id'] = 'forged'
    elif change == 'missing_result': del manifest['files'][ledger['result_files'][1]]
    elif change == 'unfinished': manifest['status'] = 'interrupted'
    elif change == 'multiple_selections': manifest['metadata']['e2e_outcomes'].append(deepcopy(ledger))
    else: artifact['tests'][0]['calls'][0]['quote'] = {'accounting': {'billing_mode': 'metered'}}
    assert quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings) == {}


def test_quota_history_ignores_contamination_label_and_deduplicates_runs():
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    artifact['quota']['contaminated'] = True
    artifact['quota']['quota_unavailable_reason'] = 'contamination_uncontrolled'
    prediction = quota_predictions_for([test], [(manifest, artifact)] * 2, {test.id: 'hash'}, settings)[test.id]
    assert prediction['sample_count'] == 1
    assert prediction['estimated_cost_usd'] == 1.


@pytest.mark.parametrize('change', ['incomplete_config', 'no_fingerprint', 'no_rows', 'integration',
    'bad_integration', 'bad_mode', 'no_ledger', 'bad_confirmation_policy', 'duplicate_row',
    'invalid_attempts', 'no_policy', 'wrong_period', 'wrong_quota_period', 'conflicting_run', 'duplicate_result'])
def test_quota_sample_requires_exact_selection_and_payment_contract(change):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    fingerprints = {test.id: 'hash'}
    records = [(manifest, artifact)]
    expected = False
    if change == 'incomplete_config': settings = {}
    elif change == 'no_fingerprint': fingerprints = {}
    elif change == 'no_rows': artifact['tests'] = []
    elif change == 'integration': manifest['mode'] = 'integration'; expected = True
    elif change == 'bad_integration': manifest['mode'] = 'integration'; artifact['tests'][0]['outcomes'] = ['failed']
    elif change == 'bad_mode': manifest['mode'] = 'unit'
    elif change == 'no_ledger': manifest['metadata'] = {}
    elif change == 'bad_confirmation_policy': manifest['metadata']['e2e_reruns'] = -1
    elif change == 'duplicate_row': artifact['tests'].append(deepcopy(artifact['tests'][0]))
    elif change == 'invalid_attempts': artifact['tests'][0]['attempts'] = 2
    elif change == 'no_policy':
        artifact['quota']['payment_policy'] = None
        settings = dict(settings, LLM_PAY_PERIOD_LIMIT=None)
        artifact['tests'][0]['compatibility'] = settings
    elif change == 'wrong_period': artifact['quota']['payment_policy']['period'] = 'week'
    elif change == 'wrong_quota_period': settings = dict(settings, LLM_QUOTA_PERIOD='week'); artifact['tests'][0]['compatibility'] = settings
    elif change == 'duplicate_result':
        test, settings, manifest, artifact = single_selection(('failed', 'passed', 'passed'))
        manifest['metadata']['e2e_outcomes'][0]['result_files'][1] = '0-result.json'
        records = [(manifest, artifact)]
    else:
        other = deepcopy(artifact); other['quota']['pct_point_delta'] = 2
        records.append((deepcopy(manifest), other))
    assert bool(quota_predictions_for([test], records, fingerprints, settings)) is expected


def test_historical_adapter_exposes_quota_samples_with_incomplete_calls(monkeypatch):
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.telemetry.accounting import compatibility
    from datetime import datetime, timezone
    test, settings, manifest, artifact = single_selection()
    settings = compatibility(settings)
    artifact['tests'][0]['compatibility'] = settings
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    cache = SimpleNamespace(records=lambda *args: [(manifest, artifact)])
    snapshot = HistoricalEstimator([test], dict(settings, TEST_COST_HISTORY_DAYS=30), cache, 'origin', 'repo', None,
                                   now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions[test.id]['estimated_cost_usd'] == 1.
    assert snapshot.predictions[test.id]['basis'] == 'historical_selected_quota'


def test_missing_legacy_payment_quote_uses_matching_declared_payment_policy():
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    artifact['quota']['payment_policy'] = None
    prediction = quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings)[test.id]
    assert prediction['estimated_cost_usd'] == 1.
    assert artifact['quota']['payment_policy'] is None


def test_completed_average_retains_prior_logical_quota_forecast():
    from api_agent.telemetry.estimation import EstimateSnapshot
    test, settings, manifest, artifact = single_selection()
    snapshot = EstimateSnapshot({}, records=[(manifest, artifact)])
    current = deepcopy(artifact)
    current['run_id'] = 'new'
    average = snapshot.completed_averages(current, {})[test.id]
    assert average['estimated_cost_usd'] == 1.
    assert average['basis'] == 'historical_selected_quota'


@pytest.mark.parametrize('mode', ['e2e', 'integration'])
def test_unlisted_allure_result_prevents_sole_selection_attribution(mode):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    manifest['mode'] = mode
    manifest['files']['other-result.json'] = 'c' * 64
    assert quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings) == {}


def test_billed_transport_attempt_prevents_quota_history_sample():
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    artifact['tests'][0]['calls'][0]['transport_attempts'] = {'attempts': [dict(
        billing={'usd': .5, 'request_id': None, 'generation_id': None}, usage={})]}
    assert quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings) == {}


def test_confirmation_projection_preserves_a_logical_quota_forecast():
    from api_agent.testing.confirmations import logical_usage
    costs = {key: dict(prompt_count=1, estimated_cost_usd=None) for key in ('t', 'c')}
    forecast = dict(estimated_cost_usd=1., basis='historical_selected_quota', sample_count=2)
    result = logical_usage({'test_costs': costs}, {'t': 't', 'c': 't'}, predictions={'t': forecast})
    assert result['test_costs']['t']['historical_average'] == forecast
    assert result['test_costs']['t']['advance_prediction'] == forecast


@pytest.mark.parametrize('settings', [
    {}, dict(LLM_PAY_PERIOD='year', LLM_QUOTA_PERIOD='day', LLM_PAY_PERIOD_LIMIT=100.),
    dict(LLM_PAY_PERIOD='day', LLM_QUOTA_PERIOD='year', LLM_PAY_PERIOD_LIMIT=100.),
    dict(LLM_PAY_PERIOD='day', LLM_QUOTA_PERIOD='day', LLM_PAY_PERIOD_LIMIT=True),
])
def test_configured_policy_does_not_guess_invalid_or_missing_settings(settings):
    from api_agent.telemetry.quota_estimates import configured_payment_policy
    assert configured_payment_policy(settings, now=None) is None


def test_monthly_policy_uses_boundary_calendar_and_keeps_provenance():
    from datetime import datetime, timezone
    from api_agent.telemetry.quota_estimates import configured_payment_policy, configured_quota_estimate
    policy = configured_payment_policy(dict(LLM_PAY_PERIOD='month', LLM_QUOTA_PERIOD='month', LLM_PAY_PERIOD_LIMIT=100.),
                                       now=datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert policy == dict(usd=100., quota_seconds=28 * 86400, period='calendar_month')
    _, _, _, artifact = single_selection()
    assert configured_quota_estimate(artifact['quota'], []) is None
    artifact['quota']['after'] = None
    assert configured_quota_estimate(artifact['quota'], artifact['tests']) is None


def test_mixed_configured_payments_cannot_price_a_shared_suite():
    from api_agent.telemetry.quota_estimates import configured_quota_estimate
    _, _, _, artifact = single_selection()
    other = deepcopy(artifact['tests'][0])
    other['compatibility']['LLM_PAY_PERIOD_LIMIT'] = 200.
    assert configured_quota_estimate(artifact['quota'], [*artifact['tests'], other]) is None


@pytest.mark.parametrize('change', ['metadata', 'outcomes', 'ledger', 'files', 'attempt_type'])
def test_malformed_manifest_sample_cannot_abort_valid_history(change):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    bad_manifest, bad_artifact = deepcopy(manifest), deepcopy(artifact)
    bad_artifact['run_id'] = 'other'
    if change == 'metadata': bad_manifest['metadata'] = 'not-an-object'
    elif change == 'outcomes': bad_manifest['metadata']['e2e_outcomes'] = {'bad': None}
    elif change == 'ledger': bad_manifest['metadata']['e2e_outcomes'] = ['not-an-object']
    elif change == 'files': bad_manifest['metadata']['e2e_outcomes'][0]['result_files'] = {'0-result.json': None}
    else: bad_manifest['metadata']['e2e_outcomes'][0]['attempts'] = True
    prediction = quota_predictions_for([test], [(bad_manifest, bad_artifact), (manifest, artifact)],
                                       {test.id: 'hash'}, settings)[test.id]
    assert prediction['sample_count'] == 1
    assert prediction['estimated_cost_usd'] == 1.


@pytest.mark.parametrize('conflict', ['payment', 'metered', 'api_key'])
def test_unbilled_transport_quote_conflict_prevents_quota_history(conflict):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, _, manifest, artifact = single_selection()
    settings = artifact['tests'][0]['compatibility']
    quote = {'payment': artifact['quota']['payment_policy']}
    if conflict == 'payment': quote['payment'] = dict(quote['payment'], usd=200.)
    else: quote['accounting'] = {'billing_mode': conflict}
    artifact['tests'][0]['calls'][0]['transport_attempts'] = {'attempts': [dict(
        quote=quote, billing=None, usage={})]}
    assert quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings) == {}


@pytest.mark.parametrize('change', ['statuses', 'status_type', 'filename_type', 'missing_reruns', 'pending_confirmations', 'invalid_sequence'])
def test_confirmation_metadata_cannot_claim_an_unfinished_or_invalid_pass(change):
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    attempts = ('failed', 'passed') if change == 'pending_confirmations' else ('passed',)
    test, settings, manifest, artifact = single_selection(attempts)
    ledger = manifest['metadata']['e2e_outcomes'][0]
    if change == 'statuses': ledger['attempt_statuses'] = 'passed'
    elif change == 'status_type': ledger['attempt_statuses'] = [True]
    elif change == 'filename_type': ledger['result_files'] = [{}]
    elif change == 'missing_reruns': del manifest['metadata']['e2e_reruns']
    elif change == 'invalid_sequence':
        test, settings, manifest, artifact = single_selection(('passed', 'passed'))
    assert quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings) == {}


def test_quota_amount_needs_valid_boundaries_and_a_policy():
    from api_agent.telemetry.quota_estimates import quota_amount
    _, _, _, artifact = single_selection()
    assert quota_amount({}) is None
    artifact['quota']['payment_policy'] = None
    assert quota_amount(artifact['quota']) is None


def test_known_call_price_is_not_overwritten_by_quota_only_history(monkeypatch):
    from datetime import datetime, timezone
    from api_agent.telemetry.accounting import compatibility
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.providers.rates import bundled_rate_card
    test, settings, manifest, artifact = single_selection()
    settings = compatibility(settings)
    row = artifact['tests'][0]
    row.update(compatibility=settings, calls_complete=True)
    quote = deepcopy(records()[0][1]['tests'][0]['calls'][0]['quote'])
    quote['accounting']['compatibility'] = settings
    row['calls'][0]['quote'] = quote
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *args, **kwargs: 'hash')
    cache = SimpleNamespace(records=lambda *args: [(manifest, artifact)])
    snapshot = HistoricalEstimator([test], dict(settings, TEST_COST_HISTORY_DAYS=30), cache, 'origin', 'repo', bundled_rate_card(),
                                   now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions[test.id]['basis'] == 'historical_list_price'
    assert snapshot.predictions[test.id]['estimated_cost_usd'] < 1.


def test_confirmation_projection_keeps_the_frozen_picker_forecast_distinct():
    from api_agent.testing.confirmations import logical_usage
    costs = {key: dict(prompt_count=1, estimated_cost_usd=None) for key in ('t', 'c')}
    frozen = dict(estimated_cost_usd=.25, basis='historical_selected_quota', sample_count=1)
    current = dict(estimated_cost_usd=1., basis='historical_selected_quota', sample_count=2)
    result = logical_usage({'test_costs': costs}, {'t': 't', 'c': 't'},
                           predictions={'t': current}, advance_predictions={'t': frozen})
    assert result['test_costs']['t']['historical_average'] == current
    assert result['test_costs']['t']['advance_prediction'] == frozen


def test_initialized_zero_window_survives_accounting_and_history():
    from api_agent.telemetry.quota_estimates import quota_predictions_for
    test, settings, manifest, artifact = single_selection()
    before, after = artifact['quota']['before'], artifact['quota']['after']
    before.update(pct_used=0, reset_at=None, window_id=None,
                  allocation_id='a' * 64, initial_window=True)
    after['allocation_id'] = 'a' * 64
    after['pct_used'] = 8
    artifact['quota']['pct_point_delta'] = 8

    prediction = quota_predictions_for([test], [(manifest, artifact)], {test.id: 'hash'}, settings)
    assert prediction[test.id]['estimated_cost_usd'] == 8
