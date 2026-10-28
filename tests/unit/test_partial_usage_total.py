"""Known totals remain lower bounds when physical usage is incomplete."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.providers.generation_attempts import TransportAttemptLedger
from api_agent.providers.llm import Usage
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.inference_usage import capture


def capture_ledger(tracker, usage, *, retry=False, complete=True):
    ledger = TransportAttemptLedger(2)
    if retry:
        token = ledger.begin(1)
        ledger.finish(token, error=SimpleNamespace(execution='possibly_executed', http_status=429),
                      retry_delay_seconds=0)
    token = ledger.begin(2 if retry else 1)
    ledger.finish(token, result=SimpleNamespace(usage=usage, billing=None))
    snapshot = ledger.snapshot()
    snapshot['complete'] = complete
    row = {'call_id': str(uuid4()), 'selection': 'synthetic', 'usage': None,
           'transport_attempts': snapshot}
    event = SimpleNamespace(text=json.dumps(row), identity=SimpleNamespace(id='test'))
    capture(event, tracker)
    return event


def test_recovered_retry_and_exact_calls_preserve_all_known_tokens_once():
    tracker = SuiteUsage('run')
    capture_ledger(tracker, Usage(13000, 1596))
    event = capture_ledger(tracker, Usage(4381, 543), retry=True)
    capture(event, tracker)

    report = tracker.report()
    row = report['usage_rows'][0]
    observed = report['usage_observations'][0]
    assert row['llm_calls'] == 2
    assert row['total_tokens'] is None
    assert observed['input_tokens'] == 17381
    assert observed['output_tokens'] == 2139
    assert observed['total_tokens'] == 19520
    assert 'total_tokens' in observed['unknown_fields']


@pytest.mark.parametrize('usage, expected', [
    (Usage(input_tokens=7), 7),
    (Usage(output_tokens=3), 3),
    (Usage(input_tokens=0), 0),
    (Usage(), None),
    (Usage(7, 3, cached_input_tokens=4, reasoning_tokens=2, cache_write_tokens=1), 10),
])
def test_partial_categories_and_subsets_form_only_known_total(usage, expected):
    tracker = SuiteUsage('run')
    capture_ledger(tracker, usage, retry=True)

    report = tracker.report()
    assert report['usage_rows'][0]['total_tokens'] is None
    observed = report['usage_observations'][0]
    assert observed['total_tokens'] == expected
    assert 'total_tokens' in observed['unknown_fields']


def test_incomplete_ledger_keeps_total_unknown_with_known_amount():
    tracker = SuiteUsage('run')
    capture_ledger(tracker, Usage(7, 3), complete=False)

    report = tracker.report()
    assert report['usage_rows'][0]['total_tokens'] is None
    assert report['usage_observations'][0]['total_tokens'] == 10
    assert 'total_tokens' in report['usage_observations'][0]['unknown_fields']


def test_legacy_partial_usage_retains_known_total():
    tracker = SuiteUsage('run')
    tracker.add_usage(provider='p', model='m', session_id='s', input_tokens=7)

    report = tracker.report()
    assert report['usage_rows'][0]['total_tokens'] is None
    assert report['usage_observations'][0]['total_tokens'] == 7


def test_explicit_known_total_is_not_recounted():
    tracker = SuiteUsage('run')
    tracker.add_usage(provider='p', model='m', session_id='s', input_tokens=7,
                      known_subtotals={'output_tokens': 3, 'total_tokens': 10})

    assert tracker.report()['usage_observations'][0]['total_tokens'] == 10


def test_different_attempts_contribute_independent_known_categories():
    tracker = SuiteUsage('run')
    ledger = TransportAttemptLedger(2)
    first = ledger.begin(1)
    ledger.finish(first, error=SimpleNamespace(execution='possibly_executed', http_status=429,
                                              usage=Usage(input_tokens=7)), retry_delay_seconds=0)
    second = ledger.begin(2)
    ledger.finish(second, result=SimpleNamespace(usage=Usage(output_tokens=3), billing=None))
    row = {'call_id': str(uuid4()), 'selection': 'synthetic', 'usage': None,
           'transport_attempts': ledger.snapshot()}
    event = SimpleNamespace(text=json.dumps(row), identity=SimpleNamespace(id='test'))

    capture(event, tracker)

    observed = tracker.report()['usage_observations'][0]
    assert observed['input_tokens'] == 7
    assert observed['output_tokens'] == 3
    assert observed['total_tokens'] == 10
    assert 'total_tokens' in observed['unknown_fields']


def test_subsets_on_separate_attempt_add_to_known_total_once():
    tracker = SuiteUsage('run')
    ledger = TransportAttemptLedger(2)
    first = ledger.begin(1)
    ledger.finish(first, error=SimpleNamespace(execution='possibly_executed', http_status=429,
                                              usage=Usage(input_tokens=10, cached_input_tokens=4)),
                  retry_delay_seconds=0)
    second = ledger.begin(2)
    ledger.finish(second, result=SimpleNamespace(
        usage=Usage(cached_input_tokens=3, cache_write_tokens=2, reasoning_tokens=6), billing=None))
    row = {'call_id': str(uuid4()), 'selection': 'synthetic', 'usage': None,
           'transport_attempts': ledger.snapshot()}
    event = SimpleNamespace(text=json.dumps(row), identity=SimpleNamespace(id='test'))

    capture(event, tracker)
    capture(event, tracker)

    report = tracker.report()
    observed = report['usage_observations'][0]
    assert observed['llm_calls'] == 1
    assert observed['input_tokens'] == 10
    assert observed['total_tokens'] == 21
    assert report['usage_rows'][0]['total_tokens'] is None
    assert 'total_tokens' in observed['unknown_fields']


@pytest.mark.parametrize('values, expected', [
    ({'cache_read_tokens': 3, 'cache_creation_tokens': 2, 'reasoning_tokens': 6}, 11),
    ({'cache_read_tokens': 0}, 0),
    ({'input_tokens': 10, 'cache_read_tokens': 4, 'reasoning_tokens': 6}, 16),
    ({'output_tokens': 8, 'reasoning_tokens': 6}, 8),
])
def test_legacy_subsets_provide_disjoint_known_total(values, expected):
    tracker = SuiteUsage('run')
    tracker.add_usage(provider='p', model='m', session_id='s', **values)

    report = tracker.report()
    assert report['usage_rows'][0]['total_tokens'] is None
    assert report['usage_observations'][0]['total_tokens'] == expected
    assert 'total_tokens' in report['usage_observations'][0]['unknown_fields']


@pytest.mark.parametrize('usage, expected', [
    (Usage(cached_input_tokens=0), 0),
    (Usage(cache_write_tokens=2, reasoning_tokens=3), 5),
    (Usage(), None),
])
def test_incomplete_ledger_preserves_subset_evidence_without_exact_total(usage, expected):
    tracker = SuiteUsage('run')
    capture_ledger(tracker, usage, complete=False)

    report = tracker.report()
    assert report['usage_rows'][0]['total_tokens'] is None
    assert report['usage_observations'][0]['total_tokens'] == expected
    assert 'total_tokens' in report['usage_observations'][0]['unknown_fields']
