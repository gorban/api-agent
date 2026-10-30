"""Worker admission identities and replay counters survive cleanup races."""
import asyncio
import pytest

from api_agent.providers import local_call_registry, setup_slots
from api_agent.providers.contracts import ContractError


def test_client_rejects_invalid_attempt_and_pending_previous_authority():
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.telemetry.privacy import EmissionBoundary
    descriptor = {'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32}
    with pytest.raises(ContractError, match='Invalid setup attempt'):
        SetupClient(descriptor, attempt=0)
    client = SetupClient(descriptor)
    client._authority_lease = EmissionBoundary().begin_evidence_lease()
    try:
        with pytest.raises(ContractError, match='Previous authority handoff remains pending'):
            client.ensure_ready()
    finally:
        client._authority_lease.release()
        client._authority_lease = None
        client.close()


def test_local_call_rejects_duplicate_and_stale_cleanup_cannot_remove_new_owner():
    identity = local_call_registry.key('synthetic-capability', 'binding', 'nonce')
    first = local_call_registry.register(identity, object())
    try:
        with pytest.raises(RuntimeError, match='Duplicate local call identity'):
            local_call_registry.register(identity, object())
        local_call_registry.remove(identity, first)
        second = local_call_registry.register(identity, object())
        try:
            local_call_registry.remove(identity, first)
            assert local_call_registry.lookup(identity) is second
        finally:
            local_call_registry.remove(identity, second)
    finally:
        local_call_registry.remove(identity, first)
    assert local_call_registry.lookup(identity) is None


def test_worker_slots_bound_concurrency_and_advance_replay_counter():
    pool = setup_slots._Slots()
    active = [pool.acquire(0) for _ in range(8)]
    with pytest.raises(ContractError, match='slots are unavailable'):
        pool.acquire(0)
    released = active.pop()
    pool.release(released)
    again = pool.acquire(0)
    assert again is released and again[1] == 2
    for slot in [*active, again]:
        pool.release(slot)


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_unhanded_authority_is_released_only_after_receiver_closes(fails):
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.telemetry.privacy import EmissionBoundary

    boundary = EmissionBoundary()
    client = SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._authority_lease = boundary.begin_evidence_lease()
    closing, permit = asyncio.Event(), asyncio.Event()

    class Writer:
        def close(self):
            closing.set()

        async def wait_closed(self):
            await permit.wait()
            if fails:
                raise OSError('synthetic closure failure')

    client._writer = Writer()
    task = asyncio.create_task(client._close())
    registration = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        registration = asyncio.create_task(boundary.aregister_secret('synthetic-later'))
        await asyncio.sleep(0)
        assert not registration.done()
        permit.set()
        if fails:
            with pytest.raises(OSError, match='synthetic closure failure'):
                await task
        else:
            await task
        await asyncio.wait_for(registration, 1)
        assert client._authority_lease is None
    finally:
        permit.set()
        await asyncio.gather(task, return_exceptions=True)
        if registration is not None:
            if not registration.done():
                registration.cancel()
            await asyncio.gather(registration, return_exceptions=True)
