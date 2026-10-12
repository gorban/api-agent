"""Detached backend clocks rejoin each waiter's display without owning its deadline."""
import asyncio
from dataclasses import asdict

import pytest

from api_agent.providers import progress
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.tui.test_results import component_timing


def client_descriptor():
    return dict(version=1, host='127.0.0.1', port=43123,
                capability='public-synthetic-capability-value-000000')


def relay(client, identifier, timeout, elapsed=0, *, active=True, parent=None):
    event = progress.ComponentProgress(identifier, 'Shared provider setup', timeout,
                                       elapsed, active, parent)
    client._progress(dict(version=1, status='progress', component=asdict(event)))


class TimingRows:
    def __init__(self):
        self.rows = {}
        self.events = []

    def observe(self, event):
        self.events.append(event)
        if event.active:
            self.rows[event.id] = event
        else:
            self.rows.pop(event.id, None)

    def text(self):
        return component_timing(self.rows.values())


@pytest.mark.parametrize('outer_limit,worker_start,child_start,remote_elapsed,entry_budget,expected', [
    (300, 10, 40, 0, 180, '0s elapsed; timeout 3m — Shared provider setup'),
    (60, 20, 50, 0, 10, '0s elapsed; timeout 10s — Shared provider setup'),
    (60, 20, 50, 5, 15, '5s elapsed; timeout 15s — Shared provider setup'),
])
def test_relay_uses_tightest_recursive_budget_and_restores_parent_clock(
        monkeypatch, outer_limit, worker_start, child_start, remote_elapsed, entry_budget, expected):
    now = [0.0]
    monkeypatch.setattr(progress, '_clock', lambda: now[0])
    rows = TimingRows()
    client = SetupClient(client_descriptor())

    async def scenario():
        with progress.progress_sink(rows.observe):
            async with progress.report_timeout('Suite', outer_limit) as outer:
                now[0] = worker_start
                async with progress.report_timeout('Worker', 400) as worker:
                    now[0] = child_start
                    relay(client, 'login', 180, remote_elapsed)
                    child = rows.rows['login']
                    assert child.parent_id == worker.id
                    assert child.timeout == entry_budget
                    assert rows.text() == expected

                    now[0] += 5
                    # A delayed frame must not recalculate the original allowance.
                    relay(client, 'login', 180, remote_elapsed + 4)
                    assert rows.rows['login'].timeout == entry_budget
                    # Observe actual ancestor heartbeats while the leaf stays active.
                    await asyncio.sleep(1.05)
                    assert 'Worker' not in rows.text() and 'Suite' not in rows.text()
                    relay(client, 'login', 180, remote_elapsed + 5, active=False)
                    restored = rows.rows[worker.id]
                    assert restored.elapsed == child_start + 5 - worker_start
                    assert restored.timeout == outer_limit - worker_start
                    assert rows.text().endswith(' — Worker')
                    assert str(int(restored.elapsed)) + 's elapsed' in rows.text()
                assert rows.rows[outer.id].elapsed == child_start + 5
                assert rows.rows[outer.id].timeout == outer_limit
                assert rows.text().endswith(' — Suite')
        assert not rows.rows
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())
    client.close()


@pytest.mark.parametrize('worker_limit,remote_limit', [(None, 180), (300, None), (None, None)])
def test_relay_preserves_untimed_fallback_and_optional_limits(worker_limit, remote_limit):
    rows = TimingRows()
    client = SetupClient(client_descriptor())

    async def scenario():
        with progress.progress_sink(rows.observe):
            async with progress.report_timeout('Worker', worker_limit) as worker:
                relay(client, 'remote', remote_limit)
                assert rows.rows['remote'].parent_id == worker.id
                assert rows.rows['remote'].timeout == remote_limit
                assert rows.text().count('elapsed') == 1
                relay(client, 'remote', remote_limit, active=False)
        assert not rows.rows

    asyncio.run(scenario())
    client.close()


