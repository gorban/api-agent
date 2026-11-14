"""Account-boundary estimates independent of unknown per-call token prices."""
from datetime import datetime
from hashlib import sha256
import math

from api_agent.providers.costs import period_seconds, valid_amount, qualified_charge
from .estimation import clean_quota, compatibility_complete, matching_compatibility, payment_per_point


def configured_payment_policy(settings, *, now):
    pay, quota = settings.get('LLM_PAY_PERIOD'), settings.get('LLM_QUOTA_PERIOD')
    amount = settings.get('LLM_PAY_PERIOD_LIMIT')
    if pay not in ('day', 'week', 'month') or quota not in ('day', 'week', 'month') or not valid_amount(amount):
        return None
    return dict(usd=amount, quota_seconds=period_seconds(quota, now=now),
                period='calendar_month' if pay == 'month' else pay)


def quota_amount(quota):
    # Selected-suite estimates accept the observed account delta. Contamination
    # labels do not turn that estimate into a per-call price or invoice charge.
    boundary = dict(quota, contaminated=False)
    if boundary.get('quota_unavailable_reason') == 'contamination_uncontrolled':
        boundary['quota_unavailable_reason'] = None
    if not clean_quota(boundary) or not quota.get('payment_policy'):
        return None
    date = datetime.fromisoformat(quota['after']['captured_at'].replace('Z', '+00:00'))
    return quota['pct_point_delta'] * payment_per_point(quota['payment_policy'], now=date)


def configured_quota_estimate(quota, rows):
    boundary = dict(quota, contaminated=False)
    if boundary.get('quota_unavailable_reason') == 'contamination_uncontrolled':
        boundary['quota_unavailable_reason'] = None
    if not clean_quota(boundary) or not rows:
        return None
    date = datetime.fromisoformat(quota['after']['captured_at'].replace('Z', '+00:00'))
    policies = [configured_payment_policy(row.get('compatibility') or {}, now=date) for row in rows]
    policy = policies[0]
    if (policy is None or any(other != policy for other in policies)
            or quota.get('payment_policy') not in (None, policy)
            or not quota_calls_compatible(rows, policy)):
        return None
    return dict(estimated_cost_usd=quota_amount(dict(quota, payment_policy=policy)),
                policy=policy, policy_source='configured_allocation' if quota.get('payment_policy') is None else 'provider_quote')


def quota_calls_compatible(rows, policy):
    """Absent call quotes are unknown; conflicting billing evidence is different."""
    providers = {(row.get('compatibility') or {}).get('_COST_PROVIDER_IDENTITY') for row in rows}
    providers.discard(None)
    if len(providers) > 1:
        return False
    for row in rows:
        for call in row.get('calls', ()):
            for evidence in (call, *(call.get('transport_attempts') or {}).get('attempts', ())):
                quote = evidence.get('quote') or {}
                if (quote.get('payment') not in (None, policy)
                        or (quote.get('accounting') or {}).get('billing_mode') in ('api_key', 'metered')
                        or qualified_charge(evidence.get('billing'), evidence.get('usage') or {}) is not None):
                    return False
    return True


def _sole_selection(manifest, rows, test):
    if not rows or manifest.get('status') != 'completed':
        return False
    base = f"{manifest.get('repo')}::{test.node_id}"
    if test.id != base:
        return False
    if manifest.get('mode') == 'integration':
        return (len(rows) == 1 and rows[0]['test_id'] == base and rows[0]['outcomes'] == ['passed']
                and sum(name.endswith('-result.json') for name in manifest.get('files', {})) == 1)
    metadata = manifest.get('metadata')
    if type(metadata) is not dict:
        return False
    outcomes = metadata.get('e2e_outcomes')
    if manifest.get('mode') != 'e2e' or type(outcomes) is not list or len(outcomes) != 1:
        return False
    ledger = outcomes[0]
    if type(ledger) is not dict:
        return False
    statuses, files = ledger.get('attempt_statuses'), ledger.get('result_files')
    if (type(statuses) is not list or type(files) is not list or type(ledger.get('attempts')) is not int
            or any(type(status) is not str or status not in {'passed', 'failed', 'broken', 'error', 'skipped', 'cancelled', 'interrupted'} for status in statuses)
            or any(type(name) is not str for name in files) or 'e2e_reruns' not in metadata):
        return False
    expected_history = sha256(f"{manifest['repo']}:e2e:{test.node_id}".encode()).hexdigest()
    if (ledger.get('history_id') != expected_history or ledger.get('complete') is not True
            or ledger.get('final_status') != 'passed' or not statuses
            or ledger.get('first_status') != statuses[0] or ledger.get('attempts') != len(rows)
            or len(statuses) != len(rows) or len(files) != len(rows) or len(set(files)) != len(files)
            or set(files) != {name for name in manifest.get('files', {}) if name.endswith('-result.json')}):
        return False
    from api_agent.testing.confirmations import confirmation_outcome
    try:
        if confirmation_outcome(statuses, metadata.get('e2e_reruns')) != ('passed', False):
            return False
    except ValueError:
        return False
    expected = {base if index == 1 else f'{base}::confirmation:{index}': [status]
                for index, status in enumerate(statuses, 1)}
    return (len({row['test_id'] for row in rows}) == len(rows)
            and all(row.get('attempts') == 1 and expected.get(row['test_id']) == row['outcomes'] for row in rows))


def quota_predictions_for(tests, records, fingerprints, settings, *, ignored_settings=None):
    """Learn only a sole logical test's whole run, including its confirmations.

    A multi-test boundary cannot be apportioned without additional evidence.
    Zero changes remain visible observations, not zero-cost training samples.
    """
    if not compatibility_complete(settings):
        return {}
    result = {}
    variants = {}
    for manifest, artifact in records:
        variants.setdefault(artifact.get('run_id'), []).append((manifest, artifact))
    target = matching_compatibility(settings, ignored_settings)
    for test in tests:
        samples, finished = [], []
        for values in variants.values():
            manifest, artifact = values[0]
            if any(value != values[0] for value in values[1:]):
                continue
            rows = artifact.get('tests') or []
            if (not _sole_selection(manifest, rows, test) or fingerprints.get(test.id) is None
                    or any(row.get('node_id') != test.node_id or row.get('source_fingerprint') != fingerprints[test.id]
                           or matching_compatibility(row.get('compatibility'), ignored_settings) != target for row in rows)):
                continue
            observed = configured_quota_estimate(artifact.get('quota') or {}, rows)
            if observed is None or observed['estimated_cost_usd'] <= 0:
                continue
            samples.append(observed['estimated_cost_usd'])
            finished.append(manifest['finished_at'])
        if samples:
            result[test.id] = dict(estimated_cost_usd=math.fsum(samples) / len(samples), known_zero=False,
                basis='historical_selected_quota', sample_count=len(samples), variation=max(samples) - min(samples),
                freshness=max(finished), quota_eligible=False, call_signature=None)
    return result
