"""Synthetic attribution evidence never requires a live quota endpoint."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from api_agent.llm_providers.gpt.quota_client import _snapshot
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.accounting import build_accounting, validate_accounting
from api_agent.telemetry.usage import QuotaSnapshot, SuiteUsage
from api_agent.testing.quota_events import capture, publish


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=2)
RUN_ID = '00000000-0000-4000-8000-000000000001'
OBSERVATION = '00000000-0000-4000-8000-000000000002'
ALLOCATION = 'a' * 64


def artifact_with(snapshot):
    usage = SuiteUsage(RUN_ID)
    usage.observe_snapshot('before', snapshot)
    usage.observe_snapshot('after', QuotaSnapshot(2, 'window', RESET, NOW + timedelta(seconds=1), 1,
                                                 ALLOCATION, '00000000-0000-4000-8000-000000000003'))
    return build_accounting(SimpleNamespace(run_id=RUN_ID), [], {}, usage, [])


def test_identity_survives_worker_event_report_and_accounting(tmp_path, monkeypatch):
    snapshot = QuotaSnapshot(1, 'window', RESET, NOW, 1, ALLOCATION, OBSERVATION)
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_identity (call)')

    publish('before', snapshot)
    usage = SuiteUsage(RUN_ID)
    event = json.loads(path.read_text())
    capture(SimpleNamespace(text=json.dumps(event['data'])), usage.observe_snapshot)
    accounting = artifact_with(usage.before)

    assert accounting['version'] == 2 and accounting['estimator_version'] == 5
    assert accounting['quota']['before']['allocation_id'] == ALLOCATION
    assert accounting['quota']['before']['observation_id'] == OBSERVATION
    assert validate_accounting(json.loads(json.dumps(accounting))) == accounting
    assert len(SuiteUsage(RUN_ID).rows()) == 0


@pytest.mark.parametrize('field,value', [('allocation_id', 'A' * 64), ('allocation_id', 'a' * 63),
                                          ('allocation_id', 123), ('observation_id', 'NOT-A-UUID'),
                                          ('observation_id', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'.upper())])
def test_snapshot_rejects_malformed_identity(field, value):
    fields = dict(allocation_id=ALLOCATION, observation_id=OBSERVATION)
    fields[field] = value

    with pytest.raises(ValueError):
        QuotaSnapshot(1, 'window', RESET, NOW, **fields)


@pytest.mark.parametrize('field,value', [('allocation_id', 'A' * 64), ('allocation_id', 'a' * 63),
                                          ('observation_id', 'bad-id')])
def test_accounting_rejects_malformed_identity(field, value):
    artifact = artifact_with(QuotaSnapshot(1, 'window', RESET, NOW, 1, ALLOCATION, OBSERVATION))
    broken = deepcopy(artifact)
    broken['quota']['before'][field] = value

    with pytest.raises(ContractError):
        validate_accounting(broken)


@pytest.mark.parametrize('field,value,error', [
    ('pct_used', 101, 'percentage'),
    ('captured_at', '2026-10-12T00:00:00', 'identity/time'),
    ('pct_step', 0, 'precision'),
])
def test_v2_accounting_rejects_invalid_snapshot_facts(field, value, error):
    artifact = artifact_with(QuotaSnapshot(1, 'window', RESET, NOW, 1, ALLOCATION, OBSERVATION))
    broken = deepcopy(artifact)
    broken['quota']['before'][field] = value

    with pytest.raises(ContractError, match=error):
        validate_accounting(broken)


def test_legacy_artifact_remains_readable_with_unknown_identity():
    artifact = artifact_with(QuotaSnapshot(1, 'window', RESET, NOW))
    artifact['version'] = 1
    artifact['estimator_version'] = 4
    for phase in ('before', 'after'):
        del artifact['quota'][phase]['allocation_id']
        del artifact['quota'][phase]['observation_id']

    assert validate_accounting(artifact) is artifact


def test_gpt_meter_subject_and_observation_boundaries():
    data = {'rateLimits': {'limitId': 'codex', 'primary': {
        'usedPercent': 28, 'windowDurationMins': 10080, 'resetsAt': int(RESET.timestamp())}}}
    first = _snapshot(data, 'opaque-subject-one', 'week', NOW)
    reread = _snapshot(data, 'opaque-subject-one', 'week', NOW)
    other_subject = _snapshot(data, 'opaque-subject-two', 'week', NOW)
    daily = deepcopy(data)
    daily['rateLimits']['primary']['windowDurationMins'] = 1440
    other_meter = _snapshot(daily, 'opaque-subject-one', 'day', NOW)

    assert first.allocation_id == reread.allocation_id
    assert len({first.allocation_id, other_subject.allocation_id, other_meter.allocation_id}) == 3
    assert first.observation_id != reread.observation_id
    assert str(UUID(first.observation_id)) == first.observation_id
    serialized = json.dumps([first.allocation_id, first.observation_id, first.window_id])
    assert 'opaque-subject' not in serialized


def test_calendar_month_length_does_not_change_meter_identity():
    january = datetime(2026, 1, 1, tzinfo=timezone.utc)
    april = datetime(2026, 4, 1, tzinfo=timezone.utc)
    def observed_at(date, days):
        data = {'rateLimits': {'limitId': 'codex', 'primary': {
            'usedPercent': 28, 'windowDurationMins': days * 1440,
            'resetsAt': int((date + timedelta(days=days)).timestamp())}}}
        return _snapshot(data, 'opaque-subject-one', 'month', date)

    january_snapshot = observed_at(january, 31)
    april_snapshot = observed_at(april, 30)

    assert january_snapshot.allocation_id == april_snapshot.allocation_id
    assert january_snapshot.window_id != april_snapshot.window_id


@pytest.mark.parametrize('before_allocation,after_allocation', [(ALLOCATION, 'b' * 64),
                                                                  ('b' * 64, ALLOCATION)])
def test_known_allocation_change_cannot_produce_quota_cost(before_allocation, after_allocation):
    usage = SuiteUsage(RUN_ID)
    usage.observe_snapshot('before', QuotaSnapshot(1, 'window', RESET, NOW, 1, before_allocation, OBSERVATION))
    usage.observe_snapshot('after', QuotaSnapshot(2, 'window', RESET, NOW + timedelta(seconds=1), 1,
                                                  after_allocation, '00000000-0000-4000-8000-000000000003'))
    usage.add_cost('call', 'test', {'list_cost_usd': 1.0,
                                  'payment': {'usd': 100.0, 'quota_seconds': 604800, 'period': 'quota'}})

    report = usage.report(now=NOW)
    artifact = build_accounting(SimpleNamespace(run_id=RUN_ID), [], {}, usage, [], summary=report)

    assert report['quota_unavailable_reason'] == 'changed_allocation'
    assert report['pct_point_delta'] is None
    assert report['raw_quota_cost_usd'] is None
    assert report['cost_basis'] == 'list_price'
    assert artifact['quota']['pct_point_delta'] is None
    assert artifact['quota']['before']['allocation_id'] == before_allocation
    assert artifact['quota']['after']['allocation_id'] == after_allocation
    assert validate_accounting(artifact) is artifact


def test_legacy_worker_event_has_explicit_unknown_identity():
    row = {'pct_used': 1, 'window_id': 'window', 'reset_at': RESET.isoformat(), 'captured_at': NOW.isoformat()}
    usage = SuiteUsage(RUN_ID)

    capture(SimpleNamespace(text=json.dumps({'phase': 'before', 'snapshot': row})), usage.observe_snapshot)

    assert usage.report()['before']['allocation_id'] is None
    assert usage.report()['before']['observation_id'] is None


@pytest.mark.parametrize('field,value', [('allocation_id', 'Z' * 64),
                                          ('observation_id', 'private-unvalidated-value')])
def test_worker_event_rejects_malformed_identity(field, value):
    row = {'pct_used': 1, 'window_id': 'window', 'reset_at': RESET.isoformat(),
           'captured_at': NOW.isoformat(), field: value}
    usage = SuiteUsage(RUN_ID)

    with pytest.raises(ValueError):
        capture(SimpleNamespace(text=json.dumps({'phase': 'before', 'snapshot': row})), usage.observe_snapshot)

    assert usage.before is None
