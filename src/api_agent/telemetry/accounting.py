"""Content-free, versioned call evidence and immutable suite cost artifacts."""
from copy import deepcopy
from hashlib import sha256
import json
import os
import stat
from datetime import datetime
from uuid import UUID
from pathlib import Path

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import result_from_wire
from api_agent.providers.rates import validate_rate_card

_COMPAT = ('_COST_PROVIDER_IDENTITY', 'LLM_DEFAULT_MODEL', 'LLM_CACHE_POLICY', 'LLM_QUOTA_PERIOD', 'LLM_PAY_PERIOD', 'LLM_PAY_PERIOD_LIMIT', 'E2E_RERUNS', '_COST_PACK_IDENTITY', '_COST_REQUEST_SETTINGS')
_METADATA = {'version', 'requested_model', 'catalog_model', 'actual_model', 'serving_revision',
             'service_tier', 'billing_mode', 'rate_card', 'compatibility'}


def compatibility(config):
    """Only non-secret configuration participates; missing fields remain null."""
    return {key: deepcopy(config.get(key)) for key in _COMPAT}


def source_fingerprint(test, *, context=None):
    """Resolve source through the collected root and node ID, never artifact paths."""
    root = test.root
    if root is None and context is not None:
        from api_agent.testing.collection import repository_label
        root = next((candidate for candidate in (context.base_root, context.owner_root)
                     if getattr(test, 'repo', None) == repository_label(context, candidate)), None)
    if root is None:
        return None
    root = Path(root).resolve()
    source = root / test.node_id.split('::', 1)[0]
    try:
        source.resolve().relative_to(root)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(4194305)
        return None if len(raw) > 4194304 else sha256(raw).hexdigest()
    except (ValueError, OSError):
        return None


def _identifier(value, nullable=False):
    return (nullable and value is None) or (type(value) is str and 0 < len(value) <= 200 and value.isprintable())


def _test_identifier(value):
    """Collected parameterized IDs retain their full identity, unlike model IDs."""
    return type(value) is str and bool(value) and value.isprintable()


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _timestamp(value, nullable=False):
    if value is None:
        return nullable
    try:
        return type(value) is str and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def validate_compatibility(value):
    if type(value) is not dict or set(value) != set(_COMPAT):
        raise ContractError('Invalid compatibility shape')
    if any(item is not None and (type(item) not in (str, int, float, bool) or (type(item) is str and (len(item) > 4096 or not item.isprintable()))) for key, item in value.items() if key != '_COST_REQUEST_SETTINGS'):
        raise ContractError('Invalid compatibility values')
    settings = value['_COST_REQUEST_SETTINGS']
    if settings is not None and (type(settings) is not dict or len(settings) > 256 or any(not _identifier(key) or type(item) is not str or len(item) != 64 or any(char not in '0123456789abcdef' for char in item) for key, item in settings.items())):
        raise ContractError('Invalid request settings compatibility')
    for key in ('_COST_PACK_IDENTITY', '_COST_PROVIDER_IDENTITY'):
        fingerprint = value[key]
        if fingerprint is not None and (type(fingerprint) is not str or len(fingerprint) != 64 or any(char not in '0123456789abcdef' for char in fingerprint)):
            raise ContractError('Invalid pack/provider compatibility')
    return value


def validate_metadata(value):
    if (type(value) is not dict or set(value) not in (_METADATA, _METADATA | {'cache_policy'}) or type(value['version']) is not int or value['version'] != 1
            or (value.get('cache_policy') is not None and (type(value['cache_policy']) is not str or value['cache_policy'] not in ('off', 'medium', 'high')))
            or any(not _identifier(value[key], nullable=key in ('actual_model', 'serving_revision')) for key in
                   ('requested_model', 'catalog_model', 'actual_model', 'serving_revision'))
            or not _identifier(value['service_tier'], nullable=True) or value['billing_mode'] not in ('subscription', 'api_key', 'unknown') or type(value['compatibility']) is not dict
            or set(value['compatibility']) != set(_COMPAT)):
        raise ContractError('Invalid call accounting metadata')
    validate_compatibility(value['compatibility'])
    if value['rate_card'] is not None:
        validate_rate_card(value['rate_card'])
    # Bound shape and forbid custom objects/NaN; no payload/callback extension keys.
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 2097152:
            raise ValueError()
    except (ValueError, TypeError):
        raise ContractError('Invalid call accounting JSON') from None
    return value


