"""Early review assertions keep their diagnostics while owned helpers still drain."""
from types import SimpleNamespace
import threading

import pytest

from api_agent.testing import setup_backend
import test_suite_cleanup_lease as review_cases
from test_suite_cleanup_lease import (
    assert_competing_suite_allowed,
    test_cancellation_resistant_inference_keeps_kernel_lease_until_disposal_ack as run_cleanup_review,
)


@pytest.fixture(params=['startup', 'entered'])
def early_failure(request, monkeypatch):
    original_backend, original_event = setup_backend.SetupBackend, threading.Event
    original_executor = review_cases.ThreadPoolExecutor
    expected = (ValueError('Controlled startup diagnostic') if request.param == 'startup'
                else AssertionError('Controlled entry diagnostic'))
    created, events, executors = [], [], []
    def backend(*args, **kwargs):
        instance = original_backend(*args, **kwargs)
        created.append(instance)
        if request.param == 'startup':
            def failed_start(): raise expected
            instance.start = failed_start
        return instance
    def event():
        instance = original_event()
        if not events and request.param == 'entered':
            def failed_entry(timeout=None): raise expected
            instance.wait = failed_entry
        events.append(instance)
        return instance
    def executor(*args, **kwargs):
        instance = original_executor(*args, **kwargs)
        executors.append(instance)
        return instance
    monkeypatch.setattr(review_cases, 'ThreadPoolExecutor', executor)
    monkeypatch.setattr(setup_backend, 'SetupBackend', backend)
    monkeypatch.setattr(threading, 'Event', event)
    return SimpleNamespace(created=created, expected=expected, executors=executors)


def test_cleanup_does_not_mask_the_initial_review_failure(tmp_path, monkeypatch, early_failure):
    with pytest.raises(type(early_failure.expected)) as caught:
        run_cleanup_review(tmp_path, monkeypatch, .04)
    assert caught.value is early_failure.expected
    assert str(caught.value) == str(early_failure.expected)
    assert len(early_failure.executors) == 1
    assert all(not thread.is_alive() for thread in early_failure.executors[0]._threads)
    assert len(early_failure.created) == 1
    backend = early_failure.created[0]
    assert backend.closed and (backend.thread is None or not backend.thread.is_alive())
    assert_competing_suite_allowed()
