"""Pure historical arithmetic means and conservative quota calibration."""
from copy import deepcopy
from datetime import datetime, timezone
import calendar
import json
import math


def compatibility_complete(value):
    if not isinstance(value, dict):
        return False
    required = ('_COST_PROVIDER_IDENTITY', 'LLM_DEFAULT_MODEL', 'LLM_CACHE_POLICY', 'E2E_RERUNS', '_COST_PACK_IDENTITY', '_COST_REQUEST_SETTINGS')
    return all(value.get(name) is not None for name in required)


def quota_allowed(row):
    calls = row.get('calls') or []
    return row.get('calls_complete') is True and row.get('observed_call_count', len(calls)) == len(calls) and (not calls or all(((call.get('quote') or {}).get('accounting') or {}).get('billing_mode') == 'subscription' for call in calls))


def key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def quota_reset_observed(quota):
    """Exclude discontinuous allocation evidence, retaining it for diagnostics."""
    if quota.get('quota_unavailable_reason') in {'changed_window', 'reset_boundary_crossed', 'negative_delta'}:
        return True
    if type(quota.get('pct_point_delta')) in (int, float) and quota['pct_point_delta'] < 0:
        return True
    before, after = quota.get('before') or {}, quota.get('after') or {}
    for field in ('window_id', 'reset_at'):
        if before.get(field) is not None and after.get(field) is not None and before[field] != after[field]:
            return True
    if (type(before.get('pct_used')) in (int, float) and type(after.get('pct_used')) in (int, float)
            and after['pct_used'] < before['pct_used']):
        return True
    for snapshot in (before, after):
        try:
            captured = datetime.fromisoformat(snapshot['captured_at'].replace('Z', '+00:00'))
            reset = datetime.fromisoformat(snapshot['reset_at'].replace('Z', '+00:00'))
            if captured >= reset:
                return True
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    return False


def priced(row, card):
    from api_agent.providers.rates import price_usage
    calls = row.get('calls')
    if row.get('calls_complete') is not True or not isinstance(calls, list) or row.get('observed_call_count', len(calls)) != len(calls):
        return None
    amounts = []
    for call in calls:
        quote = call.get('quote') or {}
        evidence = quote.get('accounting')
        if not evidence or evidence.get('service_tier') not in (None, 'standard', 'default') or not evidence.get('actual_model') or not isinstance(evidence.get('usage', call.get('usage')), dict):
            return None
        try:
            amount = price_usage(card, evidence['actual_model'], evidence.get('usage', call.get('usage')))
        except (ValueError, TypeError):
            return None
        if amount is None:
            return None
        amounts.append(amount)
    return math.fsum(amounts)


