"""Real kernel exclusion, crash release and full adapter lifetime ownership."""
import asyncio
from contextlib import contextmanager
from contextvars import copy_context
import errno
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.progress import report_timeout_sync
from api_agent.testing import suite_lock as locks


@contextmanager
def holder(path):
    ready = path.with_suffix('.ready')
    script = '''from pathlib import Path
import sys
from api_agent.testing import suite_lock as locks
locks.suite_lock_path = lambda: Path(sys.argv[1])
with locks.suite_lock():
    Path(sys.argv[2]).touch()
    sys.stdin.read()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(path), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        with report_timeout_sync('Suite-lock holder startup', 10) as budget:
            while not ready.exists():
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read())
                if budget.remaining == 0:
                    raise TimeoutError('Suite-lock holder did not start')
                time.sleep(.01)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stderr.close()


def test_global_sidecar_is_independent_of_database_and_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, 'cache_path', lambda: tmp_path/'cache'/'cost-history.sqlite3')
    expected = tmp_path/'cache'/'test-suite.lock'
    assert locks.suite_lock_path() == expected
    monkeypatch.chdir(tmp_path)
    assert locks.suite_lock_path() == expected
    assert locks.suite_lock_path().name != locks.cache_path().name


def test_real_process_contention_and_sigkill_release(isolated_suite_lock):
    path = isolated_suite_lock
    with holder(path) as process:
        with pytest.raises(locks.SuiteBusyError, match='Another test suite is running'):
            with locks.suite_lock():
                pytest.fail('Concurrent suite entered')
        process.kill()
        process.wait(timeout=5)
        assert path.exists()  # File presence is never a stale lease.
        with locks.suite_lock():
            assert locks._CURRENT.get().active
    with locks.suite_lock():
        pass


@pytest.mark.parametrize('error', [ValueError('test failed'), KeyboardInterrupt(), SystemExit(1)])
def test_exception_release_and_no_descriptor_inheritance(error, isolated_suite_lock, monkeypatch):
    descriptors = []
    original = locks._lock
    def observe(fd):
        descriptors.append(fd)
        assert not os.get_inheritable(fd)
        original(fd)
    monkeypatch.setattr(locks, '_lock', observe)
    with pytest.raises(type(error)):
        with locks.suite_lock():
            raise error
    assert locks._CURRENT.get() is None
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    with locks.suite_lock():
        pass


def test_nested_wrappers_share_one_lease_but_other_threads_cannot_enter(monkeypatch):
    acquire = Mock(wraps=locks._lock)
    monkeypatch.setattr(locks, '_lock', acquire)
    @locks.suite_locked
    def nested():
        return locks._CURRENT.get()
    @locks.suite_locked
    def run():
        lease = locks._CURRENT.get()
        assert nested() is lease
        def competitor():
            with pytest.raises(locks.SuiteBusyError):
                nested()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(competitor).result(timeout=5)
        return lease
    assert run().active is False
    assert acquire.call_count == 2  # Owner plus rejected competitor; not nested.
    assert locks._CURRENT.get() is None


def test_async_owner_worker_bridge_and_independent_tasks_are_excluded():
    @locks.suite_locked
    def synchronous():
        return locks._CURRENT.get()
    @locks.suite_locked
    async def owned():
        lease = locks._CURRENT.get()
        assert await asyncio.to_thread(locks.suite_worker(synchronous)) is lease
        async def competing():
            with pytest.raises(locks.SuiteBusyError):
                with locks.suite_lock():
                    pytest.fail('Independent task reused its parent lease')
            with pytest.raises(locks.SuiteBusyError):
                await asyncio.to_thread(synchronous)
            with pytest.raises(locks.SuiteBusyError, match='Only the active'):
                locks.suite_worker(synchronous)
        await asyncio.create_task(competing())
        with locks.suite_lock():
            assert locks._CURRENT.get() is lease
        return lease
    lease = asyncio.run(owned())
    assert not lease.active
    assert locks._CURRENT.get() is None


def test_stale_propagated_context_and_foreign_process_are_not_reentrant(monkeypatch):
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        context = copy_context()
        monkeypatch.setattr(locks, 'get_ident', lambda: lease.thread + 1)
        assert not lease.owns_context()
        monkeypatch.setattr(locks, 'get_ident', lambda: lease.thread)
        lease.pid += 1
        assert not lease.owns_context()
        lease.pid -= 1
    def later():
        with locks.suite_lock():
            assert locks._CURRENT.get() is not lease
    context.run(later)


def test_unexpected_lock_error_preserved_and_descriptor_closed(monkeypatch):
    fd_seen=[]
    def fail(fd):
        fd_seen.append(fd)
        raise OSError(errno.EINVAL, 'synthetic unsupported filesystem')
    monkeypatch.setattr(locks, '_lock', fail)
    with pytest.raises(OSError, match='synthetic unsupported filesystem'):
        with locks.suite_lock():
            pytest.fail('Acquisition failed but suite started')
    with pytest.raises(OSError):
        os.fstat(fd_seen[0])
    assert locks._CURRENT.get() is None


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO proof')
def test_fifo_lock_is_rejected_without_blocking(isolated_suite_lock):
    os.mkfifo(isolated_suite_lock)
    with pytest.raises(ValueError, match='regular file'):
        with locks.suite_lock():
            pytest.fail('FIFO accepted')


def test_windows_one_byte_lock_and_existing_file(tmp_path, monkeypatch):
    fake = SimpleNamespace(LK_NBLCK=7, locking=Mock())
    monkeypatch.setitem(sys.modules, 'msvcrt', fake)
    monkeypatch.setattr(sys, 'platform', 'win32')
    fd = os.open(tmp_path/'windows.lock', os.O_CREAT|os.O_RDWR, 0o600)
    try:
        locks._lock(fd)
        assert os.fstat(fd).st_size == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        locks._lock(fd)
        assert os.fstat(fd).st_size == 1
        assert fake.locking.call_count == 2
        assert all(call.args == (fd, 7, 1) for call in fake.locking.call_args_list)
    finally:
        os.close(fd)


@pytest.mark.parametrize('module,function', [('runner','run_unit_tests'), ('integration','run_integration_tests'),
    ('recorded','run_recorded_integration'), ('launched','run_launched_integration'), ('owned','run_owned_integration')])
def test_all_suite_entrypoints_reject_before_configuration_or_side_effects(module, function, isolated_suite_lock):
    from importlib import import_module
    run = getattr(import_module('api_agent.testing.'+module), function)
    with holder(isolated_suite_lock):
        with pytest.raises(locks.SuiteBusyError):
            if asyncio.iscoroutinefunction(run):
                asyncio.run(run(None, (), {}, operation=None))
            elif module == 'runner':
                run(None, ())
            elif module == 'integration':
                run(None, ())
            else:
                run(None, (), {})


def test_launched_lease_covers_quota_recording_and_prerequisite_cleanup(tmp_path):
    from api_agent.testing.launched import run_launched_integration
    from test_recorded_integration import fixture, fake_runner, Storage
    context, test, config = fixture(tmp_path)
    observed=[]
    def check(phase):
        def probe():
            with pytest.raises(locks.SuiteBusyError):
                with locks.suite_lock():
                    pytest.fail('Suite ownership ended too early')
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(probe).result(timeout=5)
        observed.append(phase)
    prerequisites = SimpleNamespace(prepare=lambda *a, **k: check('prepare'),
                                    finish=lambda: check('prerequisites-finish'))
    class Destination(Storage):
        def persist(self, directory, manifest):
            check('publication')
            super().persist(directory, manifest)
    def quota():
        check('quota')
        return None
    result = run_launched_integration(context, [test], config, accepted=True,
        prerequisites=prerequisites, storage=Destination(), quota_fetcher=quota,
        finish=lambda: check('refresh-finish'),
        usage_output=lambda text: check('usage-output'), runner=fake_runner())
    assert result.persisted and result.exit_code == 0
    assert observed == ['prepare','quota','usage-output','quota','refresh-finish','usage-output','publication','prerequisites-finish']
    with locks.suite_lock():
        pass


def test_async_cancel_releases_only_after_synchronous_cleanup():
    async def run():
        entered=asyncio.Event()
        cleanup=asyncio.Event()
        @locks.suite_locked
        async def operation():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleanup.set()
                await asyncio.sleep(0)
                with locks.suite_lock():
                    assert locks._CURRENT.get().active
        task=asyncio.create_task(operation())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup.is_set()
        with locks.suite_lock():
            pass
    asyncio.run(run())


def test_explicit_bridge_is_thread_bound_and_cannot_outlive_owner():
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        def probe():
            assert lease.owns_context()
            with locks.suite_lock():
                assert locks._CURRENT.get() is lease
            context = copy_context()
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert not executor.submit(context.run, lease.owns_context).result(timeout=5)
        invoke = locks.suite_worker(probe)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(invoke).result(timeout=5)
        assert locks._DELEGATED_THREAD.get() is None
    with pytest.raises(locks.SuiteBusyError, match='already stopped'):
        invoke()
    # A copied delegation from a different process must also be rejected.
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        invoke = locks.suite_worker(lambda: None)
        lease.pid += 1
        with pytest.raises(locks.SuiteBusyError, match='already stopped'):
            invoke()
        lease.pid -= 1
    assert locks.suite_worker(lambda value: value, 7)() == 7


def test_delegation_for_another_lease_does_not_authorize_a_thread():
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        context = copy_context()
        def probe():
            token = locks._DELEGATED_THREAD.set((object(), locks.get_ident()))
            try:
                assert not lease.owns_context()
            finally:
                locks._DELEGATED_THREAD.reset(token)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(context.run, probe).result(timeout=5)



def test_interruption_while_publishing_claim_cannot_leave_an_active_reentrant_lease(monkeypatch):
    original = locks._CURRENT
    def interrupted(value):
        original.set(value)
        raise KeyboardInterrupt
    try:
        monkeypatch.setattr(locks, '_CURRENT', SimpleNamespace(get=original.get, set=interrupted, reset=original.reset))
        with pytest.raises(KeyboardInterrupt):
            with locks.suite_lock():
                pytest.fail('Interrupted claim started work')
        assert not original.get().active
        monkeypatch.setattr(locks, '_CURRENT', original)
        with locks.suite_lock():
            assert original.get().active
    finally:
        original.set(None)
