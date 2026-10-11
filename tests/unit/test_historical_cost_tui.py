"""Cached historical UI fixtures use no inference, authentication or storage."""
import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.tui.app import SettingHelpDialog
from api_agent.tui.cost_history import cost_cell, total_cell, selected_summary
from api_agent.tui.search import SearchInput
from api_agent.tui.test_table import TestTable
from test_test_table import identities
from test_tui import fixture


class Snapshot:
    def __init__(self, value=1, incomplete=False, known_zero=False):
        self.value, self.incomplete, self.known_zero = value, incomplete, known_zero
        self.seen = []

    def total(self, ids):
        self.seen.append(tuple(ids))
        return {'estimated_cost_usd': self.value, 'incomplete': self.incomplete,
                'known_zero': self.known_zero}

    def synchronize(self):
        return self


@pytest.mark.parametrize('record,expected', [({}, '—'), ({'known_zero':True}, ''),
    ({'estimated_cost_usd':0, 'incomplete':True}, '—'),
    ({'estimated_cost_usd':0}, '0.000000'), ({'estimated_cost_usd':.0000001}, '<0.000001')])
def test_cost_cells(record, expected):
    assert cost_cell(record) == expected


def test_totals():
    snapshot = Snapshot(1, True)
    assert total_cell(snapshot, ['a','a']) == '—'
    assert snapshot.seen[-1] == ('a',)
    assert selected_summary(snapshot, ['a']) == 'Selected estimate: not enough historical data'
    assert total_cell(Snapshot(None, True), ['a']) == '—'
    assert 'no inference calls' in selected_summary(Snapshot(None, known_zero=True), [])
    assert selected_summary(Snapshot(None), []) == 'Selected estimate: not enough historical data'
    assert '1.000000' in selected_summary(Snapshot(), [])
    assert selected_summary(Snapshot(.0000001), ['a']) == 'Selected estimate: < $0.000001'


def test_known_zero_plus_unknown_is_not_a_free_suite_estimate():
    from api_agent.telemetry.estimation import EstimateSnapshot
    snapshot = EstimateSnapshot({'no_llm':{'estimated_cost_usd':0, 'known_zero':True},
                                 'llm':{'estimated_cost_usd':None}})
    assert total_cell(snapshot, ['no_llm', 'llm']) == '—'
    assert selected_summary(snapshot, ['no_llm', 'llm']) == 'Selected estimate: not enough historical data'
    assert total_cell(snapshot, ['no_llm']) == ''
    assert selected_summary(snapshot, ['no_llm']) == 'Selected estimate: no inference calls'
    assert total_cell(snapshot, ['llm']) == '—'



@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_cached_picker_refresh_preserves_search_range_without_cost_popup(tmp_path, monkeypatch, mode):
    async def scenario():
        gate = threading.Event()
        snapshot = Snapshot(1, True)
        refreshed = Snapshot(2)
        snapshot.synchronize = lambda: (gate.wait(5), refreshed)[1]
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: snapshot))
        tests = identities(4)
        app = fixture(tmp_path, mode, collector=lambda *a: tests)
        try:
            async with app.run_test(size=(90, 25)) as pilot:
                await pilot.pause()
                table = app.screen.query_one(TestTable)
                assert len(table.columns) == 5
                assert str(table.get_row_at(0)[4]) == '—'
                assert 'Selected estimate: not enough historical data' in str(app.screen.query_one('#body', Static).render())
                await pilot.press('shift+down')
                selected = table.selected_row_keys
                picker = app.screen
                assert 'i' not in picker._bindings.key_to_bindings
                await pilot.press('i')
                assert app.screen is picker
                await pilot.press('slash', 'i')
                control = app.screen.query_one(SearchInput)
                assert control.value == 'i'
                control.value = 'test_0[01]'
                await pilot.pause()
                control.cursor_position = len(control.value)
                table.focus()
                await pilot.press('i')
                assert control.value == 'test_0[01]i'
                control.value = 'test_0[01]'
                await pilot.pause()
                table.focus()
                await pilot.press('shift+down')
                keys = table.selected_row_keys
                cursor = table.cursor_coordinate
                selected_ids = app.selection.selected_ids.copy()
                gate.set()
                for _ in range(30):
                    await pilot.pause(.03)
                    if app.cost_estimator is refreshed:
                        break
                assert app.cost_estimator is refreshed
                assert app.focused is table and table.cursor_coordinate == cursor
                assert table.selected_row_keys == keys
                assert app.selection.selected_ids == selected_ids
                assert control.value == 'test_0[01]'
                assert all(str(table.get_row_at(i)[4]) == '2.000000' for i in range(table.row_count))
                assert selected
        finally:
            gate.set()
    asyncio.run(scenario())


