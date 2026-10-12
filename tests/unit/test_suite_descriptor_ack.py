"""Interrupted acknowledgement retries observe one disposal, never reused fds."""
from concurrent.futures import Future
import os
import threading

import pytest

from api_agent.testing import suite_lock as locks
from test_setup_backend import fake_host
from test_suite_cleanup_lease import assert_competing_suite_allowed, assert_competing_suite_blocked


@pytest.mark.parametrize('interrupt_after_close', [False, True])
def test_backend_release_retry_preserves_callback_and_single_close(fake_host, monkeypatch, tmp_path, interrupt_after_close):
    backend = fake_host.backend
    backend.start()
    entered, proceed = threading.Event(), threading.Event()
    real_dispose = locks._dispose_descriptor
    disposals = []
    def dispose(fd, acknowledgement):
        disposals.append(fd)
        if interrupt_after_close:
            os.closerange(fd, fd + 1)
        entered.set()
        assert proceed.wait(5)
        if interrupt_after_close:
            acknowledgement.set_result(None)
        else:
            real_dispose(fd, acknowledgement)
    monkeypatch.setattr(locks, '_dispose_descriptor', dispose)
    real_result = Future.result
    interrupted = False
    def result(future, *args, **kwargs):
        nonlocal interrupted
        if future is lease.closing and not interrupted:
            interrupted = True
            assert entered.wait(5)
            raise KeyboardInterrupt()
        return real_result(future, *args, **kwargs)
    monkeypatch.setattr(Future, 'result', result)
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        locks.retain_suite_cleanup(backend)
    saved_fd = lease.fd
    replacement = None
    try:
        with pytest.raises(KeyboardInterrupt):
            backend.close()
        assert backend.closed and len(backend._cleanup_callbacks) == 1
        assert lease.fd == saved_fd and disposals == [saved_fd]
        if interrupt_after_close:
            replacement = os.open(tmp_path / 'replacement', os.O_CREAT | os.O_RDWR, 0o600)
            os.dup2(replacement, saved_fd)
            if replacement != saved_fd:
                os.close(replacement)
            replacement = saved_fd
        else:
            os.fstat(saved_fd)
            assert_competing_suite_blocked()
        proceed.set()
        backend.close()
        assert lease.fd is None and not backend._cleanup_callbacks
        assert not backend.pending and not backend.thread.is_alive()
        backend.close()
        assert disposals == [saved_fd]
        if replacement is not None:
            assert os.fstat(replacement).st_nlink == 1
        assert_competing_suite_allowed()
    finally:
        proceed.set()
        backend.close()
        if replacement is not None:
            os.close(replacement)


def test_acknowledgement_retry_preserves_remaining_callbacks():
    class Owner(locks.CleanupLeaseOwner):
        _lock = threading.RLock()
    owner = Owner()
    called = []
    attempts = 0
    def second():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt()
        called.append('second')
    owner._cleanup_callbacks = [lambda: called.append('first'), second, lambda: called.append('third')]
    with pytest.raises(KeyboardInterrupt):
        owner._acknowledge_closed()
    assert called == ['first'] and len(owner._cleanup_callbacks) == 2
    owner._acknowledge_closed()
    assert called == ['first', 'second', 'third'] and not owner._cleanup_callbacks


def test_failed_disposal_start_preserves_descriptor_for_retry(fake_host, monkeypatch):
    backend = fake_host.backend
    original = locks.start_new_thread
    def failed(*args):
        raise RuntimeError('thread startup failed')
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        locks.retain_suite_cleanup(backend)
    monkeypatch.setattr(locks, 'start_new_thread', failed)
    with pytest.raises(RuntimeError, match='startup failed'):
        backend.close()
    assert lease.fd is not None and lease.closing is None
    assert len(backend._cleanup_callbacks) == 1
    assert_competing_suite_blocked()
    monkeypatch.setattr(locks, 'start_new_thread', original)
    backend.close()
    assert lease.fd is None and not backend._cleanup_callbacks
    assert_competing_suite_allowed()


@pytest.mark.parametrize('method', ['end_inspection', 'abort'])
def test_closed_inspection_retries_unacknowledged_callbacks(method):
    from api_agent.testing.inspection import IntegrationInspection
    handle = object.__new__(IntegrationInspection)
    handle.closed = True
    handle.pending = True
    handle._lock = threading.Lock()
    attempts = 0
    def acknowledge():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt()
    handle._cleanup_callbacks = [acknowledge]
    with pytest.raises(KeyboardInterrupt):
        getattr(handle, method)()
    assert len(handle._cleanup_callbacks) == 1
    getattr(handle, method)()
    assert attempts == 2 and not handle._cleanup_callbacks and not handle.pending


def test_native_descriptor_disposal_acknowledges_actual_kernel_close(tmp_path):
    fd = os.open(tmp_path / 'descriptor', os.O_CREAT | os.O_RDWR, 0o600)
    acknowledgement = Future()
    locks._dispose_descriptor(fd, acknowledgement)
    assert acknowledgement.done() and acknowledgement.result() is None
    with pytest.raises(OSError):
        os.fstat(fd)


def test_cancelled_launch_ticket_cannot_close_the_descriptor(tmp_path):
    fd = os.open(tmp_path / 'cancelled-launch', os.O_CREAT | os.O_RDWR, 0o600)
    acknowledgement = Future()
    acknowledgement.cancel()
    try:
        locks._dispose_descriptor(fd, acknowledgement)
        assert acknowledgement.cancelled()
        assert os.fstat(fd).st_nlink == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize('already_disposed', [False, True])
def test_interrupted_disposal_launch_preserves_exactly_one_owner(fake_host, monkeypatch, already_disposed):
    backend = fake_host.backend
    original = locks.start_new_thread
    def interrupted(action, arguments):
        if already_disposed:
            action(*arguments)
        raise KeyboardInterrupt()
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        locks.retain_suite_cleanup(backend)
    monkeypatch.setattr(locks, 'start_new_thread', interrupted)
    with pytest.raises(KeyboardInterrupt):
        backend.close()
    assert (lease.closing is None) is (not already_disposed)
    assert len(backend._cleanup_callbacks) == 1
    monkeypatch.setattr(locks, 'start_new_thread', original)
    backend.close()
    assert lease.fd is None and not backend._cleanup_callbacks
    assert_competing_suite_allowed()
