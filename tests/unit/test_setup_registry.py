"""Generic backend owners; no provider login, config, or external traffic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.setup_registry import SetupRegistry


class Owner:
    def __init__(self):
        self.calls = 0
        self.closes = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None
        self.close_error = False

    async def ensure_ready(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.error:
            raise self.error

    async def aclose(self):
        self.closes += 1
        if self.close_error:
            raise RuntimeError('private cleanup detail')


def run(body):
    asyncio.run(body())


@pytest.mark.parametrize('value', ['', 'x' * 513, None, True, 1])
def test_bad_identifiers(value):
    with pytest.raises(ContractError):
        SetupRegistry(value, 'selected', Owner)
    with pytest.raises(ContractError):
        SetupRegistry('operation', value, Owner)


def test_bad_factory():
    with pytest.raises(ContractError):
        SetupRegistry('operation', 'selected', None)


@pytest.mark.parametrize('value', [True, 0, -1, 1.0, None])
def test_bad_attempt(value):
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        with pytest.raises(ContractError):
            registry.register('worker', value)
        binding = registry.register('worker')
        binding['attempt'] = value
        with pytest.raises(ContractError):
            await registry.ensure_ready(binding)
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('value', ['', 'x' * 513, None, True])
def test_bad_worker(value):
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        with pytest.raises(ContractError):
            registry.register(value)
        binding = registry.register('worker')
        binding['worker_id'] = value
        with pytest.raises(ContractError):
            await registry.revoke(binding)
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('patch', [dict(version=True), dict(version=2), dict(operation_id='other'),
                                  dict(selected_identity='other'), dict(worker_id='unknown'),
                                  dict(attempt=2), dict(extra=1)])
def test_binding_checks_before_factory(patch):
    async def body():
        def factory():
            pytest.fail('invalid binding reached factory')
        registry = SetupRegistry('operation', 'selected', factory)
        binding = registry.register('worker') | patch
        with pytest.raises(ContractError):
            await registry.ensure_ready(binding)
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('binding', [None, {}, {'version': 1}])
def test_bad_binding_shape(binding):
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        with pytest.raises(ContractError):
            await registry.revoke(binding)
        await registry.aclose()
    run(body)


def test_registration_and_revocation():
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        binding = registry.register('worker')
        assert binding == dict(version=1, operation_id='operation', selected_identity='selected', worker_id='worker', attempt=1)
        with pytest.raises(ContractError):
            registry.register('worker', 2)
        await registry.revoke(binding)
        await registry.revoke(binding)
        with pytest.raises(SessionClosedError):
            await registry.ensure_ready(binding)
        with pytest.raises(ContractError):
            registry.register('worker')
        new = registry.register('worker', 2)
        await registry.revoke(binding)  # Original lease remains idempotent.
        await registry.ensure_ready(new)
        await registry.aclose()
        with pytest.raises(SessionClosedError):
            registry.register('late')
    run(body)


def test_coalescing_refresh_and_cancelled_waiter():
    async def body():
        owner = Owner()
        owner.release.clear()
        created = []
        def factory():
            created.append(owner)
            return owner
        registry = SetupRegistry('operation', 'selected', factory)
        first, second = registry.register('first'), registry.register('second')
        tasks = [asyncio.create_task(registry.ensure_ready(binding)) for binding in (first, second)]
        await owner.started.wait()
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not registry._ready.cancelled()
        owner.release.set()
        await tasks[1]
        await registry.ensure_ready(second)
        assert owner.calls == 2 and created == [owner]
        await registry.aclose()
        await registry.aclose()
        assert owner.closes == 1
    run(body)


def test_revocation_drains_waiter_but_preserves_sibling():
    async def body():
        owner = Owner()
        owner.release.clear()
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        first, second = registry.register('first'), registry.register('second')
        tasks = [asyncio.create_task(registry.ensure_ready(binding)) for binding in (first, second)]
        await owner.started.wait()
        await registry.revoke(first)
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        owner.release.set()
        await tasks[1]
        assert owner.calls == 1
        await registry.aclose()
    run(body)


def test_sticky_failure():
    async def body():
        owner = Owner()
        owner.error = ValueError('provider failure')
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        binding = registry.register('worker')
        for _ in range(2):
            with pytest.raises(ValueError, match='provider failure'):
                await registry.ensure_ready(binding)
        assert owner.calls == 1
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('ttl', [5, 0])
def test_registry_defers_failure_recovery_and_success_ttl_to_shared_owner(monkeypatch, ttl):
    from api_agent.providers import shared_setup
    from api_agent.providers.control import OperationalError
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.shared_setup import SharedSetupOwner
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(shared_setup, 'time', SimpleNamespace(monotonic=lambda: clock.now))

    async def body():
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            if len(calls) == 1:
                raise OperationalError('unavailable')
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        shared = SharedSetupOwner('operation', 'selected', lambda credentials: child,
                                  describe=describe, catalog_ttl=ttl)
        registry = SetupRegistry('operation', 'selected', lambda: shared)
        binding = registry.register('worker')
        with pytest.raises(OperationalError, match='unavailable'):
            await registry.describe(binding)
        assert (await registry.describe(binding))[0].selected_llm == 'sample'
        assert len(calls) == 2
        clock.now = 4.999
        assert (await registry.describe(binding))[0].selected_llm == 'sample'
        assert len(calls) == (2 if ttl else 3)
        clock.now = 5.0
        assert (await registry.describe(binding))[0].selected_llm == 'sample'
        assert len(calls) == (3 if ttl else 4)
        await registry.aclose()
        child.aclose.assert_awaited_once()
    run(body)


def test_registry_waiters_share_pending_catalog_and_cancellation_keeps_owner():
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.shared_setup import SharedSetupOwner
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            calls.append('describe')
            entered.set()
            await release.wait()
            return (AvailableSelection('sample'),)
        child = SimpleNamespace(call=call, aclose=AsyncMock())
        shared = SharedSetupOwner('operation', 'selected', lambda credentials: child,
                                  describe=describe, catalog_ttl=0)
        registry = SetupRegistry('operation', 'selected', lambda: shared)
        first, second = registry.register('first'), registry.register('second')
        waiters = [asyncio.create_task(registry.describe(binding)) for binding in (first, second)]
        await entered.wait()
        waiters[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiters[0]
        assert not registry._catalog.done()
        release.set()
        assert (await waiters[1])[0].selected_llm == 'sample'
        assert len(calls) == 1
        await asyncio.sleep(0)
        assert registry._catalog is None
        assert (await registry.describe(second))[0].selected_llm == 'sample'
        assert len(calls) == 2
        await registry.aclose()
        child.aclose.assert_awaited_once()
    run(body)


def test_stale_catalog_completion_cannot_clear_new_pending_retrieval():
    from api_agent.providers.llm import AvailableSelection
    async def body():
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = []
        class CatalogOwner(Owner):
            async def describe(self):
                index = len(calls)
                calls.append('describe')
                entered[index].set()
                await release[index].wait()
                return (AvailableSelection('sample'),)
        owner = CatalogOwner()
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        binding = registry.register('worker')
        first = asyncio.create_task(registry.describe(binding))
        await entered[0].wait()
        old_task = registry._catalog
        release[0].set()
        assert (await first)[0].selected_llm == 'sample'
        second = asyncio.create_task(registry.describe(binding))
        await entered[1].wait()
        current = registry._catalog
        registry._settle_catalog(old_task)
        assert registry._catalog is current and not current.done()
        release[1].set()
        assert (await second)[0].selected_llm == 'sample'
        assert calls == ['describe', 'describe']
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('owner', [object(), type('MissingClose', (), {'ensure_ready': lambda self: None})()])
def test_invalid_owner_sticky(owner):
    async def body():
        made = []
        def factory():
            made.append(owner)
            return owner
        registry = SetupRegistry('operation', 'selected', factory)
        binding = registry.register('worker')
        for _ in range(2):
            with pytest.raises(ContractError, match='Invalid setup owner'):
                await registry.ensure_ready(binding)
        assert made == [owner]
        await registry.aclose()
    run(body)


def test_close_cancels_shared_work_and_retries_disposal():
    async def body():
        owner = Owner()
        owner.release.clear()
        owner.close_error = True
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        binding = registry.register('worker')
        task = asyncio.create_task(registry.ensure_ready(binding))
        await owner.started.wait()
        with pytest.raises(CleanupPendingError) as error:
            await registry.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registry._owner is owner
        with pytest.raises(SessionClosedError):
            await registry.ensure_ready(binding)
        owner.close_error = False
        await error.value.retry()
        assert owner.closes == 2
        await registry.revoke(binding)
    run(body)


def test_revocation_after_waiter_completion_rejects_late_ack(monkeypatch):
    from api_agent.providers import setup_registry as module
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        binding = registry.register('worker')
        original = module._spawn
        count = 0
        revocations = []
        def spawn(awaitable):
            nonlocal count
            count += 1
            task = original(awaitable)
            if count == 2:  # Lease waiter, after the shared checkpoint.
                def revoke_after_completion(_):
                    revocations.append(asyncio.create_task(registry.revoke(binding)))
                task.add_done_callback(revoke_after_completion)
            return task
        monkeypatch.setattr(module, '_spawn', spawn)
        with pytest.raises(SessionClosedError, match='revoked'):
            await registry.ensure_ready(binding)
        await asyncio.gather(*revocations)
        await registry.aclose()
    run(body)


def test_revoked_lease_rejected_before_owner_side_effect():
    async def body():
        calls = []
        def factory():
            calls.append(True)
            return Owner()
        registry = SetupRegistry('operation', 'selected', factory)
        binding = registry.register('worker')
        await registry.revoke(binding)
        with pytest.raises(SessionClosedError):
            await registry.ensure_ready(binding)
        assert calls == []
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('field', ['operation_id', 'selected_identity'])
@pytest.mark.parametrize('value', ['', 'x' * 513, None, True])
def test_bound_identity_identifiers(field, value):
    async def body():
        registry = SetupRegistry('operation', 'selected', Owner)
        binding = registry.register('worker')
        binding[field] = value
        with pytest.raises(ContractError):
            await registry.revoke(binding)
        await registry.aclose()
    run(body)


def test_identifier_boundary():
    async def body():
        registry = SetupRegistry('o' * 512, 's' * 512, Owner)
        binding = registry.register('w' * 512)
        await registry.ensure_ready(binding)
        await registry.aclose()
    run(body)


@pytest.mark.parametrize('field', ['operation_id', 'selected_identity'])
def test_registry_identity_cannot_change_after_readiness(field):
    async def body():
        owner = Owner()
        factories = []
        def factory():
            factories.append(True)
            return owner
        registry = SetupRegistry('operation', 'selected', factory)
        binding = registry.register('first')
        await registry.ensure_ready(binding)
        with pytest.raises(AttributeError):
            setattr(registry, field, 'replacement')
        assert factories == [True] and owner.calls == 1
        next_binding = registry.register('second')
        assert next_binding['operation_id'] == 'operation'
        assert next_binding['selected_identity'] == 'selected'
        await registry.ensure_ready(binding)
        await registry.aclose()
    run(body)


def test_malformed_owner_retained_until_retryable_disposal():
    async def body():
        owner = Owner()
        owner.ensure_ready = None
        owner.close_error = True
        factories = []
        def factory():
            factories.append(True)
            return owner
        registry = SetupRegistry('operation', 'selected', factory)
        binding = registry.register('worker')
        for _ in range(2):
            with pytest.raises(ContractError, match='Invalid setup owner'):
                await registry.ensure_ready(binding)
        assert factories == [True] and owner.closes == 0
        with pytest.raises(CleanupPendingError) as error:
            await registry.aclose()
        assert owner.closes == 1 and registry._owner is owner
        owner.close_error = False
        await error.value.retry()
        await registry.aclose()
        assert owner.closes == 2
    run(body)


def test_shared_owner_deadline_survives_short_first_request():
    from api_agent.providers.progress import component_timeout, report_timeout, progress_sink, _scope
    async def body():
        reports = []
        class TimedOwner(Owner):
            async def ensure_ready(self):
                self.calls += 1
                async with component_timeout('Provider authentication', 1) as budget:
                    self.started.set()
                    await self.release.wait()
                    assert budget.timeout == 1 and budget.parent_id is None
        owner = TimedOwner()
        owner.release.clear()
        registry = SetupRegistry('operation', 'selected', lambda: owner)
        first, sibling = registry.register('first'), registry.register('sibling')
        async def short_request():
            async with report_timeout('First caller operation', 1) as caller:
                with progress_sink(reports.append):
                    with pytest.raises(TimeoutError):
                        async with component_timeout('First request', .03):
                            await registry.ensure_ready(first)
                    assert _scope.get() is caller
        async def release():
            async with component_timeout('Synthetic owner release', 1):
                await asyncio.sleep(.08)
                owner.release.set()
        short = asyncio.create_task(short_request())
        await owner.started.wait()
        async def long_request():
            async with component_timeout('Sibling request', 1):
                await registry.ensure_ready(sibling)
        await asyncio.gather(short, long_request(), release())
        assert owner.calls == 1
        provider_reports = [report for report in reports if report.name == 'Provider authentication']
        assert [report.active for report in provider_reports] == [True, False]
        assert all(report.timeout == 1 and report.parent_id is None for report in provider_reports)
        await registry.aclose()
    run(body)


def test_independent_timeout_scope_restores_caller_after_exception():
    from api_agent.providers.progress import independent_timeout_scope, report_timeout, _scope
    async def body():
        async with report_timeout('Caller operation', 1) as caller:
            with pytest.raises(ValueError, match='synthetic failure'):
                with independent_timeout_scope():
                    assert _scope.get() is None
                    raise ValueError('synthetic failure')
            assert _scope.get() is caller
        assert _scope.get() is None
    run(body)
