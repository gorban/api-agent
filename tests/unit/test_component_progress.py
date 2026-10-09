"""Owned component clocks, worker delivery and live literal TUI presentation."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static

from api_agent.providers.progress import ComponentProgress, progress_sink, report_timeout
from api_agent.testing.contracts import RunnerEvent
from api_agent.tui.test_results import component_timing
from test_tui import fixture
from test_tui_coverage import identity


@pytest.mark.parametrize('changes', [
    {'id': ''}, {'id': 1}, {'id': 'x' * 101}, {'name': ''}, {'name': 1},
    {'name': 'x' * 101}, {'name': '\n'}, {'timeout': -1}, {'timeout': True},
    {'timeout': float('inf')}, {'elapsed': -1}, {'elapsed': float('nan')},
    {'active': 1},
])
def test_invalid_progress_is_rejected(changes):
    with pytest.raises(ValueError, match='Invalid component progress'):
        replace(ComponentProgress('id', 'Login', 180, 0, True), **changes)


def test_observer_heartbeats_restores_sink_and_drains_its_task():
    async def run():
        outer, inner = [], []
        with progress_sink(outer.append):
            async with report_timeout('Parent', 180):
                with progress_sink(inner.append):
                    async with report_timeout('Child', 15):
                        await asyncio.sleep(1.05)
                async with report_timeout('Sibling', 30):
                    pass
        assert {row.name for row in inner} == {'Child'}
        assert len(inner) >= 3 and inner[0].active and not inner[-1].active
        assert 1 <= inner[1].elapsed < 15
        assert len({row.id for row in inner}) == 1
        assert {row.name for row in outer} == {'Parent', 'Sibling'}
        for name in ('Parent', 'Sibling'):
            rows = [row for row in outer if row.name == name]
            assert rows[0].active and not rows[-1].active
        assert len(asyncio.all_tasks()) == 1
        async with report_timeout('Unobserved', 1):
            pass
        assert all(row.name != 'Unobserved' for row in outer)
    asyncio.run(run())


@pytest.mark.parametrize('end', ['failure', 'timeout', 'cancel'])
def test_end_reports_on_failure_timeout_and_cancellation(end):
    async def run():
        rows = []
        async def action():
            with progress_sink(rows.append):
                async with report_timeout('Login', 180):
                    if end == 'failure':
                        raise RuntimeError('synthetic failure')
                    if end == 'timeout':
                        async with asyncio.timeout(.01):
                            await asyncio.sleep(60)
                    await asyncio.sleep(60)
        task = asyncio.create_task(action())
        await asyncio.sleep(0)
        if end == 'cancel':
            task.cancel()
        with pytest.raises({'failure': RuntimeError, 'timeout': TimeoutError,
                            'cancel': asyncio.CancelledError}[end]):
            await task
        assert len(rows) == 2 and rows[0].active and not rows[-1].active
        assert rows[0].id == rows[-1].id
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_reporting_failure_does_not_change_operation():
    async def run():
        def fail(_):
            raise RuntimeError('unavailable display')
        with progress_sink(fail):
            async with report_timeout('Login', 3):
                pass
    asyncio.run(run())


def test_nested_component_display_and_late_events(tmp_path):
    async def run():
        app = fixture(tmp_path)
        test = identity()
        parent = ComponentProgress('p', 'GPT login', 180, 59, True)
        child = ComponentProgress('c', 'Password-manager password', 15, 2, True, parent_id='p')
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app.unit_event(RunnerEvent('component_progress', identity=test, component=parent))
            assert not app.unit_rows
            app.unit_event(RunnerEvent('test_started', identity=test))
            app.show_unit_results('Running')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert [str(column.label) for column in table.columns.values()] == ['Status', 'Test', 'Execution time']
            table.focus()
            app.unit_event(RunnerEvent('component_progress', identity=test))
            app.unit_event(RunnerEvent('component_progress', identity=test, component=parent))
            assert table.get_row_at(0)[2] == 'GPT login: 59s elapsed; timeout 3m'
            assert 'GPT login' not in app.screen.query_one('#body', Static).content.plain
            app.unit_event(RunnerEvent('component_progress', identity=test, component=child))
            assert table.get_row_at(0)[2] == 'Password-manager password: 2s elapsed; timeout 15s'
            assert table.has_focus
            app.unit_event(RunnerEvent('component_progress', identity=test, component=replace(child, timeout=None)))
            assert table.get_row_at(0)[2] == 'GPT login: 59s elapsed; timeout 3m'
            app.unit_event(RunnerEvent('component_progress', identity=test, component=replace(parent, timeout=None)))
            assert table.get_row_at(0)[2] == 'Password-manager password: 2s elapsed'
            assert 'Password-manager password' not in app.screen.query_one('#body', Static).content.plain
            app.unit_event(RunnerEvent('component_progress', identity=test, component=parent))
            app.unit_event(RunnerEvent('component_progress', identity=test, component=replace(child, active=False)))
            assert table.get_row_at(0)[2] == component_timing((parent,))
            app.unit_event(RunnerEvent('component_progress', identity=test, component=replace(parent, elapsed=120)))
            assert table.get_row_at(0)[2] == 'GPT login: 2m elapsed; timeout 3m'
            app.unit_event(RunnerEvent('component_progress', identity=test, component=replace(parent, active=False)))
            assert table.get_row_at(0)[2] == ''
            app.unit_event(RunnerEvent('test_finished', identity=test, status='failed', duration=213.375))
            app.unit_event(RunnerEvent('component_progress', identity=test, component=parent))
            assert table.get_row_at(0) == ['FAILED', test.id, '213.375s']
            assert test.id not in app.test_components
            assert 'GPT login' not in app.unit_progress().plain
            app.test_components['stale'] = {'p': parent}
            assert 'GPT login' not in app.unit_progress().plain
            app.unit_rows['stale'] = ('RUNNING', 'stale', '')
            assert 'GPT login' not in app.unit_progress().plain
    asyncio.run(run())
    assert component_timing((ComponentProgress('x', 'Step [literal]', 61, .5, True),)) == 'Step [literal]: 0s elapsed; timeout 1m 1s'
    assert component_timing((ComponentProgress('x', 'Short step', .02, .001, True),)) == 'Short step: 0s elapsed; timeout 0.02s'


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_parallel_component_clocks_stay_in_their_rows(tmp_path, mode):
    async def run():
        app = fixture(tmp_path)
        first, second = identity('first'), identity('second')
        login = ComponentProgress('login', 'GPT login', 180, 59, True)
        helper = ComponentProgress('helper', 'Password-manager username', 15, 3, True)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app.mode = mode
            for test in (first, second):
                app.unit_event(RunnerEvent('test_started', identity=test))
            app.show_unit_results('Running')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            app.unit_event(RunnerEvent('component_progress', identity=first, component=login))
            app.unit_event(RunnerEvent('component_progress', identity=second, component=helper))
            assert table.get_row(first.id)[2] == 'GPT login: 59s elapsed; timeout 3m'
            assert table.get_row(second.id)[2] == 'Password-manager username: 3s elapsed; timeout 15s'
            assert app.screen.query_one('#body', Static).content.plain == (
                f'Running selected {mode} tests… 0 passed / 0 failed / 0 skipped / 2 running / 0 pending')
            app.unit_event(RunnerEvent('test_finished', identity=first, status='passed', duration=72.125))
            assert table.get_row(first.id) == ['PASSED', first.id, '72.125s']
            assert table.get_row(second.id)[2] == 'Password-manager username: 3s elapsed; timeout 15s'
            assert app.screen.query_one('#body', Static).content.plain == (
                f'Running selected {mode} tests… 1 passed / 0 failed / 0 skipped / 1 running / 0 pending')
            app.unit_event(RunnerEvent('test_finished', identity=second, status='failed', duration=24.875))
            app.unit_event(RunnerEvent('component_progress', identity=first, component=login))
            assert table.get_row(first.id)[2] == '72.125s'
            assert table.get_row(second.id) == ['FAILED', second.id, '24.875s']
            assert not app.test_components
            assert app.screen.query_one('#body', Static).content.plain == (
                f'Running selected {mode} tests… 1 passed / 1 failed / 0 skipped / 0 running / 0 pending')
    asyncio.run(run())


def test_pytest_binding_is_optional_and_restored(tmp_path, monkeypatch):
    import json
    from api_agent.testing.progress import pytest_runtest_protocol
    async def run():
        path = tmp_path / 'events'
        monkeypatch.delenv('API_AGENT_COMPONENT_EVENTS', raising=False)
        hook = pytest_runtest_protocol(SimpleNamespace(nodeid='test.py::test_a'), None)
        next(hook)
        async with report_timeout('Unused', 15):
            pass
        with pytest.raises(StopIteration):
            next(hook)
        assert not path.exists()
        monkeypatch.setenv('API_AGENT_COMPONENT_EVENTS', str(path))
        hook = pytest_runtest_protocol(SimpleNamespace(nodeid='test.py::test_a'), None)
        next(hook)
        async with report_timeout('Bound', 15):
            pass
        with pytest.raises(StopIteration):
            next(hook)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(rows) == 2 and all(row['node'] == 'test.py::test_a' for row in rows)
        assert rows[0]['component']['active'] and not rows[1]['component']['active']
        async with report_timeout('After', 15):
            pass
        assert len(path.read_text().splitlines()) == 2
    asyncio.run(run())
