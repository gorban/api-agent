"""Owned exchanges preserve bytes and finish disposal on exceptional IO."""
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest

from api_agent.runtime import processes

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX retained child ownership')


@pytest.fixture
def child_factory():
    children = []
    def create(script, **options):
        child = subprocess.Popen([sys.executable, '-c', script], start_new_session=True, **options)
        children.append(child)
        return child
    yield create
    for child in children:
        processes.finalize(child)
        for name in ('stdin', 'stdout', 'stderr'):
            pipe = getattr(child, name)
            if pipe is not None:
                pipe.close()


def assert_disposed(child):
    assert child.returncode is not None
    assert processes.disposal_complete(child)
    assert all(pipe is None or pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


def test_partial_writes_deliver_every_input_byte(child_factory, monkeypatch):
    child = child_factory('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())',
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    descriptor = child.stdin.fileno()
    original = os.write
    written = []
    def partial(fd, data):
        if fd == descriptor:
            count = original(fd, data[:13])
            written.append(count)
            return count
        return original(fd, data)
    monkeypatch.setattr(processes.os, 'write', partial)
    payload = bytes(range(256)) * 20

    assert processes.communicate_owned(child, payload, timeout=5) == (payload, None)

    assert len(written) > 1 and sum(written) == len(payload)
    assert_disposed(child)


def quiet_selector(monkeypatch):
    original = processes.selectors.DefaultSelector
    class Quiet:
        def __init__(self): self.inner = original()
        def __getattr__(self, name): return getattr(self.inner, name)
        def select(self, timeout):
            self.inner.select(timeout)
            return []
    monkeypatch.setattr(processes.selectors, 'DefaultSelector', Quiet)


def test_silent_selector_closes_unused_input_and_drains_both_streams(child_factory, monkeypatch):
    child = child_factory('import os; os.write(1,b"stdout"); os.write(2,b"stderr")',
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    quiet_selector(monkeypatch)

    assert processes.communicate_owned(child, b'unconsumed', timeout=5) == (b'stdout', b'stderr')

    assert child.returncode == 0
    assert_disposed(child)


def test_silent_selector_cannot_claim_complete_output_after_would_block(child_factory, monkeypatch):
    child = child_factory('print("buffered", flush=True)', stdout=subprocess.PIPE)
    descriptor = child.stdout.fileno()
    original = os.read
    quiet_selector(monkeypatch)
    def would_block(fd, count):
        if fd == descriptor and processes.disposal_complete(child):
            raise BlockingIOError('controlled incomplete output')
        return original(fd, count)
    monkeypatch.setattr(processes, 'os', SimpleNamespace(**(vars(processes.os) | {'read': would_block})))

    with pytest.raises(RuntimeError, match='output did not close'):
        processes.communicate_owned(child, timeout=5)

    assert_disposed(child)


def test_read_error_disposes_retained_child_and_closes_pipes(child_factory, monkeypatch):
    child = child_factory('import time; print("ready",flush=True); time.sleep(30)',
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    descriptor = child.stdout.fileno()
    original = os.read
    def failed_read(fd, count):
        if fd == descriptor:
            raise OSError('controlled pipe failure')
        return original(fd, count)
    monkeypatch.setattr(processes, 'os', SimpleNamespace(**(vars(processes.os) | {'read': failed_read})))

    with pytest.raises(OSError, match='controlled pipe failure'):
        processes.communicate_owned(child, timeout=5)

    assert_disposed(child)


def test_exchange_deadline_disposes_child_with_open_pipes(child_factory):
    child = child_factory('import time; time.sleep(30)', stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    with pytest.raises(subprocess.TimeoutExpired):
        processes.communicate_owned(child, timeout=.03)

    assert_disposed(child)


def test_cancellation_check_during_exchange_disposes_child(child_factory):
    child = child_factory('import time; time.sleep(30)', stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    checks = []
    def cancelled():
        checks.append(True)
        if len(checks) == 2:
            raise InterruptedError('controlled cancellation')

    with pytest.raises(InterruptedError, match='controlled cancellation'):
        processes.communicate_owned(child, timeout=5, check=cancelled)

    assert len(checks) == 2
    assert_disposed(child)
