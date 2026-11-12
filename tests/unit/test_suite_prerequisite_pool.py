"""Suite ownership remains independent of the lifetime of individual borrowers."""
import threading
import time

import pytest

from api_agent.testing.suite_prerequisite_pool import (
    PoolCancelled, PoolClosed, PoolPending, SuitePrerequisitePool,
)


class Owner:
    def __init__(self, gate=None, fail=False):
        self.gate = gate
        self.fail = fail
        self.starts = 0
        self.stops = 0
        self.entered = threading.Event()

    def acquire(self, *, deadline):
        self.starts += 1
        self.entered.set()
        if self.gate:
            assert self.gate.wait(max(0, deadline - time.monotonic()))
        if self.fail:
            raise ValueError('startup failed')
        return 'verified'

    def release(self):
        self.stops += 1


def test_simultaneous_borrowers_coalesce_and_suite_owns_lifetime():
    pool = SuitePrerequisitePool(owner_budget=5)
    gate = threading.Event()
    owner = Owner(gate)
    leases = []
    threads = [threading.Thread(target=lambda: leases.append(pool.borrow('a' * 64, lambda: owner)))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    assert owner.entered.wait(2)
    gate.set()
    for thread in threads:
        thread.join(2)
    assert len(leases) == 8 and all(lease.value == 'verified' for lease in leases)
    assert owner.starts == 1
    for lease in leases:
        lease.release()
        lease.release()
    assert owner.stops == 0
    pool.close(timeout=2)
    assert owner.stops == 1


def test_cancelled_waiter_does_not_cancel_preparation():
    pool = SuitePrerequisitePool(owner_budget=5)
    gate, cancelled = threading.Event(), threading.Event()
    owner = Owner(gate)
    errors = []
    def wait():
        try:
            pool.borrow('a' * 64, lambda: owner, cancelled=cancelled.is_set)
        except PoolCancelled as error:
            errors.append(error)
    thread = threading.Thread(target=wait)
    thread.start()
    assert owner.entered.wait(2)
    cancelled.set()
    thread.join(2)
    assert len(errors) == 1
    gate.set()
    lease = pool.borrow('a' * 64, lambda: pytest.fail('duplicate owner'))
    lease.release()
    pool.close(timeout=2)
    assert owner.starts == owner.stops == 1


def test_failure_is_shared_until_explicit_cleanup_and_invalidation():
    pool = SuitePrerequisitePool(owner_budget=5)
    owner = Owner(fail=True)
    for _ in range(2):
        with pytest.raises(ValueError, match='startup failed'):
            pool.borrow('a' * 64, lambda: owner)
    assert owner.starts == 1
    pool.invalidate('a' * 64)
    assert owner.stops == 1
    replacement = Owner()
    lease = pool.borrow('a' * 64, lambda: replacement)
    lease.release()
    pool.close(timeout=2)
    assert replacement.stops == 1


def test_drain_waits_for_borrower_and_retries_cleanup():
    pool = SuitePrerequisitePool(owner_budget=5)
    owner = Owner()
    lease = pool.borrow('a' * 64, lambda: owner)
    with pytest.raises(PoolPending):
        pool.close(timeout=0)
    with pytest.raises(PoolClosed):
        pool.borrow('b' * 64, Owner)
    assert owner.stops == 0
    lease.release()
    pool.close(timeout=2)
    assert owner.stops == 1


def test_distinct_digests_and_failed_cleanup_are_isolated():
    pool = SuitePrerequisitePool(owner_budget=5)
    first, second = Owner(), Owner()
    a = pool.borrow('a' * 64, lambda: first)
    b = pool.borrow('b' * 64, lambda: second)
    a.release()
    b.release()
    original = first.release
    def fail_once():
        first.release = original
        raise RuntimeError('cleanup pending')
    first.release = fail_once
    with pytest.raises(RuntimeError, match='cleanup pending'):
        pool.close(timeout=2)
    assert first.stops == second.stops == 0
    pool.close(timeout=2)
    assert first.stops == second.stops == 1


def test_invalid_key_never_starts_owner():
    pool = SuitePrerequisitePool(owner_budget=1)
    with pytest.raises(ValueError, match='digest'):
        pool.borrow('contains-secret', lambda: pytest.fail('must not start'))
    pool.close(timeout=1)


def test_failed_invalidation_never_reissues_partially_disposed_value():
    pool = SuitePrerequisitePool(owner_budget=2)
    owner = Owner()
    lease = pool.borrow('a' * 64, lambda: owner)
    lease.release()
    calls = []
    def release():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('partially disposed')
    owner.release = release
    with pytest.raises(RuntimeError, match='partially disposed'):
        pool.invalidate('a' * 64)
    with pytest.raises(PoolPending):
        pool.borrow('a' * 64, lambda: pytest.fail('must settle cleanup first'))
    pool.invalidate('a' * 64)
    replacement = Owner()
    lease = pool.borrow('a' * 64, lambda: replacement)
    lease.release()
    pool.close(timeout=2)
    assert len(calls) == 2 and replacement.stops == 1


def test_stale_dispose_cannot_touch_replacement():
    pool = SuitePrerequisitePool(owner_budget=2)
    first, replacement = Owner(), Owner()
    lease = pool.borrow('a' * 64, lambda: first)
    lease.release()
    old_entry = pool._entries['a' * 64]
    selected, resume = threading.Event(), threading.Event()
    def stale_dispose():
        selected.set()
        assert resume.wait(2)
        pool._dispose('a' * 64, old_entry, time.monotonic() + 1)
    thread = threading.Thread(target=stale_dispose)
    thread.start()
    assert selected.wait(1)
    pool.invalidate('a' * 64)
    new_lease = pool.borrow('a' * 64, lambda: replacement)
    resume.set()
    thread.join(2)
    assert not thread.is_alive()
    assert first.stops == 1 and replacement.stops == 0
    new_lease.release()
    pool.close(timeout=2)
    assert replacement.stops == 1


def test_cleanup_timeout_keeps_one_inflight_release_until_acknowledged():
    pool = SuitePrerequisitePool(owner_budget=2)
    owner = Owner()
    entered, gate = threading.Event(), threading.Event()
    def release():
        owner.stops += 1
        entered.set()
        assert gate.wait(3)
    owner.release = release
    lease = pool.borrow('a' * 64, lambda: owner)
    lease.release()
    with pytest.raises(PoolPending):
        pool.close(timeout=0.05)
    assert entered.wait(1)
    with pytest.raises(PoolPending):
        pool.close(timeout=0.05)
    assert owner.stops == 1
    gate.set()
    pool.close(timeout=2)
    assert owner.stops == 1


def test_late_startup_is_never_published_and_cleanup_waits_for_owner():
    pool = SuitePrerequisitePool(owner_budget=0.05)
    gate = threading.Event()
    class LateOwner(Owner):
        def acquire(self, *, deadline):
            self.starts += 1
            self.entered.set()
            assert gate.wait(3)
            return 'stale'
    owner = LateOwner()
    with pytest.raises(TimeoutError):
        pool.borrow('a' * 64, lambda: owner)
    with pytest.raises(PoolPending):
        pool.invalidate('a' * 64, timeout=0.05)
    with pytest.raises(PoolPending):
        pool.close(timeout=0)
    gate.set()
    pool.close(timeout=2)
    assert owner.stops == 1


def test_failed_startup_fans_out_and_concurrent_releases_are_idempotent():
    pool = SuitePrerequisitePool(owner_budget=2)
    gate, ready = threading.Event(), threading.Barrier(9)
    owner = Owner(gate, fail=True)
    errors = []
    def wait():
        ready.wait()
        try:
            pool.borrow('a' * 64, lambda: owner)
        except ValueError as error:
            errors.append(str(error))
    threads = [threading.Thread(target=wait) for _ in range(8)]
    for thread in threads:
        thread.start()
    ready.wait()
    assert owner.entered.wait(1)
    gate.set()
    for thread in threads:
        thread.join(2)
    assert errors == ['startup failed'] * 8 and owner.starts == 1
    pool.invalidate('a' * 64)
    healthy = Owner()
    lease = pool.borrow('b' * 64, lambda: healthy)
    threads = [threading.Thread(target=lease.release) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    pool.close(timeout=2)
    assert healthy.stops == 1


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 0, True, '1'])
def test_owner_budget_must_be_finite_positive_number(value):
    with pytest.raises(ValueError):
        SuitePrerequisitePool(owner_budget=value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, '1'])
