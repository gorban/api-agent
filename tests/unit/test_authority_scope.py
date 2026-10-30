import asyncio
import pytest

from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope, authority_scope, current_scope, join_current


def test_missing_or_incomplete_manifest_never_grants_authority():
    with pytest.raises(ContractError, match='manifest'):
        TestAuthorityScope(())
    for manifest in (None, ('ui', 'tools')):
        scope = TestAuthorityScope(manifest)
        scope.join('ui', EmissionBoundary(ContentPolicy(True)))
        with pytest.raises(ContractError, match='unavailable'):
            scope.authorities()


def test_denied_owner_does_not_authorize_content_handoff():
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', EmissionBoundary(ContentPolicy(False)))
    with pytest.raises(ContractError, match='unavailable'):
        scope.freeze()


def test_explicit_scope_admits_only_its_own_owners_and_closes():
    ui = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-ui',))
    tools = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-tool',))
    with authority_scope(('ui', 'tools')) as scope:
        join_current('ui', ui)
        join_current('tools', tools)
        assert set(scope.authorities()) == {ui, tools}
        with authority_scope(('ui',)) as other:
            other_ui = EmissionBoundary(ContentPolicy(True))
            join_current('ui', other_ui)
            assert other.authorities() == (other_ui,)
        assert current_scope() is scope
        with pytest.raises(ContractError):
            other.authorities()
    assert current_scope() is None
    with pytest.raises(ContractError):
        scope.join('ui', ui)


def test_undeclared_late_producer_invalidates_admission():
    with authority_scope(('ui',)) as scope:
        join_current('ui', EmissionBoundary())
        generation, authorities = scope.snapshot()
        assert len(authorities) == 1
        with pytest.raises(ContractError, match='Undeclared'):
            join_current('tools', EmissionBoundary())
        with pytest.raises(ContractError, match='unavailable'):
            scope.authorities()
        assert scope.commit_if_current(generation, lambda: pytest.fail('stale scope committed')) is False


def test_context_propagates_to_child_tasks_and_threads():
    async def scenario():
        with authority_scope(('tools',)) as scope:
            boundary = EmissionBoundary()
            await asyncio.to_thread(join_current, 'tools', boundary)
            assert scope.authorities() == (boundary,)
    asyncio.run(scenario())


def test_pytest_scope_uses_explicit_marker_and_revokes_after_test():
    from types import SimpleNamespace
    from api_agent.testing.provider_setup import _test_credential_authority_scope
    marker = SimpleNamespace(args=('ui',), kwargs={})
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: marker))
    fixture = _test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    scope = current_scope()
    owner = EmissionBoundary()
    join_current('ui', owner)
    assert scope.authorities() == (owner,)
    with pytest.raises(StopIteration):
        next(fixture)
    assert current_scope() is None
    with pytest.raises(ContractError):
        scope.authorities()


def test_async_registration_waits_for_owned_lease_without_blocking_loop():
    async def scenario():
        boundary = EmissionBoundary()
        lease = boundary.begin_evidence_lease()
        task = asyncio.create_task(boundary.aregister_secret('new-secret', timeout=2))
        await asyncio.sleep(0)
        assert not task.done()
        assert 'new-secret' not in boundary._secrets
        lease.release()
        await task
        assert 'new-secret' in boundary._secrets
        lease.release()
    asyncio.run(scenario())


def test_cancelled_registration_does_not_leave_waiters_or_register_secret():
    async def scenario():
        boundary = EmissionBoundary()
        lease = boundary.begin_evidence_lease()
        task = asyncio.create_task(boundary.aregister_secret('cancelled-secret', timeout=2))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not boundary._authority.registration_waiters
        assert 'cancelled-secret' not in boundary._secrets
        lease.release()
        await boundary.aregister_secret('later-secret', timeout=2)
        assert 'later-secret' in boundary._secrets
    asyncio.run(scenario())