def validate_evidence(value):
    if (type(value) is not dict or set(value) != {'call_id', 'selection', 'actual_model', 'usage', 'quote', 'started_at', 'finished_at', 'attempt', 'turn_id', 'turn_kind', 'outcome', 'service_tier', 'serving_revision'}
            or not _uuid(value['call_id']) or not _identifier(value['selection'])
            or any(not _identifier(value[key], nullable=True) for key in ('actual_model', 'service_tier', 'serving_revision'))):
        raise ContractError('Invalid inference evidence')
    if (type(value['attempt']) is not int or not 1 <= value['attempt'] <= 1000
            or value['outcome'] not in ('unknown', 'completed', 'length', 'refused', 'failed')
            or not _uuid(value['turn_id']) or value['turn_kind'] not in ('initial', 'clarification', 'approval')):
        raise ContractError('Invalid inference attempt')
    for key in ('started_at', 'finished_at'):
        if value[key] is not None:
            try:
                if datetime.fromisoformat(value[key]).tzinfo is None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ContractError('Invalid inference time') from None
    if value['started_at'] is not None and value['finished_at'] is not None and datetime.fromisoformat(value['finished_at']) < datetime.fromisoformat(value['started_at']):
        raise ContractError('Reversed inference times')
    if value['usage'] is not None:
        result_from_wire({'text': '', 'outcome': 'completed', 'usage': value['usage']})
    if value['quote'] is not None:
        from api_agent.providers.costs import validate_quote
        validate_quote(value['quote'])
    return value


def build_accounting(identity, tests, config, usage, outcomes, *, estimator=None, summary=None, context=None):
    summary = usage.report() if summary is None else summary
    quota = {key: deepcopy(summary.get(key)) for key in ('before', 'after', 'pct_point_delta',
             'quota_unavailable_reason', 'payment_policy', 'payment_month')}
    quota['contaminated'] = summary.get('contaminated', False)
    records = []
    calls = list(usage.inference_calls.values())
    selected_ids = {test.id for test in tests}
    calls_complete = not (any(call['test_id'] not in selected_ids for call in calls) or any(row['session_id'] not in selected_ids for row in summary.get('usage_rows', [])))
    for test in tests:
        own = [deepcopy(call) for call in calls if call['test_id'] == test.id]
        statuses = [outcome.status for outcome in outcomes if outcome.identity.id == test.id]
        costs = summary.get('test_costs', {}).get(test.id, {})
        prediction = None if estimator is None else deepcopy(getattr(estimator, 'advance_predictions', estimator.predictions).get(test.id))
        records.append(dict(test_id=test.id, node_id=test.node_id, source_fingerprint=source_fingerprint(test, context=context),
            compatibility=compatibility(config), outcomes=statuses, attempts=len(statuses), calls=own,
            original_estimate=costs.get('estimated_cost_usd'), list_cost_usd=costs.get('list_cost_usd'),
            raw_quota_cost_usd=costs.get('estimated_cost_usd') if costs.get('basis') == 'quota_payment' else None,
            prediction=prediction, stabilized_estimate=None, calls_complete=calls_complete,
            observed_call_count=max(len(own), sum(row['llm_calls'] for row in summary.get('usage_rows', []) if row['session_id'] == test.id))))
    artifact = dict(version=1, estimator_version=3, run_id=identity.run_id, rate_card=deepcopy(config.get('_COST_RATE_CARD')),
                    quota=quota, tests=records)
    validate_accounting(artifact)
    return artifact


