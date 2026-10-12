from copy import deepcopy
from uuid import NAMESPACE_DNS, uuid5

import pytest

from api_agent.providers.rates import bundled_rate_card
from api_agent.telemetry.estimation import calibration, overlapping_quota


COMPAT = dict(_COST_PROVIDER_IDENTITY='provider', LLM_DEFAULT_MODEL='model',
              LLM_CACHE_POLICY='fresh', E2E_RERUNS=0, _COST_PACK_IDENTITY='pack',
              _COST_REQUEST_SETTINGS={})
CARD = bundled_rate_card()
POLICY = dict(usd=100, quota_seconds=604800, period='quota')


def record(run, allocation='a' * 64, *, before=None, after=None, delta=1, step=.1,
           window='window', reset='2026-11-01T00:00:00Z'):
    if before is None:
        before = f'2026-10-{run:02d}T00:00:00Z'
    if after is None:
        after = f'2026-10-{run:02d}T01:00:00Z'
    snapshot = lambda at, observation, pct: dict(allocation_id=allocation,
        observation_id=str(uuid5(NAMESPACE_DNS, observation)), window_id=window, reset_at=reset,
        captured_at=at, pct_step=step, pct_used=pct)
    accounting = dict(billing_mode='subscription', service_tier='standard',
                      actual_model='gpt-4.1-nano', compatibility=COMPAT)
    row = dict(test_id='t', node_id='test::t', source_fingerprint='hash',
               compatibility=COMPAT, outcomes=['passed'], calls_complete=True,
               observed_call_count=1, calls=[dict(quote=dict(accounting=accounting),
               usage=dict(input_tokens=1000, output_tokens=0,
                          cached_input_tokens=0, reasoning_tokens=0))])
    artifact = dict(run_id=str(uuid5(NAMESPACE_DNS, f'run-{run}')), rate_card=CARD, tests=[row],
                    quota=dict(before=snapshot(before, f'{run}-before', 10),
                               after=snapshot(after, f'{run}-after', 10 + delta),
                               pct_point_delta=delta, payment_policy=POLICY))
    return dict(status='completed'), artifact


def test_unknown_allocation_cannot_calibrate():
    history = [record(run) for run in (1, 2, 3)]
    for _, artifact in history:
        artifact['quota']['before']['allocation_id'] = None
        artifact['quota']['after']['allocation_id'] = None
    assert calibration(history, CARD) == {}


def test_independent_allocations_require_individual_qualification():
    history = [record(run) for run in (1, 2, 3)]
    history.extend(record(run, 'b' * 64) for run in (4, 5))
    result = next(iter(calibration(history, CARD).values()))
    assert result['sample_count'] == 3
    assert set(result['allocations']) == {'a' * 64}


def test_proven_shared_boundary_reduces_endpoint_uncertainty():
    history = [record(run, delta=.5, step=.2) for run in (1, 2, 3)]
    for index in (1, 2):
        prior = history[index - 1][1]['quota']['after']
        current = history[index][1]['quota']['before']
        current.update(deepcopy(prior))
        history[index][1]['quota']['after']['pct_used'] = current['pct_used'] + .5
    result = next(iter(calibration(history, CARD).values()))
    allocation = result['allocations']['a' * 64]
    assert allocation['span_count'] == 1
    assert allocation['uncertainty'] == pytest.approx(.2)
    assert allocation['spans'][0]['run_ids'] == [artifact['run_id'] for _, artifact in history]


def test_equal_boundary_facts_without_shared_observation_do_not_connect():
    history = [record(run, delta=.5, step=.2) for run in (1, 2, 3)]
    for index in (1, 2):
        prior = history[index - 1][1]['quota']['after']
        current = history[index][1]['quota']['before']
        current.update(deepcopy(prior))
        history[index][1]['quota']['after']['pct_used'] = current['pct_used'] + .5
        current['observation_id'] = str(uuid5(NAMESPACE_DNS, f'different-{index}'))
    assert calibration(history, CARD) == {}


