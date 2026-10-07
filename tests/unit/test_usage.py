import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from api_agent.telemetry.usage import CSV_COLUMNS, QuotaSnapshot, SuiteUsage


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def snapshot(pct, *, window='w1', reset=None, at=NOW):
    return QuotaSnapshot(pct, window, reset or NOW + timedelta(hours=2), at)


def test_counts_and_missing_metrics_remain_unknown():
    usage = SuiteUsage('run-1')
    usage.add_usage(provider='gateway', model='m', session_id='s', input_tokens=2,
                    output_tokens=3, cache_read_tokens=0, reasoning_tokens=1)
    usage.add_usage(provider='gateway', model='m', session_id='s', input_tokens=None,
                    output_tokens=4, total_tokens=4, cache_read_tokens=0)
    usage.add_usage(provider='gateway', model='m', session_id='s', input_tokens=1,
                    output_tokens=0, total_tokens=1, cache_read_tokens=0)
    row, = usage.rows()
    assert row['llm_calls'] == 3
    assert row['input_tokens'] is None
    assert row['output_tokens'] == 7
    assert row['total_tokens'] == 10
    assert row['cache_read_tokens'] == 0
    assert row['cache_creation_tokens'] is None
    assert row['reasoning_tokens'] is None
    assert tuple(row) == CSV_COLUMNS
    assert usage.record_prompt(turn_id='t1', prompt_id='p1', kind='initial')
    assert not usage.record_prompt(turn_id='t1', prompt_id='p1', kind='initial')
    assert usage.record_prompt(turn_id='t1', prompt_id='retry', kind='initial')
    assert usage.record_prompt(turn_id='t2', prompt_id='answer', kind='clarification')
    assert usage.record_prompt(turn_id='t3', prompt_id='yes', kind='approval')
    assert usage.report(now=NOW)['prompt_counts'] == {'approval': 1, 'clarification': 1, 'initial': 2}


def test_quota_delta_cost_reset_and_zero_prompts():
    usage = SuiteUsage('run-2')
    usage.capture_before(lambda: snapshot(21.5))
    usage.capture_after(lambda: snapshot(23, at=NOW + timedelta(minutes=1)))
    report = usage.report(now=NOW)
    assert report['pct_point_delta'] == 1.5
    assert report['estimated_cost_usd'] is None
    assert report['cost_per_prompt_usd'] is None
    assert report['pct_used_per_prompt'] is None
    assert report['prompt_count'] == 0
    assert report['reset_countdown_hms'] == '02:00:00'
    assert report['concurrent_consumption_possible'] is True
    assert json.loads(json.dumps(report))['before']['captured_at'] == NOW.isoformat()
    usage.record_prompt(turn_id='one', prompt_id='first', kind='initial')
    assert usage.report(now=NOW)['cost_per_prompt_usd'] is None
    assert usage.report(now=NOW)['pct_used_per_prompt'] == '1.500'


@pytest.mark.parametrize('before,after,reason', [
    (None, snapshot(2), 'missing_snapshot'),
    (snapshot(5, window=None), snapshot(6), 'missing_window_identifier'),
    (snapshot(5), snapshot(6, window='w2'), 'changed_window'),
    (snapshot(5), snapshot(4), 'negative_delta'),
    (snapshot(5, at=NOW + timedelta(minutes=2)), snapshot(6), 'reversed_capture_time'),
    (snapshot(5), snapshot(6, at=NOW + timedelta(hours=3)), 'reset_boundary_crossed'),
])
def test_unavailable_boundaries(before, after, reason):
    usage = SuiteUsage('run')
    usage.before, usage.after = before, after
    report = usage.report(now=NOW)
    assert report['quota_unavailable_reason'] == reason
    assert report['pct_point_delta'] is None
    assert report['estimated_cost_usd'] is None
    assert report['pct_used_per_prompt'] is None


def test_validation_and_capture_idempotence():
    with pytest.raises(ValueError):
        QuotaSnapshot(1, '', None, NOW)
    with pytest.raises(ValueError):
        QuotaSnapshot(1, 'w', None, NOW.replace(tzinfo=None))
    usage = SuiteUsage('run')
    with pytest.raises(ValueError):
        usage.add_usage(provider='p', model='m', session_id='s', input_tokens=2,
                        output_tokens=2, total_tokens=5)
    with pytest.raises(ValueError):
        usage.add_usage(provider='p', model='m', session_id='s', input_tokens=-1)
    with pytest.raises(ValueError):
        usage.record_prompt(turn_id='t', prompt_id='p', kind='other')
    usage.record_prompt(turn_id='t', prompt_id='p', kind='initial')
    with pytest.raises(ValueError):
        usage.record_prompt(turn_id='t', prompt_id='p', kind='approval')
    usage.capture_before(lambda: snapshot(1))
    with pytest.raises(ValueError):
        usage.capture_before(lambda: snapshot(2))