def test_unit_picker_skips_history(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: pytest.fail('unit history')))
        app = fixture(tmp_path, 'unit', collector=lambda *a: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.cost_estimator is None
            app.refresh_cost_summary()
            assert len(app.screen.query_one(TestTable).columns) == 4
    asyncio.run(scenario())


def test_history_failure_and_stale_refresh_are_nonblocking(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: (_ for _ in ()).throw(OSError('cache'))))
        app = fixture(tmp_path, 'integration', collector=lambda *a: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'tests' and app.cost_estimator is None
            failed = Snapshot()
            failed.synchronize = lambda: (_ for _ in ()).throw(OSError('storage'))
            await app.synchronize_cost_history(failed, app.selection)
            stale = Snapshot()
            await app.synchronize_cost_history(stale, app.selection)
            assert app.cost_estimator is None
            app.cost_estimator = stale
            stale.synchronize = lambda: None
            await app.synchronize_cost_history(stale, app.selection)
            assert app.cost_estimator is stale
            assert app.historical_runner_options(lambda *a: None) == {}
            assert app.historical_runner_options(lambda *a, **kw: None) == {'historical_estimator':stale}
            assert app.historical_runner_options(lambda *, historical_estimator: None) == {'historical_estimator':stale}
    asyncio.run(scenario())


def test_costs_hidden_live_and_completed_without_cost_popup(tmp_path, monkeypatch):
    async def scenario():
        snapshot = Snapshot()
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: snapshot))
        tests = identities(2)
        app = fixture(tmp_path, 'integration', collector=lambda *a: tests)
        async with app.run_test(size=(60, 25)) as pilot:
            await pilot.pause()
            app.unit_rows = {test.id: ('PASSED', test.id, '1s') for test in tests}
            app.show_unit_results('Running')
            await pilot.pause()
            assert len(app.screen.query_one(TestTable).columns) == 3
            assert 'i' not in app.screen._bindings.key_to_bindings
            live = app.screen
            await pilot.press('i')
            assert app.screen is live
            app.unit_result = SimpleNamespace(tests=(), cancelled=False)
            app.test_costs = {tests[0].id: {'known_zero':True}, tests[1].id: {
                'estimated_cost_usd':.012, 'raw_quota_cost_usd':0, 'list_cost_usd':.02,
                'basis':'stabilized_history'}}
            app.show_unit_results('Done')
            await pilot.pause()
            table = app.screen.query_one(TestTable)
            assert len(table.columns) == 4
            assert str(table.get_row_at(0)[3]) == ''
            assert str(table.get_row_at(1)[3]) == '0.012000'
            table.move_cursor(row=1)
            completed = app.screen
            assert 'i' not in completed._bindings.key_to_bindings
            await pilot.press('i')
            assert app.screen is completed
            assert app.test_costs[tests[1].id]['basis'] == 'stabilized_history'
            assert app.test_costs[tests[1].id]['raw_quota_cost_usd'] == 0
    asyncio.run(scenario())


class PerLeafSnapshot(Snapshot):
    """Deliberately count each raw ID so deduplication mutants change totals."""
    def total(self, ids):
        ids = tuple(ids)
        self.seen.append(ids)
        return {'estimated_cost_usd': sum(self.value.get(identity, 0) for identity in ids),
                'incomplete':False}


def test_deduplicated_per_leaf_totals():
    snapshot = PerLeafSnapshot({'a':1, 'b':2})
    assert total_cell(snapshot, ['b','a','a']) == '3.000000'
    assert len(snapshot.seen[-1]) == 2
    assert selected_summary(snapshot, ['b','a','a']) == 'Selected estimate: $3.000000'
    assert len(snapshot.seen[-1]) == 2