def test_overlap_only_within_same_allocation_and_window():
    first = record(1)[1]
    second = record(2, before='2026-10-01T00:30:00Z', after='2026-10-01T01:30:00Z')[1]
    assert overlapping_quota(first, [({}, second)])
    second['quota']['before']['allocation_id'] = 'b' * 64
    second['quota']['after']['allocation_id'] = 'b' * 64
    assert not overlapping_quota(first, [({}, second)])


def test_duplicate_run_identity_does_not_add_quota_weight():
    history = [record(run) for run in (1, 2, 3)]
    duplicate = deepcopy(history[0])
    result = next(iter(calibration(history + [duplicate], CARD).values()))
    assert result['sample_count'] == 3


def test_zero_runs_can_qualify_when_later_ticks_cover_bounds():
    history = [record(run, delta=0, step=.1) for run in (1, 2)]
    history.append(record(3, delta=2, step=.1))
    result = next(iter(calibration(history, CARD).values()))
    assert result['sample_count'] == 3
    assert result['allocations']['a' * 64]['delta'] == 2


@pytest.mark.parametrize('change', [
    lambda artifact: artifact['quota']['after'].update(reset_at='2026-12-01T00:00:00Z'),
    lambda artifact: artifact['quota']['before'].pop('pct_step'),
    lambda artifact: artifact['tests'][0].update(calls_complete=False),
    lambda artifact: artifact['tests'][0]['calls'][0]['quote']['accounting'].update(actual_model='unpriced'),
    lambda artifact: artifact['tests'][0]['calls'][0]['quote']['accounting'].update(billing_mode='api_key'),
])
def test_invalid_sample_does_not_qualify(change):
    history = [record(run) for run in (1, 2, 3)]
    change(history[0][1])
    assert calibration(history, CARD) == {}


def test_separate_reset_windows_can_qualify_same_allocation():
    history = [record(run, window=f'window-{run}', reset='2026-12-01T00:00:00Z') for run in (1, 2, 3)]
    result = next(iter(calibration(history, CARD).values()))
    assert result['allocations']['a' * 64]['span_count'] == 3


def test_separate_overlap_inventory_contaminates_by_run_identity():
    history = [record(run) for run in (1, 2, 3)]
    other = record(4, before='2026-10-01T00:30:00Z', after='2026-10-01T01:30:00Z')
    inventory = deepcopy(history + [other])
    assert calibration(history, CARD, overlap_records=inventory) == {}


@pytest.mark.parametrize('field,value', [('contaminated', True), ('quota_unavailable_reason', 'timeout')])
def test_rejected_overlapping_inventory_still_contaminates(field, value):
    history = [record(run) for run in (1, 2, 3)]
    other = record(4, before='2026-10-01T00:30:00Z', after='2026-10-01T01:30:00Z')
    other[1]['quota'][field] = value
    assert calibration(history, CARD, overlap_records=history + [other]) == {}


def test_conflicting_duplicate_run_excludes_all_variants():
    history = [record(run) for run in (1, 2, 3)]
    other = record(4, before='2026-10-01T00:30:00Z', after='2026-10-01T01:30:00Z')
    other[1]['run_id'] = history[0][1]['run_id']
    assert calibration(history, CARD, overlap_records=history + [other]) == {}


def test_identical_duplicate_does_not_overlap_itself():
    artifact = record(1)[1]
    assert not overlapping_quota(artifact, [({}, deepcopy(artifact))])


@pytest.mark.parametrize('change', [
    lambda q: q['after'].update(pct_used=q['before']['pct_used']),
    lambda q: q['before'].update(pct_used=None),
    lambda q: q['after'].update(captured_at=q['before']['captured_at']),
])
def test_inconsistent_percentage_evidence_rejected(change):
    history = [record(run) for run in (1, 2, 3)]
    change(history[0][1]['quota'])
    assert calibration(history, CARD) == {}