def test_csv_rotation_preserves_legacy(tmp_path):
    usage = SuiteUsage('run')
    usage.add_usage(provider='gateway', model='m', session_id='s', input_tokens=0,
                    output_tokens=0)
    external = tmp_path / 'shared.csv'
    external.write_text('old,header\nold,row\n')
    usage.write_csv(tmp_path / 'run.csv', external)
    assert (tmp_path / 'shared.csv.legacy').read_text() == 'old,header\nold,row\n'
    with external.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[0]['total_tokens'] == '0'
    assert rows[0]['cache_read_tokens'] == ''
    assert (tmp_path / 'run.csv').read_text().startswith(','.join(CSV_COLUMNS))


@pytest.mark.parametrize('pct', [float('nan'), float('inf'), -1, 101, '5', True, 10**500])
def test_invalid_percentage_is_explicitly_unavailable(pct):
    usage = SuiteUsage('run')
    usage.before = snapshot(pct)
    usage.after = snapshot(10)
    assert usage.report(now=NOW)['quota_unavailable_reason'] == 'invalid_percentage'


def test_input_errors_and_capture_after():
    with pytest.raises(ValueError):
        SuiteUsage('')
    usage = SuiteUsage('run')
    with pytest.raises(ValueError):
        usage.add_usage(provider='', model='m', session_id='s')
    with pytest.raises(ValueError):
        usage.add_usage(provider='p', model='m', session_id='s', llm_calls=0)
    with pytest.raises(ValueError):
        usage.add_usage(provider='p', model='m', session_id='s', input_tokens=True)
    with pytest.raises(ValueError):
        usage.record_prompt(turn_id='', prompt_id='p', kind='initial')
    with pytest.raises(TypeError):
        usage.capture_before(lambda: {})
    with pytest.raises(TypeError):
        usage.capture_after(lambda: {})
    usage.capture_after(lambda: snapshot(1))
    with pytest.raises(ValueError):
        usage.capture_after(lambda: snapshot(2))


def test_unchanged_window_zero_delta_and_missing_pct(tmp_path):
    usage = SuiteUsage('run')
    usage.before = snapshot(0)
    usage.after = snapshot(0)
    report = usage.report(now=NOW + timedelta(hours=3))
    assert report['pct_point_delta'] == 0
    assert report['estimated_cost_usd'] is None
    assert report['reset_countdown_hms'] == '00:00:00'
    usage.after = snapshot(None)
    assert usage.report(now=NOW)['quota_unavailable_reason'] == 'missing_snapshot'
    usage.after = QuotaSnapshot(0, 'w1', None, NOW)
    assert usage.report(now=NOW)['quota_unavailable_reason'] == 'missing_window_identifier'
    usage.before = None
    assert usage.report(now=NOW)['pct_used_before'] is None
    usage.after = None
    assert usage.report(now=NOW)['reset_countdown_hms'] is None
    usage.write_csv(tmp_path / 'run.csv')
    assert (tmp_path / 'run.csv').read_text().strip() == ','.join(CSV_COLUMNS)


def test_csv_append_and_second_rotation_name(tmp_path):
    usage = SuiteUsage('run')
    usage.add_usage(provider='p', model='m', session_id='s', input_tokens=1, output_tokens=1, total_tokens=2)
    external = tmp_path / 'shared.csv'
    usage.write_csv(tmp_path / 'run.csv', external)
    usage.write_csv(tmp_path / 'run.csv', external)
    with external.open(newline='') as stream:
        assert len(list(csv.DictReader(stream))) == 2
    (tmp_path / 'shared.csv.legacy').write_text('previous')
    external.write_text('old,header\n')
    usage.write_csv(tmp_path / 'run.csv', external)
    assert (tmp_path / 'shared.csv.legacy').read_text() == 'previous'
    assert (tmp_path / 'shared.csv.legacy1').read_text() == 'old,header\n'


def test_provider_cost_estimator_owns_currency_conversion():
    calls = []
    def estimate(**metrics):
        calls.append(metrics)
        return sum(row['total_tokens'] or 0 for row in metrics['usage_rows']) * 0.002
    usage = SuiteUsage('synthetic', cost_estimator=estimate)
    usage.add_usage(provider='synthetic', model='m', session_id='s', input_tokens=2, output_tokens=3)
    usage.record_prompt(turn_id='t', prompt_id='p', kind='initial')
    report = usage.report(now=NOW)
    assert report['estimated_cost_usd'] == 0.01
    assert report['cost_per_prompt_usd'] == 0.01
    assert calls[0]['before'] is None and calls[0]['after'] is None
    assert report['quota_unavailable_reason'] == 'missing_snapshot'


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), '1'])
def test_invalid_provider_cost_estimate_is_rejected(value):
    usage = SuiteUsage('synthetic', cost_estimator=lambda **metrics: value)
    with pytest.raises(ValueError, match='cost estimate'):
        usage.report(now=NOW)
