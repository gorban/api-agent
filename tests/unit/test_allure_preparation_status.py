"""Selected-report preparation stays visible through test completion and errors."""
import asyncio

import pytest
from textual.widgets import Static
from textual.color import Color

from api_agent.testing.allure_viewer import ViewerLocation
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from test_allure_navigation import Viewer, snapshot
from test_allure_results_lifetime import prepare_live_results
from test_tui import fixture


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
@pytest.mark.parametrize('failure', [False, True])
def test_selected_preparation_survives_results_refresh(tmp_path, monkeypatch, mode, failure):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        notices = []
        class PausedViewer(Viewer):
            async def show_preview(self, *args, **kwargs):
                entered.set()
                await release.wait()
                if failure:
                    raise RuntimeError('Controlled report failure')
                return ViewerLocation('http://127.0.0.1:5252/exact')
        monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
        app.allure_viewer = PausedViewer()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            one, two = prepare_live_results(app)
            app.mode = mode
            app.unit_rows['one'] = ('PASSED', 'one', '1s')
            app.receive_allure_snapshot(snapshot(), app._inspection_generation)
            await pilot.pause()
            request = asyncio.create_task(app.open_selected_allure())
            try:
                await entered.wait()
                status = app.screen.query_one('#allure-loading', Static)
                expected_mode = 'E2E' if mode == 'e2e' else 'integration'
                assert status.display and str(status.render()) == f'Preparing selected {expected_mode} run in Allure…'
                assert status.styles.color == Color.parse('darkorange')
                app.unit_rows['two'] = ('PASSED', 'two', '2s')
                app.unit_result = IntegrationRunResult(0, (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.show_unit_results()
                assert app.screen.test_run_complete
                await pilot.pause()
                assert status.display and str(status.render()).startswith('Preparing selected')
                app.toggle_row('one')
                await pilot.pause()
                assert app.screen.kind == 'test-failure'
                assert app.screen.query_one('#allure-loading', Static).display
                await app.close_view()
                await pilot.pause()
                assert app.screen.kind == 'test-results'
                status = app.screen.query_one('#allure-loading', Static)
                assert status.display
                await pilot.press('q')
                await pilot.pause()
                assert notices == []
                release.set()
                await request
                assert not status.display and str(status.render()) == ''
                await pilot.press('escape')
                await pilot.pause()
                assert app.screen.kind == 'test-results' and not app.screen.query_one('#allure-loading', Static).display
                assert notices and ('Could not open Allure history; check reporting setup and storage.' in str(notices[-1])
                                    if failure else 'Allure' in str(notices[-1]))
            finally:
                release.set()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('superseded', [False, True], ids=['cancelled', 'newer-operation'])
def test_interruption_clears_only_its_own_preparation(tmp_path, superseded):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class PausedViewer(Viewer):
            async def show_preview(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return ViewerLocation('http://127.0.0.1:5252/old')
        app.allure_viewer = PausedViewer()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            prepare_live_results(app)
            app.unit_rows['one'] = ('PASSED', 'one', '1s')
            app.receive_allure_snapshot(snapshot(), app._inspection_generation)
            await pilot.pause()
            request = asyncio.create_task(app.open_selected_allure())
            await entered.wait()
            status = app.screen.query_one('#allure-loading', Static)
            assert status.display
            if superseded:
                app._allure_generation = object()
                app._allure_loading = 'Newer selected report is loading'
                app.refresh_allure_loading()
                release.set()
                await request
                assert status.display and str(status.render()) == 'Newer selected report is loading'
            else:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert not status.display and app._allure_loading == ''
            await app.close_allure()
            assert not status.display and app._allure_loading == ''
    asyncio.run(scenario())