def overlapping_quota(artifact, records):
    quota = artifact.get('quota') or {}
    if not clean_quota(quota):
        return False
    start = datetime.fromisoformat(quota['before']['captured_at'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(quota['after']['captured_at'].replace('Z', '+00:00'))
    for _, other in records:
        other_quota = other.get('quota') or {}
        if other != artifact and clean_quota(other_quota):
            begin = datetime.fromisoformat(other_quota['before']['captured_at'].replace('Z', '+00:00'))
            finish = datetime.fromisoformat(other_quota['after']['captured_at'].replace('Z', '+00:00'))
            if start < finish and begin < end:
                return True
    return False


def clean_quota(quota):
    try:
        before, after = quota['before'], quota['after']
        start = datetime.fromisoformat(before['captured_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(after['captured_at'].replace('Z', '+00:00'))
        reset = datetime.fromisoformat(after['reset_at'].replace('Z', '+00:00'))
        return (not quota_reset_observed(quota) and not quota.get('quota_unavailable_reason') and not quota.get('contaminated')
                and before['window_id'] is not None and before['window_id'] == after['window_id']
                and before['reset_at'] == after['reset_at'] and start.tzinfo is not None
                and end.tzinfo is not None and reset.tzinfo is not None and start <= end < reset)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def calibration(records, card, *, overlap_records=None):
    groups = {}
    intervals = []
    for manifest, artifact in (records if overlap_records is None else overlap_records):
        try:
            quota = artifact.get('quota') or {}
            if quota_reset_observed(quota):
                continue
            start = datetime.fromisoformat((quota.get('before') or {})['captured_at'].replace('Z', '+00:00'))
            end = datetime.fromisoformat((quota.get('after') or {})['captured_at'].replace('Z', '+00:00'))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        intervals.append((start, end, artifact))
    for start, end, artifact in intervals:
        if not any(candidate == artifact and manifest.get('status') == 'completed' for manifest, candidate in records):
            continue
        quota = artifact.get('quota') or {}
        before, after = quota.get('before') or {}, quota.get('after') or {}
        delta = quota.get('pct_point_delta')
        policy = quota.get('payment_policy')
        if not clean_quota(quota):
            continue
        steps = [before.get('pct_step'), after.get('pct_step')]
        if (any(not quota_allowed(row) for row in artifact.get('tests', [])) or quota.get('quota_unavailable_reason') is not None or quota.get('contaminated') or policy is None
                or type(delta) not in (int, float) or not math.isfinite(delta) or delta < 0
                or any(type(step) not in (int, float) or not math.isfinite(step) or step <= 0 for step in steps)
                or any(other != artifact and start < finish and begin < end for begin, finish, other in intervals)):
            continue
        tests = artifact.get('tests', [])
        if any(not row.get('outcomes') or row['outcomes'][-1] != 'passed' or not compatibility_complete(row.get('compatibility')) or call_signature(row) is None for row in tests):
            continue
        amounts = [priced(row, artifact.get('rate_card') or card) for row in tests]
        if not amounts or any(amount is None for amount in amounts):
            continue
        total = math.fsum(amounts)
        if total <= 0:
            continue
        group = calibration_key(artifact)
        groups.setdefault(group, []).append((delta, total, sum(steps) / 2))
    qualified = {}
    for group, samples in groups.items():
        delta = math.fsum(row[0] for row in samples)
        uncertainty = math.fsum(row[2] for row in samples)
        amount = math.fsum(row[1] for row in samples)
        if len(samples) >= 3 and delta > 0 and uncertainty <= delta * .25:
            qualified[group] = {'pct_per_usd': delta / amount, 'sample_count': len(samples), 'relative_uncertainty': uncertainty / delta}
    return qualified


class EstimateSnapshot:
    def __init__(self, predictions, *, records=(), card=None, overlap_records=None, now=None):
        self.predictions = deepcopy(predictions)
        self.records = deepcopy(records)
        self.card = deepcopy(card)
        self.overlap_records = deepcopy(overlap_records)
        self.now = now or datetime.now(timezone.utc)

    def total(self, ids):
        rows = [self.predictions.get(identity, {}) for identity in dict.fromkeys(ids)]
        known = [row['estimated_cost_usd'] for row in rows if row.get('estimated_cost_usd') is not None]
        return {'estimated_cost_usd': math.fsum(known) if known else None, 'incomplete': len(known) != len(rows),
                'known_count': len(known), 'unknown_count': len(rows) - len(known),
                'known_zero': bool(rows) and all(row.get('known_zero') for row in rows)}

    def complete(self, artifact):
        """Stabilize observed use with calibration frozen before this suite began."""
        quota = artifact.get('quota') or {}
        policy = quota.get('payment_policy')
        amounts = {}
        for row in artifact['tests']:
            amount = priced(row, self.card)
            amounts[row['test_id']] = (None if row.get('calls_complete') is not True else row.get('list_cost_usd') if amount is None else amount)
        scale, basis, calibration_details = 1., 'list_price', None
        eligible = all(priced(row, self.card) is not None and compatibility_complete(row.get('compatibility')) and call_signature(row) is not None and quota_allowed(row) for row in artifact['tests'])
        if policy is not None and eligible:
            group = calibration_key(artifact)
            calibrated = calibration(self.records, self.card, overlap_records=self.overlap_records).get(group)
            if calibrated is not None:
                scale, basis = calibrated['pct_per_usd'] * payment_per_point(policy, now=self.now), 'historical_quota'
                calibration_details = dict(calibrated, scale_factor=scale, provenance=json.loads(group))
            else:
                known = list(amounts.values())
                delta = quota.get('pct_point_delta')
                steps = [(quota.get(phase) or {}).get('pct_step') for phase in ('before', 'after')]
                if (clean_quota(quota) and not overlapping_quota(artifact, self.overlap_records or self.records) and known
                        and all(amount is not None for amount in known) and math.fsum(known) > 0
                        and type(delta) in (int, float) and math.isfinite(delta) and delta > 0
                        and all(type(step) in (int, float) and math.isfinite(step) and step > 0 for step in steps)
                        and sum(steps) / 2 <= delta * .25):
                    scale, basis = delta * payment_per_point(policy, now=self.now) / math.fsum(known), 'current_quota'
        return {identity: {'estimated_cost_usd': None if amount is None else amount * scale,
                           'known_zero': any(row['test_id'] == identity and row.get('calls_complete') is True and row.get('observed_call_count') == 0 and not row['calls'] for row in artifact['tests']), 'basis': 'unavailable' if amount is None else basis,
                           'sample_count': self.predictions.get(identity, {}).get('sample_count', 0), 'calibration': deepcopy(calibration_details)}
                for identity, amount in amounts.items()}

    def synchronize(self):
        return self

    def close(self):
        return None

    def ingest(self, directory, manifest):
        return None


def calibration_key(artifact):
    card = artifact.get('rate_card')
    epoch = None if card is None else {name: card.get(name) for name in ('source', 'revision', 'currency', 'unit', 'models')}
    return key({'window_id': ((artifact.get('quota') or {}).get('before') or {}).get('window_id'), 'policy': (artifact.get('quota') or {}).get('payment_policy'), 'rate_card': epoch,
                'compatibility': sorted({key(row.get('compatibility')) for row in artifact.get('tests', [])}),
                'model_signatures': sorted({call_signature(row) for row in artifact.get('tests', []) if call_signature(row) is not None})})


def payment_per_point(policy, *, now=None):
    date = now or datetime.now(timezone.utc)
    seconds = {'calendar_month': calendar.monthrange(date.year, date.month)[1] * 86400,
               'day': 86400, 'week': 604800}.get(policy['period'], policy['quota_seconds'])
    return policy['usd'] * policy['quota_seconds'] / seconds / 100


def call_signature(row):
    """Different actual revisions/settings never collapse into the same sample."""
    signatures = set()
    for call in row.get('calls', []):
        metadata = (call.get('quote') or {}).get('accounting')
        if (not metadata or not compatibility_complete(metadata.get('compatibility'))
                or metadata['compatibility'] != row.get('compatibility')):
            return None
        signatures.add(key({name: metadata.get(name) for name in ('requested_model', 'catalog_model', 'actual_model', 'serving_revision', 'service_tier', 'billing_mode')}))
    return key(sorted(signatures))


def predictions_for(tests, records, card, fingerprints, compatibility):
    result = {}
    for test in tests:
        samples, signatures, zeroes, finished, quota_eligible = [], set(), [], [], []
        has_token_history = False
        for manifest, artifact in records:
            if quota_reset_observed(artifact.get('quota') or {}):
                continue
            for row in artifact.get('tests', []):
                if (compatibility_complete(compatibility) and row.get('compatibility') == compatibility and fingerprints.get(test.id) is not None
                        and row.get('source_fingerprint') == fingerprints[test.id] and row.get('node_id') == test.node_id):
                    has_token_history |= any(type(value) is int and value > 0
                        for call in row.get('calls', []) for value in (call.get('usage') or {}).values())
                    if not row.get('outcomes') or row['outcomes'][-1] != 'passed':
                        continue
                    signature = call_signature(row)
                    amount = priced(row, card) if signature is not None else None
                    if amount is not None:
                        samples.append(amount)
                        quota_eligible.append(quota_allowed(row))
                        signatures.add(signature)
                        zeroes.append(row.get('calls_complete') is True and row.get('observed_call_count') == 0 and row.get('calls') == [])
                        finished.append(manifest.get('finished_at', ''))
        if len(signatures) > 1:
            samples = []
        amount = math.fsum(samples) / len(samples) if samples else None
        result[test.id] = {'estimated_cost_usd': amount, 'known_zero': bool(samples) and all(zeroes),
                           'has_token_history': has_token_history,
                           'basis': 'historical_list_price' if samples else 'unavailable', 'sample_count': len(samples),
                           'call_signature': next(iter(signatures)) if len(signatures) == 1 else None, 'quota_eligible': bool(samples) and all(quota_eligible), 'variation': None if not samples else max(samples) - min(samples), 'freshness': max(finished, default=None)}
    return result
