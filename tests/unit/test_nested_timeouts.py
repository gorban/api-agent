"""Nested budgets retain independent clocks and honor the earliest deadline."""
import asyncio
from dataclasses import replace

import pytest

from api_agent.providers import progress as p
from api_agent.tui.test_results import component_timing


def test_recursive_budget_uses_remaining_time_at_child_entry(monkeypatch):
    async def run():
        now = [100.0]
        monkeypatch.setattr(p, '_clock', lambda: now[0])
        rows = []
        with p.progress_sink(rows.append):
            async with p.report_timeout('Graph', 60) as graph:
                now[0] = 120
                async with p.report_timeout('Group', 100) as group:
                    assert group.timeout == 40
                    now[0] = 145
                    async with p.report_timeout('Node', 80) as node:
                        assert node.timeout == 15
                        assert node.remaining == 15
                        now[0] = 150
                        assert node.timeout == 15  # A fixed budget, not a countdown.
                        assert node.remaining == 10
                    assert graph.remaining == 10
                    async with p.report_timeout('Short node', 3) as shorter:
                        assert shorter.timeout == 3
                        assert shorter.deadline == 153
        child = [row for row in rows if row.name == 'Node']
        assert child[0].elapsed == 0 and child[-1].elapsed == 5
        assert all(row.timeout == 15 for row in child)
        assert child[0].parent_id == group.id
        assert rows[0].parent_id is None
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_unobserved_scope_still_clamps_and_restores_after_error(monkeypatch):
    async def run():
        now = [0.0]
        monkeypatch.setattr(p, '_clock', lambda: now[0])
        async with p.report_timeout('Parent', 5):
            now[0] = 3
            with pytest.raises(RuntimeError):
                async with p.report_timeout('Child', 10) as child:
                    assert child.timeout == 2
                    raise RuntimeError('synthetic')
            async with p.report_timeout('Sibling', 1) as sibling:
                assert sibling.timeout == 1
            now[0] = 8
            async with p.report_timeout('Expired parent child', 3) as expired:
                assert expired.timeout == expired.remaining == 0
        async with p.report_timeout('Independent', 10) as independent:
            assert independent.timeout == 10 and independent.parent_id is None
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


@pytest.mark.parametrize('limit', [None, 5])
def test_untimed_scope_inherits_deadline_without_claiming_own_timeout(monkeypatch, limit):
    async def run():
        now = [0.0]
        monkeypatch.setattr(p, '_clock', lambda: now[0])
        rows = []
        with p.progress_sink(rows.append):
            async with p.report_timeout('Parent', limit) as parent:
                now[0] = 2
                async with p.report_timeout('Untimed', None) as child:
                    assert child.deadline == parent.deadline
                    assert child.remaining == (None if limit is None else 3)
                    now[0] = 3
        assert [row.timeout for row in rows if row.name == 'Untimed'] == [None, None]
        assert rows[-2].elapsed == 1 and rows[-1].elapsed == 3
    asyncio.run(run())


@pytest.mark.parametrize('limit', [0, -1, True, '3', float('inf'), float('nan')])
def test_invalid_declared_limits_do_not_enter_scope(limit):
    async def run():
        with pytest.raises(ValueError):
            async with p.report_timeout('Invalid', limit):
                pytest.fail('Invalid timeout entered')
        async with p.report_timeout('Valid', 1) as scope:
            assert scope.parent_id is None
    asyncio.run(run())


@pytest.mark.parametrize('parent_limit,child_limit', [(.04, 10), (10, .04)])
def test_enforced_scope_expires_and_drains_before_independent_scope(parent_limit, child_limit):
    async def run():
        rows, cleanup = [], []
        async def work():
            # An observing parent cannot mask a broken child enforcer.
            async with p.report_timeout('Parent', parent_limit):
                async with p.component_timeout('Child', child_limit) as child:
                    assert 0 < child.timeout <= min(parent_limit, child_limit)
                    try:
                        await asyncio.sleep(60)
                    finally:
                        cleanup.append('child stopped')
        with p.progress_sink(rows.append):
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(work(), 1)
            assert asyncio.get_running_loop().time() - started < .7
            async with p.component_timeout('Independent', None) as independent:
                assert independent.timeout is None and independent.remaining is None
                await asyncio.sleep(0)
        assert cleanup == ['child stopped']
        assert [row.name for row in rows if not row.active] == ['Child', 'Parent', 'Independent']
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_parallel_children_do_not_inherit_sibling_deadlines():
    async def run():
        ready = asyncio.Event()
        async def child(name, timeout):
            async with p.report_timeout(name, timeout) as scope:
                ready.set()
                await asyncio.sleep(0)
                return scope
        async with p.report_timeout('Parent', 10) as parent:
            first = asyncio.create_task(child('Short', 1))
            await ready.wait()
            second = asyncio.create_task(child('Long', 8))
            a, b = await asyncio.gather(first, second)
            assert a.parent_id == b.parent_id == parent.id
            assert a.timeout == 1 and b.timeout == 8
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_exhausted_ancestor_prevents_enforced_child_body(monkeypatch):
    async def run():
        now = [0.0]
        monkeypatch.setattr(p, '_clock', lambda: now[0])
        rows = []
        with p.progress_sink(rows.append):
            async with p.report_timeout('Parent', 1):
                now[0] = 2
                with pytest.raises(TimeoutError, match='already expired'):
                    async with p.component_timeout('Child', 10):
                        pytest.fail('Executed after parent deadline')
        child = [row for row in rows if row.name == 'Child']
        assert len(child) == 2 and all(row.timeout == 0 for row in child)
        assert child[0].active and not child[-1].active
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_display_leaf_clamped_budget_or_nearest_timed_ancestor():
    root = p.ComponentProgress('root', 'Test', 60, 50, True)
    group = p.ComponentProgress('group', 'Untimed group', None, 25, True, parent_id='root')
    node = p.ComponentProgress('node', 'Node', 10, 2, True, parent_id='group')
    assert component_timing((root, group, node)) == '2s elapsed; timeout 10s — Node'
    untimed = replace(node, timeout=None)
    assert component_timing((root, group, untimed)) == '50s elapsed; timeout 1m — Test'
    assert component_timing((replace(root, timeout=None), group, untimed)) == '2s elapsed — Node'
    assert component_timing((untimed,)) == '2s elapsed — Node'
    assert component_timing(()) == ''
    second = replace(untimed, id='second', name='Other node')
    assert component_timing((root, group, untimed, second)) == '50s elapsed; timeout 1m — Test'
    cycle = replace(root, timeout=None, parent_id='group')
    assert component_timing((cycle, group)) == '50s elapsed — Test; 25s elapsed — Untimed group'


@pytest.mark.parametrize('parent_id', ['', 2, 'x' * 101, 'self'])
def test_invalid_parent_identity_rejected(parent_id):
    with pytest.raises(ValueError):
        p.ComponentProgress('self', 'Name', 1, 0, True, parent_id=parent_id)
