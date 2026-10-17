"""A genuine runner completion preserves the pending leave choice and its owners."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static
from textual.widgets._toast import Toast

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.allure_viewer import ViewerLocation
from api_agent.tui.app import QuitDialog, WorkbenchScreen
from test_allure_links import REPORT, CDP, interactions, link_cells
from test_allure_navigation import Viewer
from test_inspection_controls import handle
from test_tui import fixture


@pytest.mark.parametrize('retain_browser,final_status,exit_code', [
    (True, 'failed', 1), (False, 'failed', 1), (False, 'passed', 0),
], ids=['retained-failure', 'no-browser-failure', 'no-browser-success'])
@pytest.mark.parametrize('accept_first_choice', [False, True], ids=['cancel-original', 'confirm-original'])
def test_runner_completion_keeps_original_confirmation_and_no_orphan_screens(tmp_path, monkeypatch, interactions, accept_first_choice, retain_browser, final_status, exit_code):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        one = TestIdentity('base::test.py::one', 'base', 'test.py', 'test.py::one')
        two = TestIdentity('base::test.py::two', 'base', 'test.py', 'test.py::two')
        browser, viewer = handle(), Viewer()
        def runner(context, tests, config, *, on_event, **options):
            on_event(RunnerEvent('test_finished', identity=one, status='passed', duration=1))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise AssertionError('Fixture never released the controlled runner.')
            return SimpleNamespace(execution=IntegrationRunResult(exit_code,
                (TestResult(one, 'passed', 1), TestResult(two, final_status, 2)), '', inspections=(browser,) if retain_browser else ()),
                persisted=True, exit_code=exit_code)
        app = fixture(tmp_path, 'integration', collector=lambda *args: (one, two))
        app.integration_runner = runner
        monkeypatch.setattr(app, 'open_url', lambda url: interactions.append(('open', url)))
        async with app.run_test(size=(115, 45), notifications=True) as pilot:
            try:
                await pilot.pause()
                await pilot.press('s')
                await asyncio.wait_for(entered.wait(), 10)
                await pilot.pause()
                results = app.screen
                app.allure_viewer = viewer
                app.show_allure_location(ViewerLocation(REPORT, CDP))
                await pilot.pause()
                notification = results.query_one(Toast)._notification
                await pilot.press('c')
                await pilot.pause()
                dialog, depth = app.screen, len(app.screen_stack)
                assert isinstance(dialog, QuitDialog)
                release.set()
                await asyncio.wait_for(app.test_run.stopped.wait(), 10)
                await pilot.pause()
                assert app.screen is dialog and len(app.screen_stack) == depth
                assert results.test_run_complete and not app.test_run_active()
                assert app.unit_result.tests[1].status == final_status
                assert not viewer.closed and not browser.closed
                await pilot.press(*(('y', 'enter') if accept_first_choice else ('escape',)))
                await pilot.pause()
                if not accept_first_choice:
                    assert app.screen is results
                    assert results.query_one(DataTable).get_row_at(0)[0] == final_status.upper()
                    expected_summary = 'Integration results: ' + ('PASS' if exit_code == 0 else 'FAIL')
                    assert expected_summary in results.query_one('#body', Static).content.plain
                    toast = results.query_one(Toast)
                    assert toast._notification is notification
                    assert not viewer.closed and not browser.closed
                    position, _ = link_cells(toast, REPORT)[-1]
                    await pilot.click(toast, offset=position)
                    await pilot.pause()
                    assert interactions == [('copy', REPORT), ('open', REPORT)]
                    await pilot.press('c')
                    await pilot.pause()
                    await pilot.press('y', 'enter')
                    await pilot.pause()
                assert app.screen.kind == 'menu' and viewer.closed
                assert browser.closed == retain_browser
                assert app.operation_status == exit_code
                assert not app.collection_cancel.is_set()
                assert not any(isinstance(screen, QuitDialog) for screen in app.screen_stack)
                assert not any(isinstance(screen, WorkbenchScreen) and screen.kind == 'test-results'
                               for screen in app.screen_stack)
                assert not results.is_attached
            finally:
                release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('failed', [False, True], ids=['history-ready', 'history-error'])
def test_history_generation_updates_its_page_beneath_quit_confirmation(tmp_path, failed):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class PausedHistory(Viewer):
            async def show_history(self, source):
                self.calls.append(source)
                entered.set()
                await release.wait()
                if failed:
                    raise RuntimeError('Controlled history failure')
                return ViewerLocation(REPORT, CDP)
        viewer = PausedHistory()
        async with app.run_test(size=(115, 45)) as pilot:
            await pilot.pause()
            app.allure_viewer = viewer
            request = asyncio.create_task(app.show_allure_history())
            try:
                await asyncio.wait_for(entered.wait(), 10)
                await pilot.pause()
                page = app.screen
                await pilot.press('q')
                await pilot.pause()
                dialog, depth = app.screen, len(app.screen_stack)
                assert isinstance(dialog, QuitDialog)
                await app.open_selected_allure()
                release.set()
                await request
                await pilot.pause()
                assert app.screen is dialog and len(app.screen_stack) == depth
                assert len(viewer.calls) == 1 and not viewer.closed
                await pilot.press('escape')
                await pilot.pause()
                assert app.screen is page
                if failed:
                    assert 'Controlled history failure' in str(page.query_one('#error', Static).content)
                    assert app.operation_status == 3
                else:
                    assert REPORT + '\n\nBrowser CDP' in str(page.query_one('#body', Static).content)
                    assert app.operation_status == 0
            finally:
                release.set()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())
