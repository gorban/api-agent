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


@pytest.mark.parametrize('accept_first_choice', [False, True], ids=['cancel-original', 'confirm-original'])
def test_runner_completion_keeps_original_confirmation_and_no_orphan_screens(tmp_path, monkeypatch, interactions, accept_first_choice):
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
            return SimpleNamespace(execution=IntegrationRunResult(0,
                (TestResult(one, 'passed', 1), TestResult(two, 'failed', 2)), '', inspections=(browser,)),
                persisted=True, exit_code=1)
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
                assert app.unit_result.tests[1].status == 'failed'
                assert not viewer.closed and not browser.closed
                await pilot.press(*(('y', 'enter') if accept_first_choice else ('escape',)))
                await pilot.pause()
                if not accept_first_choice:
                    assert app.screen is results
                    assert results.query_one(DataTable).get_row_at(0)[0] == 'FAILED'
                    assert 'Integration results: FAIL' in results.query_one('#body', Static).content.plain
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
                assert app.screen.kind == 'menu' and viewer.closed and browser.closed
                assert not any(isinstance(screen, QuitDialog) for screen in app.screen_stack)
                assert not any(isinstance(screen, WorkbenchScreen) and screen.kind == 'test-results'
                               for screen in app.screen_stack)
                assert not results.is_attached
            finally:
                release.set()
    asyncio.run(scenario())