def test_cleanup_timeout_must_be_finite_nonnegative_number(value):
    pool = SuitePrerequisitePool(owner_budget=1)
    with pytest.raises(ValueError):
        pool.close(timeout=value)
    with pytest.raises(ValueError):
        pool.invalidate('a' * 64, timeout=value)


def test_thread_start_failure_is_shared_and_cleanup_can_be_retried(monkeypatch):
    original = threading.Thread.start
    calls = []
    def fail_once(thread):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('thread unavailable')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_once)
    pool = SuitePrerequisitePool(owner_budget=2)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='thread unavailable'):
            pool.borrow('a' * 64, lambda: pytest.fail('owner must not start'))
    pool.invalidate('a' * 64)
    owner = Owner()
    lease = pool.borrow('a' * 64, lambda: owner)
    lease.release()
    pool.close(timeout=2)
    assert owner.stops == 1


def test_cleanup_thread_start_failure_blocks_borrow_until_retry(monkeypatch):
    pool = SuitePrerequisitePool(owner_budget=2)
    owner = Owner()
    lease = pool.borrow('a' * 64, lambda: owner)
    lease.release()
    original = threading.Thread.start
    calls = []
    def fail_once(thread):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('thread unavailable')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_once)
    with pytest.raises(RuntimeError, match='thread unavailable'):
        pool.invalidate('a' * 64)
    with pytest.raises(PoolPending):
        pool.borrow('a' * 64, lambda: pytest.fail('cleanup required'))
    pool.invalidate('a' * 64)
    assert owner.stops == 1
    pool.close(timeout=2)