def test_search_toggle_summary_and_exit_restore_cost_cells(tmp_path, monkeypatch):
    async def scenario():
        tests = identities(4)
        snapshot = PerLeafSnapshot({test.id:1 for test in tests})
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: snapshot))
        app = fixture(tmp_path, 'integration', collector=lambda *a: tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('slash')
            app.screen.query_one(SearchInput).value = 'test_00'
            await pilot.pause()
            assert '$4.000000' in str(app.screen.query_one('#body', Static).render())
            await pilot.press('enter')
            assert len(app.selection.selected_ids) == 3
            assert '$3.000000' in str(app.screen.query_one('#body', Static).render())
            await pilot.press('escape')
            table = app.screen.query_one(TestTable)
            rows = {row.id:row for row in app.selection.groups()}
            for row in table.ordered_rows:
                assert str(table.get_row(row.key)[4]) == f'{len(rows[row.key.value].members):.6f}'
    asyncio.run(scenario())


def test_sync_during_modal_refreshes_after_close(tmp_path, monkeypatch):
    async def scenario():
        gate = threading.Event()
        snapshot, replacement = Snapshot(1), Snapshot(2)
        snapshot.synchronize = lambda: (gate.wait(5), replacement)[1]
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: snapshot))
        app = fixture(tmp_path, 'integration', collector=lambda *a: identities(4))
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                picker = app.screen
                table = picker.query_one(TestTable)
                await pilot.press('shift+down')
                keys, cursor = table.selected_row_keys, table.cursor_coordinate
                app.push_screen(SettingHelpDialog('Information', 'Fixture modal'))
                await pilot.pause()
                gate.set()
                for _ in range(30):
                    await pilot.pause(.03)
                    if app.cost_estimator is replacement:
                        break
                assert isinstance(app.screen, SettingHelpDialog)
                assert app.cost_estimator is replacement
                assert app._pending_cost_refresh is app.selection
                await pilot.press('escape')
                await pilot.pause()
                assert app.screen is picker and app.focused is table
                assert app._pending_cost_refresh is None
                assert table.selected_row_keys == keys and table.cursor_coordinate == cursor
                assert all(str(table.get_row_at(i)[4]) == '2.000000' for i in range(table.row_count))
        finally:
            gate.set()
    asyncio.run(scenario())


def test_refresh_finishing_after_start_cannot_replace_frozen_snapshot(tmp_path, monkeypatch):
    async def scenario():
        from api_agent.testing.lifecycle import TestRunLifecycle
        gate = threading.Event()
        snapshot, replacement = Snapshot(1), Snapshot(2)
        snapshot.synchronize = lambda: (gate.wait(5), replacement)[1]
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *a: snapshot))
        app = fixture(tmp_path, 'integration', collector=lambda *a: identities(2))
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                app.test_run = TestRunLifecycle()
                gate.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.cost_estimator is snapshot
                assert getattr(app, '_pending_cost_refresh', None) is None
                app.test_run.finish()
        finally:
            gate.set()
    asyncio.run(scenario())


def test_background_history_cannot_replace_completed_run_snapshot(tmp_path, monkeypatch):
    async def scenario():
        from api_agent.testing.lifecycle import TestRunLifecycle
        gate = threading.Event()
        original, replacement = Snapshot(1), Snapshot(2)
        original.synchronize = lambda: (gate.wait(5), replacement)[1]
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history', SimpleNamespace(estimator_for=lambda *a: original))
        tests = identities(2)
        app = fixture(tmp_path, 'integration', collector=lambda *a: tests)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                app.test_run = TestRunLifecycle()
                app.unit_rows = {test.id: ('PASSED', test.id, '1s') for test in tests}
                app.show_unit_results('Running')
                await pilot.pause()
                app.unit_result = SimpleNamespace(tests=(), cancelled=False)
                app.test_costs = {test.id: {'estimated_cost_usd':1, 'basis':'frozen at Start'} for test in tests}
                app.test_run.finish()
                app.show_unit_results('Done')
                await pilot.pause()
                gate.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.cost_estimator is original
                assert all(row['basis'] == 'frozen at Start' for row in app.test_costs.values())
        finally:
            gate.set()
    asyncio.run(scenario())


def test_back_to_selection_immediately_reads_new_local_history(tmp_path, monkeypatch):
    from api_agent.telemetry.history import HistoryCache
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from test_historical_cache import fixture as history_fixture
    cache = HistoryCache(tmp_path / 'history.sqlite3')
    counts = []
    def estimator(*args):
        count = len(cache.records('local'))
        counts.append(count)
        return Snapshot(1 + count)
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=estimator))
    tests = identities(2)
    def runner(*args, **kwargs):
        manifest, payload = history_fixture()
        cache.ingest('local', manifest, payload)
        result = IntegrationRunResult(0, tuple(TestResult(test, 'passed', .1) for test in tests), '')
        return SimpleNamespace(execution=result, persisted=True, exit_code=0)
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: tests)
        app.integration_runner = runner
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert counts == [0]
            assert str(app.screen.query_one(TestTable).get_row_at(0)[4]) == '1.000000'
            await pilot.press('s')
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.kind == 'test-results' and app.screen.test_run_complete
            assert len(cache.records('local')) == 1
            await pilot.press('b')
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert counts == [0, 1]
            assert str(app.screen.query_one(TestTable).get_row_at(0)[4]) == '2.000000'
            assert app.selection.selected_ids == {test.id for test in tests}
    try:
        asyncio.run(scenario())
    finally:
        cache.close()
