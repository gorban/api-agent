"""Every direct, attached and successor cleanup duty retains its own exclusion."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.operation import OperationRunner
from api_agent.testing import owned, setup_backend, suite_lock as locks
from test_recorded_integration import fixture, Storage
from test_suite_cleanup_lease import assert_competing_suite_allowed, assert_competing_suite_blocked


@pytest.fixture(params=[False, True], ids=['no-inner-backend', 'closed-inner-backend'])
def nested_failure(tmp_path, request):
    context, test, config = fixture(tmp_path)
    config['AGENT_DJANGO_URL'] = 'https://localhost:8443'
    @asynccontextmanager
    async def prepare():
        state = SimpleNamespace(runtime_live=False, released=False, allow_cleanup=False, primary_retries=0)
        backend = setup_backend.SetupBackend({}, tmp_path) if request.param else None
        class Manager:
            lease = object()
            def acquire(self):
                return self.lease
            def release(self, *, retain):
                state.released = True
        class Runtime:
            emissions = None
            async def astart(self):
                state.runtime_live = True
                if backend is not None:
                    locks.retain_suite_cleanup(backend)
                    backend.start()
            async def aclose(self):
                if not state.allow_cleanup:
                    raise RuntimeError('Synthetic runtime cleanup pending')
                state.runtime_live = False
        class Listener:
            async def start(self, *args): pass
            async def wait_ready(self, timeout): pass
            async def stop(self): pass
        async def primary_retry():
            state.primary_retries += 1
        state.primary = CleanupPendingError((object(),), primary_retry)
        operation = OperationRunner(resolve=lambda: SimpleNamespace(values=config, errors=(), missing=()),
            prepare_dependencies=lambda config: None,
            prepare_https=lambda *args: SimpleNamespace(material=object()), plan=lambda config: (),
            manager=lambda *args: Manager(), compose_runtime=lambda *args: Runtime(),
            create_application=lambda *args: object(), listener=Listener, timeout=.1)
        def runner(*args, **hooks):
            hooks['before_run']()
            if backend is not None:
                backend.close()
                assert backend.closed and not backend.thread.is_alive()
            raise state.primary
        try:
            with pytest.raises(CleanupPendingError) as caught:
                await owned.run_owned_integration(context, (test,), config, operation=operation,
                    accepted=True, runner=runner, storage=Storage())
            assert caught.value is state.primary
            state.outer = operation.pending_cleanup
            assert state.outer is state.primary.cleanup_pending and state.outer is not state.primary
            assert state.runtime_live and not state.released
            assert_competing_suite_blocked()
            yield state
        finally:
            state.allow_cleanup = True
            if operation.pending_cleanup is not None:
                await operation.pending_cleanup.retry()
            await state.primary.retry()
            if backend is not None:
                backend.close()
            assert not state.runtime_live and state.released
            assert_competing_suite_allowed()
    return prepare


def test_primary_retry_cannot_release_the_distinct_outer_runtime(nested_failure):
    async def scenario():
        async with nested_failure() as state:
            await state.primary.retry()
            assert state.primary_retries == 1
            assert state.runtime_live and not state.released
            assert_competing_suite_blocked()
            state.allow_cleanup = True
            await state.outer.retry()
            assert not state.runtime_live and state.released
            assert_competing_suite_allowed()
    asyncio.run(scenario())


def test_outer_retry_cannot_release_the_primary_workload_duty(nested_failure):
    async def scenario():
        async with nested_failure() as state:
            state.allow_cleanup = True
            await state.outer.retry()
            assert not state.runtime_live and state.released and state.primary_retries == 0
            assert_competing_suite_blocked()
            await state.primary.retry()
            assert state.primary_retries == 1
            assert_competing_suite_allowed()
    asyncio.run(scenario())


@pytest.mark.parametrize('order', [(0, 1), (1, 0)])
def test_successor_primary_and_attachment_must_both_acknowledge(order):
    completed = []
    async def first(): completed.append(0)
    async def second(): completed.append(1)
    direct, attached = CleanupPendingError((), first), CleanupPendingError((), second)
    direct.cleanup_pending = attached
    async def retry(): raise direct
    original = CleanupPendingError((), retry)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(original)
    with pytest.raises(CleanupPendingError) as caught:
        asyncio.run(original.retry())
    assert caught.value is direct
    receipts = (direct, attached)
    asyncio.run(receipts[order[0]].retry())
    assert completed == [order[0]]
    assert_competing_suite_blocked()
    asyncio.run(receipts[order[1]].retry())
    assert completed == list(order)
    assert_competing_suite_allowed()
    asyncio.run(original.retry())
    assert completed == list(order)


def test_a_self_receipt_still_requires_its_retry_despite_a_distinct_attachment():
    attempts = 0
    completed = []
    async def outer_retry(): completed.append('outer')
    outer = CleanupPendingError((), outer_retry)
    async def retry():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise original
        completed.append('original')
    original = CleanupPendingError((), retry)
    original.cleanup_pending = outer
    with locks.suite_lock():
        locks.retain_cleanup_receipt(original)
    with pytest.raises(CleanupPendingError):
        asyncio.run(original.retry())
    asyncio.run(outer.retry())
    assert completed == ['outer']
    assert_competing_suite_blocked()
    asyncio.run(original.retry())
    assert completed == ['outer', 'original']
    assert_competing_suite_allowed()


def test_receipt_chain_deduplicates_a_cycle_and_repeated_registration():
    completed = []
    async def first(): completed.append('first')
    async def second(): completed.append('second')
    one, two = CleanupPendingError((), first), CleanupPendingError((), second)
    one.cleanup_pending, two.cleanup_pending = two, one
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        locks.retain_cleanup_receipt(one)
        locks.retain_cleanup_receipt(two)
        assert lease.owners == {id(one), id(two)}
    asyncio.run(one.retry())
    assert_competing_suite_blocked()
    asyncio.run(two.retry())
    asyncio.run(one.retry())
    assert completed == ['first', 'second']
    assert_competing_suite_allowed()


def test_successor_dependencies_can_cycle_without_repeating_disposal():
    attempts = [0, 0]
    async def first():
        attempts[0] += 1
        if attempts[0] == 1: raise two
    async def second():
        attempts[1] += 1
        if attempts[1] == 1: raise one
    one, two = CleanupPendingError((), first), CleanupPendingError((), second)
    with locks.suite_lock(): locks.retain_cleanup_receipt(one)
    with pytest.raises(CleanupPendingError): asyncio.run(one.retry())
    with pytest.raises(CleanupPendingError): asyncio.run(two.retry())
    assert_competing_suite_blocked()
    asyncio.run(one.retry())
    assert attempts == [2, 1]
    asyncio.run(two.retry())
    assert attempts == [2, 1]
    assert_competing_suite_allowed()


def test_already_acknowledged_successor_can_complete_a_new_predecessor():
    calls = []
    async def complete(): calls.append('completed')
    done = CleanupPendingError((), complete)
    async def retry(): raise done
    later = CleanupPendingError((), retry)
    with locks.suite_lock():
        locks.retain_cleanup_receipt(done)
        locks.retain_cleanup_receipt(later)
    asyncio.run(done.retry())
    assert_competing_suite_blocked()
    with pytest.raises(CleanupPendingError): asyncio.run(later.retry())
    assert calls == ['completed']
    assert_competing_suite_allowed()


def test_repeated_failed_successor_does_not_add_duplicate_parent_callbacks():
    async def complete(): pass
    next_receipt = CleanupPendingError((), complete)
    async def retry(): raise next_receipt
    original = CleanupPendingError((), retry)
    with locks.suite_lock():
        lease = locks._CURRENT.get()
        locks.retain_cleanup_receipt(original)
    with pytest.raises(CleanupPendingError): asyncio.run(original.retry())
    with pytest.raises(CleanupPendingError): asyncio.run(original.retry())
    assert len(lease.receipts[id(next_receipt)].parents) == 1
    asyncio.run(next_receipt.retry())
    assert_competing_suite_allowed()