def test_owner_jobs_inherit_callers_context():
    import contextvars
    binding = contextvars.ContextVar('suite_binding')
    token = binding.set('suite')
    observed = []
    class ContextOwner(Owner):
        def acquire(self, *, deadline):
            observed.append(('acquire', binding.get()))
            return super().acquire(deadline=deadline)
        def release(self):
            observed.append(('release', binding.get()))
            super().release()
    try:
        pool = SuitePrerequisitePool(owner_budget=2)
        lease = pool.borrow('a' * 64, ContextOwner)
        lease.release()
        pool.close(timeout=2)
    finally:
        binding.reset(token)
    assert observed == [('acquire', 'suite'), ('release', 'suite')]


def test_context_managed_borrower_releases_on_exception():
    pool = SuitePrerequisitePool(owner_budget=2)
    owner = Owner()
    with pytest.raises(ValueError, match='scenario failed'):
        with pool.borrow('a' * 64, lambda: owner) as value:
            assert value == 'verified'
            raise ValueError('scenario failed')
    pool.close(timeout=2)
    assert owner.stops == 1
    pool.invalidate('b' * 64)


def test_drain_wakes_a_waiter_without_cancelling_owner():
    pool = SuitePrerequisitePool(owner_budget=2)
    gate = threading.Event()
    owner = Owner(gate)
    errors = []
    def wait():
        try:
            pool.borrow('a' * 64, lambda: owner)
        except PoolClosed:
            errors.append('closed')
    thread = threading.Thread(target=wait)
    thread.start()
    assert owner.entered.wait(1)
    with pytest.raises(PoolPending):
        pool.close(timeout=0)
    thread.join(1)
    assert errors == ['closed']
    gate.set()
    pool.close(timeout=2)
    assert owner.stops == 1


def test_stale_cleanup_checks_identity_before_release_and_removal():
    pool = SuitePrerequisitePool(owner_budget=2)
    old, new = Owner(), Owner()
    lease = pool.borrow('a' * 64, lambda: old)
    lease.release()
    stale = pool._entries['a' * 64]
    pool.invalidate('a' * 64)
    lease = pool.borrow('a' * 64, lambda: new)
    stale.disposing = True
    pool._cleanup('a' * 64, stale)
    assert not stale.disposing and old.stops == 1 and new.stops == 0
    lease.release()
    pool.close(timeout=2)


def test_cleanup_does_not_remove_replaced_entry_after_release():
    pool = SuitePrerequisitePool(owner_budget=2)
    old, replacement = Owner(), Owner()
    entered, gate = threading.Event(), threading.Event()
    def release():
        old.stops += 1
        entered.set()
        assert gate.wait(2)
    old.release = release
    lease = pool.borrow('a' * 64, lambda: old)
    lease.release()
    stale = pool._entries['a' * 64]
    thread = threading.Thread(target=pool._cleanup, args=('a' * 64, stale))
    thread.start()
    assert entered.wait(1)
    # Simulate an identity change during a delayed native release; the final
    # acknowledgement must not remove or release the replacement.
    with pool._condition:
        del pool._entries['a' * 64]
    new_lease = pool.borrow('a' * 64, lambda: replacement)
    gate.set()
    thread.join(2)
    assert not thread.is_alive() and replacement.stops == 0
    new_lease.release()
    pool.close(timeout=2)
    assert old.stops == replacement.stops == 1


@pytest.mark.parametrize('transition', ['close', 'cleanup'])
def test_waiter_rechecks_admission_after_startup_wakeup(monkeypatch, transition):
    pool = SuitePrerequisitePool(owner_budget=2)
    gate = threading.Event()
    owner = Owner(gate)
    original_wait = pool._condition.wait
    def wait_and_transition(timeout=None):
        result = original_wait(timeout)
        entry = pool._entries['a' * 64]
        if not entry.starting:
            # Model an admission transition that wins the lock between startup
            # acknowledgement and this waiter's reacquisition.
            if transition == 'close':
                pool._closed = True
            else:
                entry.cleanup_required = True
        return result
    monkeypatch.setattr(pool._condition, 'wait', wait_and_transition)
    errors = []
    def wait():
        try:
            pool.borrow('a' * 64, lambda: owner)
        except (PoolClosed, PoolPending) as error:
            errors.append(error)
    thread = threading.Thread(target=wait)
    thread.start()
    assert owner.entered.wait(1)
    gate.set()
    thread.join(2)
    assert len(errors) == 1
    assert isinstance(errors[0], PoolClosed if transition == 'close' else PoolPending)
    monkeypatch.setattr(pool._condition, 'wait', original_wait)
    pool.close(timeout=2)
    assert owner.stops == 1


def test_stop_admission_retains_existing_lease_and_rejects_new_owner():
    pool = SuitePrerequisitePool(owner_budget=5)
    owner = Owner()
    borrower = pool.borrow('1' * 64, lambda: owner)
    pool.stop_admission()
    with pytest.raises(PoolClosed):
        pool.borrow('2' * 64, lambda: pytest.fail('owner started after admission stopped'))
    assert owner.stops == 0
    borrower.release()
    pool.close(timeout=5)
    assert owner.stops == 1