def validate_accounting(value):
    if (type(value) is not dict or set(value) != {'version', 'estimator_version', 'run_id', 'rate_card', 'quota', 'tests'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['estimator_version']) is not int or value['estimator_version'] not in (1, 2, 3) or not _uuid(value['run_id'])
            or type(value['quota']) is not dict or set(value['quota']) not in ({'before', 'after', 'pct_point_delta',
                'quota_unavailable_reason', 'payment_policy', 'payment_month'}, {'before', 'after', 'pct_point_delta',
                'quota_unavailable_reason', 'payment_policy', 'payment_month', 'contaminated'}) or type(value['tests']) is not list):
        raise ContractError('Invalid suite accounting')
    if value['rate_card'] is not None:
        validate_rate_card(value['rate_card'])
    quota = value['quota']
    if type(quota.get('contaminated', False)) is not bool:
        raise ContractError('Invalid quota contamination')
    from api_agent.providers.costs import valid_amount
    if quota['pct_point_delta'] is not None and (not valid_amount(quota['pct_point_delta']) or quota['pct_point_delta'] > 100):
        raise ContractError('Invalid quota delta')
    for snapshot in (quota['before'], quota['after']):
        if snapshot is not None:
            if type(snapshot) is not dict or set(snapshot) not in ({'pct_used', 'window_id', 'reset_at', 'captured_at'}, {'pct_used', 'window_id', 'reset_at', 'captured_at', 'pct_step'}):
                raise ContractError('Invalid quota snapshot')
            if snapshot['pct_used'] is not None and (not valid_amount(snapshot['pct_used']) or snapshot['pct_used'] > 100):
                raise ContractError('Invalid quota percentage')
            if not _timestamp(snapshot['captured_at']) or not _timestamp(snapshot['reset_at'], nullable=True) or not _identifier(snapshot['window_id'], nullable=True):
                raise ContractError('Invalid quota identity/time')
            step = snapshot.get('pct_step')
            if step is not None and (not valid_amount(step) or not 0 < step <= 100):
                raise ContractError('Invalid quota precision')
    if quota['payment_policy'] is not None:
        from api_agent.providers.costs import validate_quote
        validate_quote({'list_cost_usd': None, 'payment': quota['payment_policy']})
    seen = set()
    call_ids = set()
    keys = {'test_id', 'node_id', 'source_fingerprint', 'compatibility', 'outcomes', 'attempts', 'calls',
            'original_estimate', 'list_cost_usd', 'raw_quota_cost_usd', 'prediction', 'stabilized_estimate', 'calls_complete', 'observed_call_count'}
    for row in value['tests']:
        if (type(row) is not dict or set(row) != keys or not _test_identifier(row['test_id'])
                or not _test_identifier(row['node_id']) or row['test_id'] in seen or type(row['calls']) is not list
                or type(row['calls_complete']) is not bool or type(row['observed_call_count']) is not int or row['observed_call_count'] < len(row['calls'])
                or type(row['outcomes']) is not list or type(row['attempts']) is not int or row['attempts'] != len(row['outcomes'])
                or type(row['compatibility']) is not dict or set(row['compatibility']) != set(_COMPAT)):
            raise ContractError('Invalid test accounting')
        validate_compatibility(row['compatibility'])
        from api_agent.providers.costs import valid_amount
        if (row['source_fingerprint'] is not None and (type(row['source_fingerprint']) is not str or len(row['source_fingerprint']) != 64 or any(char not in '0123456789abcdef' for char in row['source_fingerprint']))) or any(status not in ('passed', 'failed', 'skipped', 'error', 'cancelled') for status in row['outcomes']) or any(row[key] is not None and not valid_amount(row[key]) for key in ('original_estimate', 'list_cost_usd', 'raw_quota_cost_usd')):
            raise ContractError('Invalid test cost facts')
        seen.add(row['test_id'])
        for call in row['calls']:
            if type(call) is not dict or call.get('test_id') != row['test_id']:
                raise ContractError('Invalid test call identity')
            if call.get('call_id') in call_ids:
                raise ContractError('Duplicate accounting call')
            call_ids.add(call.get('call_id'))
            validate_evidence({key: val for key, val in call.items() if key != 'test_id'})
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        raise ContractError('Invalid suite accounting JSON') from None
    return value
