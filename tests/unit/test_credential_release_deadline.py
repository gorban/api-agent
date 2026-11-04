"""A real helper exit cannot release a credential after its fixed budget."""
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from api_agent.auth import credentials, owned_process


@pytest.mark.parametrize('phase', ['exit', 'drain'])
@pytest.mark.parametrize('boundary', ['deadline', 'cancellation'])
def test_terminal_exit_rechecks_budget_before_releasing_private_output(monkeypatch, boundary, phase):
    real_waitid = os.waitid
    late = threading.Event()
    cancelled = threading.Event()
    children = []
    factory = owned_process.OwnedProcess
    def start(*args, **kwargs):
        child = factory(*args, **kwargs)
        children.append(child)
        return child
    def terminal_wait(*args):
        result = real_waitid(*args)
        if result is not None and phase == 'exit':
            expire()
        return result
    def expire():
        late.set()
        if boundary == 'cancellation': cancelled.set()
    class Reader(threading.Thread):
        def join(self, timeout=None):
            super().join(timeout)
            if phase == 'drain': expire()
    def clock():
        return time.monotonic() + (60 if late.is_set() and boundary == 'deadline' else 0)
    monkeypatch.setattr(owned_process, 'OwnedProcess', start)
    monkeypatch.setattr(credentials, 'threading', SimpleNamespace(**(vars(threading) | {'Thread': Reader})))
    monkeypatch.setattr(credentials, 'os', SimpleNamespace(**(vars(os) | {'waitid': terminal_wait})))
    monkeypatch.setattr(credentials, 'time', SimpleNamespace(**(vars(time) | {'monotonic': clock})))

    error = credentials.CredentialRecoveryError if boundary == 'cancellation' else credentials.CredentialError
    with pytest.raises(error, match='cancelled' if boundary == 'cancellation' else 'timed out') as failure:
        credentials._run([sys.executable, '-c', 'print("private-result")'], 10, cancel_event=cancelled)

    assert late.is_set()
    assert 'private-result' not in str(failure.value)
    assert len(children) == 1
    assert children[0].returncode == 0