def test_scope_freeze_defers_new_owner_and_registration_until_release():
    async def scenario():
        with authority_scope(('ui',)) as scope:
            first, second = EmissionBoundary(ContentPolicy(True)), EmissionBoundary(ContentPolicy(True))
            await scope.ajoin('ui', first)
            lease = scope.freeze()
            registration = asyncio.create_task(first.aregister_secret('later', timeout=2))
            enrollment = asyncio.create_task(scope.ajoin('ui', second, timeout=2))
            await asyncio.sleep(0)
            assert not registration.done() and not enrollment.done()
            assert lease.secrets == ()
            lease.release()
            await asyncio.gather(registration, enrollment)
            assert 'later' in first._secrets
            assert set(scope.authorities()) == {first, second}
    asyncio.run(scenario())


def test_scope_freeze_cancellation_and_idempotent_release():
    async def scenario():
        with authority_scope(('ui',)) as scope:
            await scope.ajoin('ui', EmissionBoundary(ContentPolicy(True)))
            lease = scope.freeze()
            task = asyncio.create_task(scope.ajoin('ui', EmissionBoundary(), timeout=2))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not scope._waiters
            lease.release()
            lease.release()
            assert scope._leases == 0
    asyncio.run(scenario())


def test_closing_scope_wakes_pending_owner_admission():
    async def scenario():
        scope = TestAuthorityScope(('ui',))
        await scope.ajoin('ui', EmissionBoundary(ContentPolicy(True)))
        lease = scope.freeze()
        waiting = asyncio.create_task(scope.ajoin('ui', EmissionBoundary(), timeout=2))
        await asyncio.sleep(0)
        scope.close()
        with pytest.raises(ContractError, match='closed'):
            await asyncio.wait_for(waiting, .5)
        lease.release()
    asyncio.run(scenario())


def test_scope_close_rejects_new_jobs_before_waiting_for_existing_job():
    from concurrent.futures import Future
    import threading
    from api_agent.providers.contracts import ContractError
    from api_agent.testing.authority_scope import TestAuthorityScope

    scope = TestAuthorityScope(('coordinator',))
    pending = Future()
    scope.enroll_evidence(pending)
    closing = threading.Thread(target=scope.close)
    closing.start()
    try:
        for _ in range(10000):
            with scope._lock:
                if scope._closing:
                    break
        assert scope._closing
        with pytest.raises(ContractError):
            scope.enroll_evidence(Future())
        assert closing.is_alive()
        lease = scope.freeze()
        lease.release()
    finally:
        pending.set_result(None)
        closing.join(timeout=2)
    assert not closing.is_alive()


def test_owned_lease_upgrade_waits_for_other_snapshot_and_refreshes_union():
    async def scenario():
        from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
        boundary = EmissionBoundary(ContentPolicy(True), secrets=('original',))
        owner = TestAuthorityScope(('ui',))
        other = TestAuthorityScope(('ui',))
        owner.join('ui', boundary)
        other.join('ui', boundary)
        owner_lease = owner.freeze()
        other_lease = other.freeze()
        try:
            task = asyncio.create_task(owner_lease.aupgrade(boundary, ['new-worker-secret']))
            await asyncio.sleep(0)
            assert not task.done()
            assert 'new-worker-secret' not in owner_lease.secrets
            other_lease.release()
            await asyncio.wait_for(task, 2)
            assert set(owner_lease.secrets) == {'original', 'new-worker-secret'}
            assert 'new-worker-secret' in boundary._secrets
        finally:
            owner_lease.release()
            other_lease.release()
            owner.close()
            other.close()
    asyncio.run(scenario())


def test_cancelled_owned_upgrade_does_not_register_or_strand_waiters():
    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        owner = TestAuthorityScope(('ui',))
        other = TestAuthorityScope(('ui',))
        owner.join('ui', boundary)
        other.join('ui', boundary)
        first, second = owner.freeze(), other.freeze()
        try:
            task = asyncio.create_task(first.aupgrade(boundary, ['unadmitted-secret']))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert 'unadmitted-secret' not in boundary._secrets
            assert not boundary._authority.registration_waiters
        finally:
            first.release()
            second.release()
            owner.close()
            other.close()
    asyncio.run(scenario())
