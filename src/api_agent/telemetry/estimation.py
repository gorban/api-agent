"""Pure historical arithmetic means and conservative quota calibration."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
import uuid
from api_agent.providers.costs import period_seconds


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


def matching_compatibility(value, ignored_settings=None):
    """Ignore only declared non-cost settings in a copy of raw evidence."""
    result = deepcopy(value)
    settings = result.get('_COST_REQUEST_SETTINGS') if isinstance(result, dict) else None
    if isinstance(settings, dict):
        for name in ignored_settings or ():
            settings.pop(name, None)
    return result


def quota_reset_observed(quota):
    """Exclude discontinuous allocation evidence, retaining it for diagnostics."""
    if quota.get('quota_unavailable_reason') in {'changed_window', 'changed_allocation', 'reset_boundary_crossed', 'negative_delta'}:
        return True
    if type(quota.get('pct_point_delta')) in (int, float) and quota['pct_point_delta'] < 0:
        return True
    before, after = quota.get('before') or {}, quota.get('after') or {}
    for field in ('allocation_id', 'window_id', 'reset_at'):
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


def reported_charge(row):
    from api_agent.providers.costs import qualified_charge
    calls = row.get('calls')
    if row.get('calls_complete') is not True or not isinstance(calls, list) or not calls or row.get('observed_call_count', len(calls)) != len(calls):
        return None
    amounts = []
    for call in calls:
        metadata = (call.get('quote') or {}).get('accounting') or {}
        if metadata.get('billing_mode') != 'api_key':
            return None
        amount = qualified_charge(metadata.get('billing'), call.get('usage') or {})
        if amount is None:
            return None
        amounts.append(amount)
    return math.fsum(amounts)


def execution_cost(row, card, *, frozen_fallback=False):
    """Prefer qualified charges per call, keeping list-price fallback separate."""
    from api_agent.providers.costs import valid_amount
    calls = row.get('calls')
    if (row.get('calls_complete') is not True or not isinstance(calls, list)
            or row.get('observed_call_count', len(calls)) != len(calls)):
        return None, 'unavailable'
    amounts, bases = [], set()
    for call in calls:
        single = {'calls_complete': True, 'calls': [call]}
        amount = reported_charge(single)
        basis = 'reported_charge'
        if amount is None:
            amount = priced(single, card)
            if amount is None and frozen_fallback:
                recorded = (call.get('quote') or {}).get('list_cost_usd')
                if valid_amount(recorded):
                    amount = recorded
            basis = 'list_price'
        if amount is None:
            return None, 'unavailable'
        amounts.append(amount)
        bases.add(basis)
    return math.fsum(amounts), next(iter(bases)) if len(bases) == 1 else 'mixed' if bases else 'list_price'


def overlapping_quota(artifact, records):
    quota = artifact.get('quota') or {}
    target = quota_interval(quota)
    if target is None:
        return False
    start, end, allocation, window = target
    for _, other in records:
        other_quota = other.get('quota') or {}
        interval = quota_interval(other_quota)
        if other.get('run_id') is not None and other.get('run_id') == artifact.get('run_id'):
            if other != artifact:
                return True
            continue
        if interval is None:
            continue
        begin, finish, other_allocation, other_window = interval
        if allocation == other_allocation and window == other_window and start < finish and begin < end:
            return True
    return False


def quota_allocation(quota):
    before, after = quota.get('before') or {}, quota.get('after') or {}
    identity = before.get('allocation_id')
    return identity if isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{64}', identity) and identity == after.get('allocation_id') else None


def canonical_observation(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def quota_window(quota):
    before = quota.get('before') or {}
    return before.get('window_id'), before.get('reset_at')


def quota_interval(quota):
    """Parse overlap evidence without applying sample eligibility filters."""
    try:
        allocation = quota_allocation(quota)
        window = quota_window(quota)
        before, after = quota['before'], quota['after']
        start = datetime.fromisoformat(before['captured_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(after['captured_at'].replace('Z', '+00:00'))
        if allocation is None or any(value is None for value in window) or window != (after.get('window_id'), after.get('reset_at')) or start.tzinfo is None or end.tzinfo is None or end < start:
            return None
        return start, end, allocation, window
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def shared_boundary(left, right):
    after, before = left['after'], right['before']
    fields = ('allocation_id', 'observation_id', 'window_id', 'reset_at', 'pct_used', 'pct_step', 'captured_at')
    return canonical_observation(after.get('observation_id')) and all(after.get(field) is not None and after.get(field) == before.get(field) for field in fields)


def clean_quota(quota):
    try:
        before, after = quota['before'], quota['after']
        start = datetime.fromisoformat(before['captured_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(after['captured_at'].replace('Z', '+00:00'))
        reset = datetime.fromisoformat(after['reset_at'].replace('Z', '+00:00'))
        before_pct, after_pct, delta = before.get('pct_used'), after.get('pct_used'), quota.get('pct_point_delta')
        valid_pct = (all(type(value) in (int, float) and math.isfinite(value) for value in (before_pct, after_pct, delta))
                     and math.isclose(after_pct - before_pct, delta, rel_tol=0,
                                      abs_tol=max(math.ulp(before_pct), math.ulp(after_pct), math.ulp(delta)))
                     and (start < end or delta == 0))
        valid_observations = all(snapshot.get('observation_id') is None or canonical_observation(snapshot.get('observation_id'))
                                 for snapshot in (before, after))
        return (valid_pct and valid_observations and not quota_reset_observed(quota) and not quota.get('quota_unavailable_reason') and not quota.get('contaminated')
                and before['window_id'] is not None and before['window_id'] == after['window_id']
                and before['reset_at'] == after['reset_at'] and start.tzinfo is not None
                and end.tzinfo is not None and reset.tzinfo is not None and start <= end < reset)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def calibration(records, card, *, overlap_records=None, ignored_settings=None):
    # Inspect all available intervals before choosing completed, compatible samples.
    # An unfinished or incompatible run can still contaminate an allocation.
    intervals = []
    variants = {}
    conflicted = set()
    for manifest, artifact in list(records) + list(overlap_records or ()):
        run_id = artifact.get('run_id')
        if run_id is None:
            continue
        if run_id in variants and variants[run_id] != (manifest, artifact):
            conflicted.add(run_id)
        variants.setdefault(run_id, (manifest, artifact))
    seen_intervals = set()
    for _, artifact in records if overlap_records is None else overlap_records:
        run_id = artifact.get('run_id')
        if run_id is not None and run_id in seen_intervals and run_id not in conflicted:
            continue
        if run_id is not None:
            seen_intervals.add(run_id)
        interval = quota_interval(artifact.get('quota') or {})
        if interval is not None:
            intervals.append((*interval, artifact))
    contaminated = set(conflicted)
    for index, (start, end, allocation, window, artifact) in enumerate(intervals):
        for other_start, other_end, other_allocation, other_window, other_artifact in intervals[index + 1:]:
            if artifact.get('run_id') != other_artifact.get('run_id') and allocation == other_allocation and window == other_window and start < other_end and other_start < end:
                contaminated.update((artifact.get('run_id'), other_artifact.get('run_id')))

    groups = {}
    seen_runs = set()
    for manifest, artifact in records:
        run_id = artifact.get('run_id')
        if run_id is None or run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        quota = artifact.get('quota') or {}
        before, after = quota.get('before') or {}, quota.get('after') or {}
        allocation = quota_allocation(quota)
        delta = quota.get('pct_point_delta')
        steps = (before.get('pct_step'), after.get('pct_step'))
        tests = artifact.get('tests') or []
        if (manifest.get('status') != 'completed' or run_id in contaminated or not clean_quota(quota)
                or allocation is None or quota.get('payment_policy') is None or not artifact.get('rate_card') or not tests
                or type(delta) not in (int, float) or not math.isfinite(delta) or delta < 0
                or any(type(step) not in (int, float) or not math.isfinite(step) or step <= 0 for step in steps)
                or any(not quota_allowed(row) or not row.get('outcomes') or row['outcomes'][-1] != 'passed'
                       or not compatibility_complete(row.get('compatibility')) or call_signature(row) is None for row in tests)):
            continue
        amounts = [priced(row, artifact['rate_card']) for row in tests]
        if any(amount is None for amount in amounts):
            continue
        amount = math.fsum(amounts)
        if amount <= 0:
            continue
        group = calibration_key(artifact, ignored_settings=ignored_settings)
        groups.setdefault(group, {}).setdefault(allocation, []).append(
            (before['captured_at'], after['captured_at'], run_id, quota, delta, amount))

    qualified = {}
    for group, allocations in groups.items():
        evidence = {}
        for allocation, samples in allocations.items():
            samples.sort(key=lambda sample: (sample[0], sample[1], str(sample[2])))
            spans = []
            for sample in samples:
                if spans and spans[-1][-1][1] == sample[0] and quota_window(spans[-1][-1][3]) == quota_window(sample[3]) and shared_boundary(spans[-1][-1][3], sample[3]):
                    spans[-1].append(sample)
                else:
                    spans.append([sample])
            delta = math.fsum(sample[4] for sample in samples)
            amount = math.fsum(sample[5] for sample in samples)
            uncertainty = math.fsum((span[0][3]['before']['pct_step'] + span[-1][3]['after']['pct_step']) / 2 for span in spans)
            if len(samples) < 3 or delta <= 0 or uncertainty > delta * .25:
                continue
            item = evidence.setdefault(allocation, {'sample_count': 0, 'span_count': 0, 'delta': 0., 'amount_usd': 0., 'uncertainty': 0., 'spans': []})
            item['sample_count'] += len(samples)
            item['span_count'] += len(spans)
            item['delta'] += delta
            item['amount_usd'] += amount
            item['uncertainty'] += uncertainty
            item['spans'].extend({'run_ids': [sample[2] for sample in span], 'window_id': quota_window(span[0][3])[0],
                                  'reset_at': quota_window(span[0][3])[1], 'before': span[0][3]['before']['captured_at'],
                                  'after': span[-1][3]['after']['captured_at'],
                                  'delta': math.fsum(sample[4] for sample in span),
                                  'uncertainty': (span[0][3]['before']['pct_step'] + span[-1][3]['after']['pct_step']) / 2}
                                 for span in spans)
        if evidence:
            delta = math.fsum(item['delta'] for item in evidence.values())
            amount = math.fsum(item['amount_usd'] for item in evidence.values())
            uncertainty = math.fsum(item['uncertainty'] for item in evidence.values())
            qualified[group] = {'pct_per_usd': delta / amount,
                                'sample_count': sum(item['sample_count'] for item in evidence.values()),
                                'relative_uncertainty': uncertainty / delta, 'allocations': evidence}
    return qualified


class EstimateSnapshot:
    def __init__(self, predictions, *, records=(), card=None, overlap_records=None, now=None, ignored_settings=None):
        self.predictions = deepcopy(predictions)
        self.records = deepcopy(records)
        self.card = deepcopy(card)
        self.overlap_records = deepcopy(overlap_records)
        self.now = now or datetime.now(timezone.utc)
        self.ignored_settings = deepcopy(ignored_settings)

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
        if policy is not None and eligible and quota_allocation(quota) is not None:
            group = calibration_key(artifact, ignored_settings=self.ignored_settings)
            calibrated = calibration(self.records, self.card, overlap_records=self.overlap_records,
                                     ignored_settings=self.ignored_settings).get(group)
            if calibrated is not None:
                scale, basis = calibrated['pct_per_usd'] * payment_per_point(policy, now=self.now), 'historical_quota'
                calibration_details = dict(calibrated, scale_factor=scale, provenance=json.loads(group))
            else:
                known = list(amounts.values())
                delta = quota.get('pct_point_delta')
                steps = [(quota.get(phase) or {}).get('pct_step') for phase in ('before', 'after')]
                if (clean_quota(quota) and quota_allocation(quota) is not None and not overlapping_quota(artifact, self.overlap_records or self.records) and known
                        and all(amount is not None for amount in known) and math.fsum(known) > 0
                        and type(delta) in (int, float) and math.isfinite(delta) and delta > 0
                        and all(type(step) in (int, float) and math.isfinite(step) and step > 0 for step in steps)
                        and sum(steps) / 2 <= delta * .25):
                    scale, basis = delta * payment_per_point(policy, now=self.now) / math.fsum(known), 'current_quota'
        execution = {row['test_id']: execution_cost(row, self.card, frozen_fallback=True) for row in artifact['tests']}
        charges = {row['test_id']: execution[row['test_id']] for row in artifact['tests']
                   if any(reported_charge({'calls_complete': True, 'calls': [call]}) is not None
                          for call in row.get('calls') or ())}
        return {identity: {'estimated_cost_usd': charges[identity][0] if identity in charges else None if amount is None else amount * scale,
                           'known_zero': any(row['test_id'] == identity and row.get('calls_complete') is True and row.get('observed_call_count') == 0 and not row['calls'] for row in artifact['tests']), 'basis': charges[identity][1] if identity in charges else 'unavailable' if amount is None else basis,
                           'sample_count': self.predictions.get(identity, {}).get('sample_count', 0), 'calibration': deepcopy(calibration_details),
                           'ignored_cost_settings': tuple(self.ignored_settings or ())}
                for identity, amount in amounts.items()}

    def completed_averages(self, artifact, stabilized):
        """Include this finished observation once, without refitting calibration."""
        from types import SimpleNamespace
        records = [(manifest, prior) for manifest, prior in self.records
                   if artifact.get('run_id') is None or prior.get('run_id') != artifact['run_id']]
        records.append(({'finished_at': datetime.now(timezone.utc).isoformat()}, artifact))
        averages = {}
        for row in artifact['tests']:
            test = SimpleNamespace(id=row['test_id'], node_id=row['node_id'])
            prediction = predictions_for((test,), records, self.card,
                {test.id: row.get('source_fingerprint')}, row.get('compatibility'),
                ignored_settings=self.ignored_settings)[test.id]
            # Reuse only pre-run-qualified calibration, never the new sample.
            calibrated = stabilized.get(test.id, {}).get('calibration')
            if prediction['quota_eligible'] and calibrated is not None:
                scale = calibrated['scale_factor']
                prediction['estimated_cost_usd'] *= scale
                prediction['variation'] *= scale
                prediction['basis'] = 'historical_quota'
                prediction['calibration'] = deepcopy(calibrated)
            averages[test.id] = prediction
        return averages

    def synchronize(self):
        return self

    def close(self):
        return None

    def ingest(self, directory, manifest):
        return None


def calibration_key(artifact, *, ignored_settings=None):
    card = artifact.get('rate_card')
    epoch = None if card is None else {name: card.get(name) for name in ('source', 'revision', 'currency', 'unit', 'models')}
    return key({'policy': (artifact.get('quota') or {}).get('payment_policy'), 'rate_card': epoch,
                'compatibility': sorted({key(matching_compatibility(row.get('compatibility'), ignored_settings)) for row in artifact.get('tests', [])}),
                'model_signatures': sorted({call_signature(row) for row in artifact.get('tests', []) if call_signature(row) is not None})})


def payment_per_point(policy, *, now=None):
    seconds = (policy['quota_seconds'] if policy['period'] == 'quota' else
               period_seconds(policy['period'], now=now))
    return policy['usd'] * policy['quota_seconds'] / seconds / 100


def call_signature(row):
    """Different actual revisions/settings never collapse into the same sample."""
    signatures = set()
    for call in row.get('calls', []):
        metadata = (call.get('quote') or {}).get('accounting')
        if (not metadata or not compatibility_complete(metadata.get('compatibility'))
                or metadata['compatibility'] != row.get('compatibility')):
            return None
        signature = {name: metadata.get(name) for name in ('requested_model', 'catalog_model', 'actual_model', 'serving_revision', 'service_tier', 'billing_mode')}
        signature['cache_policy'] = metadata.get('cache_policy', 'unknown')
        signatures.add(key(signature))
    return key(sorted(signatures))


def predictions_for(tests, records, card, fingerprints, compatibility, *, ignored_settings=None):
    result = {}
    immutable = {}
    conflicting = set()
    for manifest, artifact in records:
        run_id = artifact.get('run_id')
        if run_id is None:
            continue
        if run_id in immutable and immutable[run_id] != (manifest, artifact):
            conflicting.add(run_id)
        immutable.setdefault(run_id, (manifest, artifact))
    for test in tests:
        samples, signatures, zeroes, finished, quota_eligible = [], set(), [], [], []
        has_token_history = False
        reported_samples = False
        seen_runs = set()
        for manifest, artifact in records:
            run_id = artifact.get('run_id')
            if run_id in conflicting or (run_id is not None and run_id in seen_runs):
                continue
            if run_id is not None:
                seen_runs.add(run_id)
            if quota_reset_observed(artifact.get('quota') or {}):
                continue
            for row in artifact.get('tests', []):
                if (compatibility_complete(compatibility) and matching_compatibility(row.get('compatibility'), ignored_settings) == matching_compatibility(compatibility, ignored_settings) and fingerprints.get(test.id) is not None
                        and row.get('source_fingerprint') == fingerprints[test.id] and row.get('node_id') == test.node_id):
                    has_token_history |= any(type(value) is int and value > 0
                        for call in row.get('calls', []) for value in (call.get('usage') or {}).values())
                    if not row.get('outcomes') or row['outcomes'][-1] != 'passed':
                        continue
                    signature = call_signature(row)
                    amount, sample_basis = execution_cost(row, card) if signature is not None else (None, 'unavailable')
                    reported_samples |= amount is not None and sample_basis in ('reported_charge', 'mixed')
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
                           'basis': ('historical_reported_charge' if reported_samples else 'historical_list_price') if samples else 'unavailable', 'sample_count': len(samples),
                           'call_signature': next(iter(signatures)) if len(signatures) == 1 else None, 'quota_eligible': bool(samples) and all(quota_eligible), 'variation': None if not samples else max(samples) - min(samples), 'freshness': max(finished, default=None),
                           'ignored_cost_settings': tuple(ignored_settings or ())}
    return result
