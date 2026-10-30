"""Direct factory startup owns deferred admission and disposal across callers."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.api_providers.graphql_mcp import factory, GraphQLMcpError
from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import EmissionBoundary


@pytest.fixture
def setup(monkeypatch, tmp_path):
    owners = []
    class Owner:
        def __init__(self, *args, **kwargs):
            self.closes = 0
            self.fail_close = False
            owners.append(self)
        async def aclose(self):
            self.closes += 1
            if self.fail_close:
                raise RuntimeError('synthetic cleanup failure')
    monkeypatch.setattr(factory, 'GraphQLMcpOwner', Owner)
    monkeypatch.setattr(factory, 'installed_script', lambda _: tmp_path / 'installed.js')
    authority = EmissionBoundary()
    lease = authority.begin_evidence_lease()
    provider = factory.create_tool({'ENDPOINT': 'http://127.0.0.1:1/graphql',
        'HEADERS': {'Authorization': 'Bearer synthetic'}},
        {'owner_root': tmp_path, 'emissions': authority})
    return SimpleNamespace(provider=provider, lease=lease, owners=owners, authority=authority)


@pytest.mark.asyncio
async def test_concurrent_start_and_cancelled_waiter_share_one_owner(setup):
    p = setup.provider
    first = asyncio.create_task(p.astart())
    second = asyncio.create_task(p.astart())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done() and setup.owners == []
    setup.lease.release()
    await second
    assert len(setup.owners) == 1 and p._ready
    assert 'synthetic' in setup.authority._secrets
    await p.aclose()
    assert setup.owners[0].closes == 1


@pytest.mark.asyncio
async def test_close_during_frozen_admission_prevents_late_owner(setup):
    p = setup.provider
    starting = asyncio.create_task(p.astart())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await p.aclose()
    setup.lease.release()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await asyncio.sleep(0)
    assert setup.owners == [] and not p._ready and p._deferred is None
    with pytest.raises(GraphQLMcpError, match='provider_closed'):
        await p.astart()
    with pytest.raises(GraphQLMcpError, match='provider_closed'):
        p.for_invocation()


@pytest.mark.asyncio
async def test_start_failure_retry_reuses_owner_and_cleanup_retry(setup, monkeypatch):
    p = setup.provider
    setup.lease.release()
    monkeypatch.setattr(factory, 'installed_script', lambda _: (_ for _ in ()).throw(GraphQLMcpError('assets_missing')))
    attempts = []
    async def prepare(_):
        attempts.append(True)
        if len(attempts) == 1:
            raise GraphQLMcpError('asset_install')
    monkeypatch.setattr(factory, 'prepare_assets', prepare)
    with pytest.raises(GraphQLMcpError, match='asset_install'):
        await p.astart()
    assert len(setup.owners) == 1 and not p._ready
    await p.astart()
    assert len(setup.owners) == 1 and p._ready and len(attempts) == 2
    setup.owners[0].fail_close = True
    with pytest.raises(CleanupPendingError) as caught:
        await p.aclose()
    assert caught.value.owners == (p,)
    setup.owners[0].fail_close = False
    await caught.value.retry()
    assert setup.owners[0].closes == 2 and not p._ready


@pytest.mark.asyncio
async def test_close_during_asset_preparation_drains_owned_startup(setup, monkeypatch):
    p = setup.provider
    setup.lease.release()
    entered, drained = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(factory, 'installed_script', lambda _: (_ for _ in ()).throw(GraphQLMcpError('assets_missing')))
    async def prepare(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    monkeypatch.setattr(factory, 'prepare_assets', prepare)
    starting = asyncio.create_task(p.astart())
    await entered.wait()
    await p.aclose()
    assert drained.is_set() and setup.owners[0].closes == 1 and not p._ready
    with pytest.raises(asyncio.CancelledError):
        await starting


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['startup', 'owner'])
async def test_resistant_cleanup_retains_pending_task_and_retry(setup, monkeypatch, phase):
    p = setup.provider
    p._cleanup_timeout = .01
    setup.lease.release()
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == 'startup':
        monkeypatch.setattr(factory, 'installed_script', lambda _: (_ for _ in ()).throw(GraphQLMcpError('assets_missing')))
        async def prepare(_):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        monkeypatch.setattr(factory, 'prepare_assets', prepare)
        starting = asyncio.create_task(p.astart())
        await entered.wait()
    else:
        await p.astart()
        async def close():
            entered.set()
            await release.wait()
        setup.owners[0].aclose = close
    with pytest.raises(CleanupPendingError) as caught:
        await p.aclose()
    closing = p._closing
    assert not closing.done()
    release.set()
    await caught.value.retry()
    assert p._closing is closing and closing.done() and not p._ready
    if phase == 'startup':
        with pytest.raises(GraphQLMcpError, match='provider_closed'):
            await starting


@pytest.mark.asyncio
async def test_cancelled_owner_cleanup_retains_receipt_and_can_retry(setup):
    p = setup.provider
    setup.lease.release()
    await p.astart()
    async def cancelled_close():
        raise asyncio.CancelledError
    setup.owners[0].aclose = cancelled_close
    with pytest.raises(asyncio.CancelledError) as caught:
        await p.aclose()
    assert caught.value.cleanup_pending.owners == (p,)
    async def closed():
        setup.owners[0].closes += 1
    setup.owners[0].aclose = closed
    await caught.value.cleanup_pending.retry()
    assert setup.owners[0].closes == 1


@pytest.mark.asyncio
async def test_concurrent_close_waiters_keep_failed_attempt_identity(setup, monkeypatch):
    p = setup.provider
    setup.lease.release()
    await p.astart()
    first_release, successor_release = asyncio.Event(), asyncio.Event()
    attempts = []
    async def close():
        attempts.append(True)
        if len(attempts) == 1:
            await first_release.wait()
            raise RuntimeError('synthetic first disposal failure')
        await successor_release.wait()
    setup.owners[0].aclose = close
    real_wait = asyncio.wait
    waiters = 0
    async def ordered_wait(tasks, **kwargs):
        nonlocal waiters
        waiters += 1
        ordinal = waiters
        result = await real_wait(tasks, **kwargs)
        if ordinal == 2:
            # The first caller retries before the second caller consumes its
            # completed attempt. Its result must still belong to attempt one.
            while p._closing in tasks:
                await asyncio.sleep(0)
        return result
    monkeypatch.setattr(factory.asyncio, 'wait', ordered_wait)
    async def retry_immediately():
        with pytest.raises(CleanupPendingError):
            await p.aclose()
        await p.aclose()
    first = asyncio.create_task(retry_immediately())
    second = asyncio.create_task(p.aclose())
    while waiters < 2:
        await asyncio.sleep(0)
    first_release.set()
    with pytest.raises(CleanupPendingError):
        await second
    assert not first.done() and len(attempts) == 2
    successor_release.set()
    await first