class DetachedLoginRegistry:
    def __init__(self):
        self.release = asyncio.Event()

    def register(self, worker, attempt):
        return dict(version=1, operation_id='public-operation', selected_identity='public-selection',
                    worker_id=worker, attempt=attempt)

    async def ensure_ready(self, binding):
        with progress.independent_timeout_scope():
            async with progress.report_timeout('Private login name must stay local', 180):
                await self.release.wait()

    async def revoke(self, binding):
        pass


def test_real_setup_transport_shows_login_leaf_then_restores_worker():
    async def scenario():
        rows = TimingRows()
        arrived = asyncio.Event()
        loop = asyncio.get_running_loop()
        registry = DetachedLoginRegistry()
        server = SetupServer(registry, client_descriptor()['capability'])
        client = SetupClient(await server.start())

        def observe(event):
            def apply():
                rows.observe(event)
                if event.name == 'Shared provider setup' and event.timeout == 180 and event.active:
                    arrived.set()
            loop.call_soon_threadsafe(apply)

        try:
            with progress.progress_sink(observe):
                pending = asyncio.create_task(asyncio.to_thread(client.ensure_ready))
                await asyncio.wait_for(arrived.wait(), 3)
                assert rows.text() == '0s elapsed; timeout 3m — Shared provider setup'
                assert 'Worker' not in rows.text()
                registry.release.set()
                await asyncio.wait_for(pending, 3)
            stopped = [e for e in rows.events if not e.active and e.name == 'Shared provider setup']
            assert stopped and all(e.parent_id is not None for e in stopped)
            assert not rows.rows and not client._progress_timeouts
        finally:
            registry.release.set()
            await asyncio.to_thread(client.close)
            await server.aclose()

    asyncio.run(scenario())



def test_remote_root_cannot_impersonate_its_local_parent():
    from api_agent.providers.contracts import ContractError
    client = SetupClient(client_descriptor())

    async def scenario():
        async with progress.report_timeout('Worker', 300) as scope:
            with pytest.raises(ContractError, match='Invalid setup progress'):
                relay(client, scope.id, 180)

    asyncio.run(scenario())
    client.close()



def test_late_child_rejoins_ancestor_as_heartbeats_arrive_and_depart():
    rows = TimingRows()
    client = SetupClient(client_descriptor())

    async def scenario():
        with progress.progress_sink(rows.observe):
            async with progress.report_timeout('Worker', 300) as worker:
                relay(client, 'child', 15, 3, parent='parent')
                assert rows.rows['child'].parent_id == worker.id
                assert rows.text() == '3s elapsed; timeout 15s — Shared provider setup'

                relay(client, 'parent', 60, 12, parent='grandparent')
                assert rows.rows['child'].parent_id == 'parent'
                assert rows.rows['parent'].parent_id == worker.id
                assert rows.text() == '3s elapsed; timeout 15s — Shared provider setup'

                relay(client, 'grandparent', 120, 20)
                assert rows.rows['parent'].parent_id == 'grandparent'
                assert rows.text() == '3s elapsed; timeout 15s — Shared provider setup'

                relay(client, 'child', 15, 4, active=False, parent='parent')
                assert rows.text() == '12s elapsed; timeout 1m — Shared provider setup'
                relay(client, 'parent', 60, 13, active=False, parent='grandparent')
                assert rows.text() == '20s elapsed; timeout 2m — Shared provider setup'
                relay(client, 'grandparent', 120, 21, active=False)
                assert rows.text().endswith(' — Worker')
        assert not rows.rows

    asyncio.run(scenario())
    client.close()


def test_remote_parent_departure_reanchors_its_still_active_child():
    rows = TimingRows()
    client = SetupClient(client_descriptor())

    async def scenario():
        with progress.progress_sink(rows.observe):
            async with progress.report_timeout('Worker', 300) as worker:
                relay(client, 'parent', 60)
                relay(client, 'child', 15, 2, parent='parent')
                relay(client, 'parent', 60, 3, active=False)
                assert rows.rows['child'].parent_id == worker.id
                assert rows.text() == '2s elapsed; timeout 15s — Shared provider setup'
                relay(client, 'child', 15, 3, active=False, parent='parent')
        assert not rows.rows

    asyncio.run(scenario())
    client.close()
