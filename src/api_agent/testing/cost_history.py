"""Read cached predictions immediately; storage refresh is optional and bounded."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json
from hashlib import sha256

from api_agent.telemetry.estimation import EstimateSnapshot, predictions_for, calibration, key, payment_per_point, quota_allowed, calibration_key
from api_agent.telemetry.history import HistoryCache, MAX_BYTES
from .collection import repository_label
from .storage import RunStorage
from .cost_context import cost_config
from api_agent.providers.progress import report_timeout_sync


def allocation_scope(config):
    """Hash validated entry identifiers; never resolve or retain credentials."""
    index = config.get('UNATTENDED_USER_INDEX')
    auth = config.get('UNATTENDED_AUTH')
    if type(index) is not int or index < 0 or not isinstance(auth, dict) or set(auth) != {'password_manager', 'roles'}:
        return None
    manager, roles = auth['password_manager'], auth['roles']
    if (not isinstance(manager, dict) or set(manager) != {'manager', 'cli_path'} or not isinstance(roles, dict)
            or not roles or any(not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in manager.values())):
        return None
    role = config.get('LLM_CREDENTIAL_ROLE')
    entries = roles.get(role) if isinstance(role, str) and role.strip() else None
    if not isinstance(entries, list) or index >= len(entries):
        return None
    entry = entries[index]
    if (not isinstance(entry, dict) or set(entry) != {'vault', 'user'}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in entry.values())):
        return None
    selected = {role: entry}
    fingerprints = [config.get(name) for name in ('_COST_PROVIDER_IDENTITY', '_COST_PACK_IDENTITY')]
    settings = config.get('_COST_REQUEST_SETTINGS')
    if (not selected or any(not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in fingerprints)
            or not isinstance(settings, dict) or any(not isinstance(name, str) or not isinstance(value, str)
                or len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for name, value in settings.items())):
        return None
    return sha256(json.dumps([fingerprints, settings, role, index, manager, selected], sort_keys=True).encode()).hexdigest()


class HistoricalEstimator(EstimateSnapshot):
    def __init__(self, tests, config, cache, origin, repo, card, storage=None, now=None):
        from api_agent.telemetry.accounting import compatibility, source_fingerprint
        self.tests, self.config, self.cache = tuple(tests), dict(config), cache
        self.now = now or datetime.now(timezone.utc)
        self.scope = allocation_scope(config)
        if 'UNATTENDED_AUTH' in config:
            self.config.pop('_COST_QUOTA_WINDOW_ID', None)
            config = self.config
        if self.scope is not None and cache is not None:
            window = cache.bound_window(origin, self.scope, self.now)
            if window is not None:
                self.config['_COST_QUOTA_WINDOW_ID'] = window
                config = self.config
        self.origin, self.repo, self.storage = origin, repo, storage
        fingerprints = {test.id: source_fingerprint(test) for test in tests}
        self.fingerprints, self.compatibility = fingerprints, compatibility(config)
        records = cache.records(origin) if cache is not None else []
        cutoff = self.now - timedelta(days=config.get('TEST_COST_HISTORY_DAYS', 7))
        records = [(manifest, artifact) for manifest, artifact in records if cutoff <= datetime.fromisoformat(manifest['finished_at'].replace('Z', '+00:00')) <= self.now]
        overlap_records = cache.records(None) if cache is not None else []
        predictions = predictions_for(tests, records, card, fingerprints, self.compatibility)
        # A policy is learned only from exactly compatible successful histories.
        policies = {}
        for _, artifact in records:
            policy = (artifact.get('quota') or {}).get('payment_policy')
            if policy and any(row.get('compatibility') == self.compatibility for row in artifact.get('tests', [])):
                policies[key(policy)] = policy
        if len(policies) == 1 and config.get('_COST_QUOTA_WINDOW_ID') is not None:
            policy = next(iter(policies.values()))
            groups = calibration(records, card, overlap_records=overlap_records)
            group = calibration_key({'rate_card': card, 'quota': {'payment_policy': policy, 'before': {'window_id': config['_COST_QUOTA_WINDOW_ID']}}, 'tests': [{'compatibility': self.compatibility}]})
            expected = json.loads(group)
            qualified = [(candidate, value) for candidate, value in groups.items() if all(json.loads(candidate)[name] == expected[name] for name in ('window_id', 'policy', 'rate_card', 'compatibility'))]
            if len(qualified) == 1:
                for row in predictions.values():
                    if row['estimated_cost_usd'] is not None and row.get('quota_eligible') is True and row.get('call_signature') in json.loads(qualified[0][0])['model_signatures']:
                        row['estimated_cost_usd'] *= qualified[0][1]['pct_per_usd'] * payment_per_point(policy, now=self.now)
                        row['basis'] = 'historical_quota'
                        row['variation'] *= qualified[0][1]['pct_per_usd'] * payment_per_point(policy, now=self.now)
                        row['calibration'] = dict(qualified[0][1], scale_factor=qualified[0][1]['pct_per_usd'] * payment_per_point(policy, now=self.now), provenance=json.loads(qualified[0][0]))
        super().__init__(predictions, records=records, card=card, overlap_records=overlap_records, now=self.now)

    def synchronize(self):
        if self.cache is None or self.storage is None:
            return self
        error = False
        try:
            with report_timeout_sync('Cost history synchronization', 30) as budget:
                for manifest, payload in self.storage.accounting_history(self.repo, budget=budget):
                    if budget.remaining == 0:
                        raise TimeoutError()
                    self.cache.ingest(self.origin, manifest, payload)
        except Exception:
            error = True
        try:
            replacement = HistoricalEstimator(self.tests, self.config, self.cache, self.origin, self.repo, self.card, self.storage, self.now)
            replacement.sync_unavailable = error
            return replacement
        except Exception:
            self.sync_unavailable = True
            return self

    def details(self, ids):
        text = super().details(ids)
        text += ('\nQuota calibration: selected account window unavailable; using list prices.' if self.config.get('_COST_QUOTA_WINDOW_ID') is None else '')
        return text + ('\nHistory synchronization unavailable.' if getattr(self, 'sync_unavailable', False) else '')

    def ingest(self, directory, manifest):
        try:
            if self.cache is not None:
                # Reuse the storage reader's no-follow regular-file checks.
                payload = RunStorage._read_source(Path(directory) / 'accounting.json', max_bytes=MAX_BYTES)
                if len(payload) <= MAX_BYTES:
                    self.cache.ingest(self.origin, manifest, payload)
                    if self.scope is not None:
                        self.cache.bind(self.origin, self.scope, manifest['run_id'])
        except Exception:
            pass


def estimator_for(context, tests, config, *, now=None, cache=None, rate_card=None):
    from api_agent.providers.rates import cached_rate_card
    config = cost_config(context, config)
    card = rate_card or config.get('_COST_RATE_CARD') or cached_rate_card(context, config)
    repo = repository_label(context, context.owner_root)
    origin = sha256(json.dumps([config.get(name) for name in ('ALLURE_S3_ENDPOINT', 'ALLURE_S3_BUCKET', 'ALLURE_S3_PREFIX')] + [repo]).encode()).hexdigest()
    try:
        cache = cache if cache is not None else HistoryCache()
        storage = None
        if config.get('ALLURE_S3_BUCKET'):
            storage = RunStorage(config['ALLURE_S3_BUCKET'], prefix=config.get('ALLURE_S3_PREFIX', 'allure'),
                                 endpoint_url=config.get('ALLURE_S3_ENDPOINT'), region_name=config.get('ALLURE_S3_REGION'))
        return HistoricalEstimator(tests, config, cache, origin, repo, card, storage, now)
    except Exception:
        return EstimateSnapshot({test.id: {'estimated_cost_usd': None, 'known_zero': False, 'basis': 'unavailable', 'sample_count': 0} for test in tests}, card=card)
