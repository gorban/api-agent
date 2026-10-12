"""Early review assertions keep their diagnostics while owned helpers still drain."""
from types import SimpleNamespace
import threading

import pytest

from api_agent.testing import setup_backend
from test_suite_cleanup_lease import (
    assert_competing_suite_allowed,
    test_cancellation_resistant_inference_keeps_kernel_lease_until_disposal_ack as run_cleanup_review,
)


@pytest.fixture(params=['startup', 'entered'])
def early_failure(request, monkeypatch):
    original_backend, original_event = setup_backend.SetupBackend, threading.Event
    created, events = [], []
    def backend(*args, **kwargs):
        instance = original_backend(*args, **kwargs)
        created.append(instance)
        if request.param == 'startup':
            def failed_start(): raise ValueError('Controlled startup diagnostic')
            instance.start = failed_start
        return instance
    def event():
        instance = original_event()
        if not events and request.param == 'entered':
            instance.wait = lambda timeout=None: False
        events.append(instance)
        return instance
    monkeypatch.setattr(setup_backend, 'SetupBackend', backend)
    monkeypatch.setattr(threading, 'Event', event)
    return SimpleNamespace(created=created, error=ValueError if request.param == 'startup' else AssertionError)


def test_cleanup_does_not_mask_the_initial_review_failure(tmp_path, monkeypatch, early_failure):
    with pytest.raises(early_failure.error) as caught:
        run_cleanup_review(tmp_path, monkeypatch, .04)
    assert not isinstance(caught.value, UnboundLocalError)
    assert len(early_failure.created) == 1
    backend = early_failure.created[0]
    assert backend.closed and (backend.thread is None or not backend.thread.is_alive())
    assert_competing_suite_allowed()