def test_qualified_allocations_pool_by_total_delta_over_total_price():
    history = [record(run) for run in (1, 2, 3)]
    others = [record(run, 'b' * 64, delta=2) for run in (4, 5, 6)]
    for _, artifact in others:
        artifact['quota']['after']['pct_used'] = 12
    result = next(iter(calibration(history + others, CARD).values()))
    assert result['sample_count'] == 6
    assert set(result['allocations']) == {'a' * 64, 'b' * 64}
    assert result['pct_per_usd'] == pytest.approx(9 / sum(item['amount_usd'] for item in result['allocations'].values()))


def test_tiny_fabricated_delta_cannot_qualify():
    history = [record(run, delta=1e-10, step=1e-12) for run in (1, 2, 3)]
    for _, artifact in history:
        artifact['quota']['after']['pct_used'] = artifact['quota']['before']['pct_used']
    assert calibration(history, CARD) == {}


def test_missing_frozen_rate_card_cannot_qualify_quota():
    history = [record(run) for run in (1, 2, 3)]
    history[0][1]['rate_card'] = None
    assert calibration(history, CARD) == {}


@pytest.mark.parametrize('phase,field,value', [
    ('before', 'allocation_id', 'A' * 64),
    ('after', 'allocation_id', 'invalid'),
    ('before', 'observation_id', 'invalid'),
    ('after', 'observation_id', '00000000-0000-0000-0000-00000000000A'),
])
def test_malformed_identity_cannot_qualify(phase, field, value):
    history = [record(run) for run in (1, 2, 3)]
    history[0][1]['quota'][phase][field] = value
    assert calibration(history, CARD) == {}


def test_weighted_pool_differs_from_mean_of_allocation_factors():
    history = [record(run) for run in (1, 2, 3)]
    others = [record(run, 'b' * 64, delta=2) for run in (4, 5, 6)]
    for _, artifact in others:
        artifact['quota']['after']['pct_used'] = 12
        artifact['tests'][0]['calls'][0]['usage']['input_tokens'] = 3000
    result = next(iter(calibration(history + others, CARD).values()))
    allocations = result['allocations']
    a, b = allocations['a' * 64], allocations['b' * 64]
    assert b['amount_usd'] == pytest.approx(3 * a['amount_usd'])
    assert result['pct_per_usd'] == pytest.approx((a['delta'] + b['delta']) / (a['amount_usd'] + b['amount_usd']))
    assert result['pct_per_usd'] != pytest.approx((a['delta'] / a['amount_usd'] + b['delta'] / b['amount_usd']) / 2)


def test_same_run_id_conflicting_artifact_blocks_current_quota_even_without_time_overlap():
    current = record(1)[1]
    other = deepcopy(current)
    other['quota']['before']['captured_at'] = '2026-10-08T00:00:00Z'
    other['quota']['after']['captured_at'] = '2026-10-08T01:00:00Z'
    assert overlapping_quota(current, [({}, other)])


def test_manifest_status_conflict_excludes_calibration_run():
    history = [record(run) for run in (1, 2, 3)]
    conflicting = (dict(status='failed'), deepcopy(history[0][1]))
    assert calibration(history + [conflicting], CARD) == {}


def test_manifest_status_conflict_excludes_prediction_run():
    from types import SimpleNamespace
    from api_agent.telemetry.estimation import predictions_for
    history = [record(1)]
    conflicting = (dict(status='failed'), deepcopy(history[0][1]))
    test = SimpleNamespace(id='t', node_id='test::t')
    prediction = predictions_for([test], history + [conflicting], CARD, {'t': 'hash'}, COMPAT)['t']
    assert prediction['sample_count'] == 0


def test_unknown_run_identity_in_overlap_inventory_still_blocks_known_samples():
    history = [record(run) for run in (1, 2, 3)]
    other = record(4, before='2026-10-01T00:30:00Z', after='2026-10-01T01:30:00Z')
    other[1].pop('run_id')
    assert calibration(history, CARD, overlap_records=history + [other]) == {}
