"""Completed test details remain available while a suite progresses behind them."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from textual.widgets import DataTable

from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.runner import UnitRunResult
from test_test_table import identities
from test_tui import fixture


@pytest.mark.parametrize('mode,status', [
    (mode, status) for mode in ('integration', 'e2e')
    for status in ('passed', 'failed', 'error', 'skipped')
] + [('unit', 'failed')])
@pytest.mark.parametrize('finish_while_reading', [False, True])
def test_completed_details_return_to_latest_results(tmp_path, monkeypatch, mode, status, finish_while_reading):
    async def scenario():
        app = fixture(tmp_path)
        copied = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await pilot.pause()
                first, second = identities(2)
                app.mode = mode
                app.test_run = TestRunLifecycle()
                app.collection_cancel = app.test_run.cancel_event
                app.unit_rows = {item.id: ('PENDING', item.id, '') for item in (first, second)}
                app.show_unit_results(app.unit_progress())
                await pilot.pause()
                app.unit_event(RunnerEvent('test_started', identity=first))
                app.unit_event(RunnerEvent('test_finished', identity=first, status=status,
                                           duration=1.5, traceback='Synthetic reason' if status != 'passed' else ''))
                app.unit_event(RunnerEvent('test_started', identity=second))
                table = app.screen.query_one(DataTable)
                table.focus()
                await pilot.press('enter')
                await pilot.pause()
                details = app.screen
                assert details.kind == 'test-failure'
                assert first.id in details.body.plain
                assert 'Execution time: 1.500s' in details.body.plain
                assert 'Accounting is pending' in details.body.plain
                assert 'Tokens this run:' not in details.body.plain
                assert app.test_run_active() and not app.collection_cancel.is_set()
                if mode != 'unit':
                    assert app.selected_allure_key(details) == first.id
                await pilot.press('ctrl+c')
                copied.assert_awaited_once_with(app, first.id)
                app.unit_event(RunnerEvent('test_finished', identity=second, status='failed',
                                           duration=2, traceback='Second synthetic reason'))
                if finish_while_reading:
                    tests = (TestResult(first, status, 1.5), TestResult(second, 'failed', 2, 'Second synthetic reason'))
                    app.unit_result = (UnitRunResult(1, tests, (), '', require_full_coverage=False)
                                       if mode == 'unit' else IntegrationRunResult(1, tests, ''))
                    app.show_unit_results()
                    app.finish_test_run()
                await pilot.pause()
                assert app.screen is details
                assert app.unit_rows[second.id][0] == 'FAILED'
                if mode != 'unit':
                    assert app.selected_allure_key(details) == first.id
                await pilot.press('b')
                await pilot.pause()
                assert app.screen.kind == 'test-results'
                table = app.screen.query_one(DataTable)
                assert table.has_focus
                assert table.ordered_rows[table.cursor_row].key.value == first.id
                assert app.screen.test_run_complete == finish_while_reading
                assert not app.collection_cancel.is_set()
                if not finish_while_reading:
                    assert app.test_run_active()
                    app.test_run.finish()
            finally:
                if app.test_run is not None:
                    app.test_run.finish()
    asyncio.run(scenario())


def test_running_rows_and_superseded_attempts_do_not_open_details(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            try:
                await pilot.pause()
                item = identities(1)[0]
                app.mode = 'integration'
                app.test_run = TestRunLifecycle()
                app.collection_cancel = app.test_run.cancel_event
                app.unit_rows = {item.id: ('PENDING', item.id, '')}
                app.show_unit_results(app.unit_progress())
                await pilot.pause()
                screen = app.screen
                app.finished_test_results[item.id] = TestResult(item, 'passed', .1)
                await pilot.press('enter')
                assert app.screen is screen
                app.unit_event(RunnerEvent('test_started', identity=item))
                app.finished_test_results[item.id] = TestResult(item, 'passed', .1)
                await pilot.press('enter')
                assert app.screen is screen
                app.unit_event(RunnerEvent('test_finished', identity=item, status='passed'))
                assert item.id in app.finished_test_results
                app.unit_event(RunnerEvent('test_started', identity=item))
                assert item.id not in app.finished_test_results
                await pilot.press('enter')
                assert app.screen is screen
                app.test_run.finish()
            finally:
                if app.test_run is not None:
                    app.test_run.finish()
    asyncio.run(scenario())


def test_background_runner_finishes_and_publishes_while_details_stay_open(tmp_path):
    import threading
    from types import SimpleNamespace
    first, second = identities(2)
    first_finished, release = threading.Event(), threading.Event()
    def runner(*args, on_event, **kwargs):
        on_event(RunnerEvent('test_finished', identity=first, status='passed', duration=.1))
        on_event(RunnerEvent('test_started', identity=second))
        first_finished.set()
        assert release.wait(10), 'Background runner barrier not released'
        on_event(RunnerEvent('test_finished', identity=second, status='failed', duration=.2,
                             traceback='Synthetic background failure'))
        return SimpleNamespace(execution=IntegrationRunResult(1, (
            TestResult(first, 'passed', .1), TestResult(second, 'failed', .2, 'Synthetic background failure')), ''),
            usage={}, persisted=True, exit_code=1)

    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: (first, second))
        app.integration_runner = runner
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await pilot.pause()
                await app.workers.wait_for_complete()
                app.test_run = TestRunLifecycle()
                app.collection_cancel = app.test_run.cancel_event
                app.unit_rows = {item.id: ('PENDING', item.id, '') for item in (first, second)}
                app.show_unit_results(app.unit_progress())
                app.run_worker(app._run_integrations())
                assert await asyncio.to_thread(first_finished.wait, 5)
                await pilot.pause()
                await pilot.press('enter')
                await pilot.pause()
                details = app.screen
                assert details.kind == 'test-failure'
                release.set()
                await asyncio.wait_for(app.test_run.stopped.wait(), 5)
                await pilot.pause()
                assert app.screen is details
                assert app.unit_result.tests[1].status == 'failed'
                assert app.integration_persisted is True
                await pilot.press('b')
                await pilot.pause()
                assert app.screen.kind == 'test-results' and app.screen.test_run_complete
                assert app.screen.query_one(DataTable).get_row_at(0)[0] == 'FAILED'
                table = app.screen.query_one(DataTable)
                assert table.ordered_rows[table.cursor_row].key.value == first.id
            finally:
                release.set()
    asyncio.run(scenario())


def test_new_integration_clears_old_details_before_queued_worker_runs(tmp_path, monkeypatch):
    from unittest.mock import Mock
    async def scenario():
        item = identities(1)[0]
        app = fixture(tmp_path, 'integration', collector=lambda *args: (item,))
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await pilot.pause()
                await app.workers.wait_for_complete()
                app.finished_test_results[item.id] = TestResult(item, 'passed', .1)
                queued = []
                def hold(coroutine, **options):
                    queued.append(options)
                    coroutine.close()
                    return Mock()
                monkeypatch.setattr(app, 'run_worker', hold)
                await app.choose('confirmed-continue')
                await pilot.pause()
                assert queued and app.screen.kind == 'test-results'
                assert app.finished_test_results == {}
                assert app.unit_rows[item.id][0] == 'PENDING'
                await pilot.press('enter')
                assert app.screen.kind == 'test-results'
            finally:
                if app.test_run is not None:
                    app.test_run.finish()
    asyncio.run(scenario())


@pytest.mark.parametrize('previous,current,view_run,current_run,expected', [
    ('final', 'final', 'run', 'run', True),
    ('final', 'replacement', 'run', 'run', False),
    (None, None, 'run', 'run', True),
    (None, 'final', 'run', 'run', True),
    (None, None, 'run', 'other-run', False),
    (None, 'final', 'run', 'other-run', False),
    (None, None, None, None, False),
    (None, None, None, 'run', False),
])
def test_detail_visit_identity_survives_only_its_own_finalization(previous, current, view_run, current_run, expected):
    from types import SimpleNamespace
    from api_agent.tui.test_results import details_belong_to_run
    identities = {key: object() for key in ('final', 'replacement', 'run', 'other-run')}
    identities[None] = None
    screen = SimpleNamespace(result_return=(identities[previous], 'test', 0),
                             result_lifecycle=identities[view_run])
    assert details_belong_to_run(screen, identities[current], identities[current_run]) is expected
