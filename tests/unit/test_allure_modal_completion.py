"""A genuine runner completion preserves the pending leave choice and its owners."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Input, Static
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
                assert dialog.message.startswith('Stop running tests, close Allure and return')
                release.set()
                await asyncio.wait_for(app.test_run.stopped.wait(), 10)
                await pilot.pause()
                assert app.screen is dialog and len(app.screen_stack) == depth
                assert results.test_run_complete and not app.test_run_active()
                assert app.unit_result.tests[1].status == final_status
                assert 'Stop running tests' not in dialog.message
                expected_leave = 'End browser inspection, close Allure' if retain_browser else 'Close Allure'
                assert dialog.message.startswith(expected_leave)
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
            async def show_archive(self, source):
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
                    assert str(page.query_one('#error', Static).content) == 'Could not open Allure history; check reporting setup and storage.'
                    assert 'Controlled history failure' not in str(page.query_one('#error', Static).content)
                    assert app.operation_status == 3
                else:
                    assert REPORT + '\n\nBrowser CDP' in str(page.query_one('#body', Static).content)
                    assert app.operation_status == 0
            finally:
                release.set()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('natural_close', [False, True], ids=['retained-browser', 'browser-closes'])
def test_active_leave_warning_tracks_resources_and_completion_without_replacing_input(tmp_path, natural_close):
    from api_agent.testing.inspection_controls import InspectionGroup
    from api_agent.testing.lifecycle import TestRunLifecycle
    from test_allure_results_lifetime import prepare_live_results
    async def scenario():
        app = fixture(tmp_path)
        viewer, browser = Viewer(), handle()
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            one, two = prepare_live_results(app)
            app.test_run = TestRunLifecycle()
            app.allure_viewer = viewer
            app.inspections = InspectionGroup((browser,))
            app.integration_exit_status = app.operation_status = 0
            await pilot.pause()
            try:
                await pilot.press('c')
                await pilot.pause()
                dialog = app.screen
                assert dialog.message.startswith('Stop running tests, end browser inspection, close Allure and return')
                answer = dialog.query_one('#quit-answer', Input)
                answer.focus()
                answer.value = 'y'
                if natural_close:
                    browser.closed = True
                    app.unit_event(RunnerEvent('inspection_closed'))
                    assert dialog.message.startswith('Stop running tests, close Allure and return')
                app.unit_result = IntegrationRunResult(0,
                    (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.unit_rows = {test.identity.id: ('PASSED', test.identity.id, '1s')
                                 for test in app.unit_result.tests}
                app.show_unit_results()
                app.finish_test_run()
                await pilot.pause()
                assert app.screen is dialog
                expected = 'Close Allure' if natural_close else 'End browser inspection, close Allure'
                assert dialog.message.startswith(expected) and 'Stop running tests' not in dialog.message
                assert str(dialog.query_one('#quit-message', Static).content) == dialog.message
                assert dialog.query_one('#quit-answer', Input) is answer
                assert answer.value == 'y' and app.focused is answer
                assert not viewer.closed
                await pilot.press('enter')
                await pilot.pause()
                assert app.screen.kind == 'menu' and viewer.closed and browser.closed
                assert app.operation_status == 0 and not app.collection_cancel.is_set()
            finally:
                app.test_run.finish()
    asyncio.run(scenario())


def test_immediate_completion_updates_leave_warning_before_dialog_mount(tmp_path):
    from api_agent.testing.lifecycle import TestRunLifecycle
    from test_allure_results_lifetime import prepare_live_results
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            one, two = prepare_live_results(app)
            app.test_run = TestRunLifecycle()
            app.allure_viewer = Viewer()
            await pilot.pause()
            try:
                await app.close_view()
                dialog = app.screen
                assert isinstance(dialog, QuitDialog) and not dialog.is_mounted
                app.unit_result = IntegrationRunResult(0,
                    (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.show_unit_results()
                app.finish_test_run()
                await pilot.pause()
                assert app.screen is dialog
                assert dialog.message.startswith('Close Allure and return')
                assert str(dialog.query_one('#quit-message', Static).content) == dialog.message
                await pilot.press('escape')
                await pilot.pause()
                assert app.screen.kind == 'test-results'
            finally:
                app.test_run.finish()
    asyncio.run(scenario())


def test_late_completion_without_a_screen_does_not_refresh_a_dialog(tmp_path):
    from api_agent.testing.lifecycle import TestRunLifecycle
    app = fixture(tmp_path)
    app.test_run = TestRunLifecycle()
    assert not app.screen_stack
    app.finish_test_run()
    assert not app.test_run_active()



def test_completion_updates_connected_warning_before_parent_mount_acknowledgement(tmp_path, monkeypatch):
    from api_agent.testing.lifecycle import TestRunLifecycle
    from test_allure_results_lifetime import prepare_live_results
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = QuitDialog._on_compose
        async def composed_but_not_mounted(dialog, event):
            await original(dialog, event)
            entered.set()
            await release.wait()
        monkeypatch.setattr(QuitDialog, '_on_compose', composed_but_not_mounted)
        app = fixture(tmp_path)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            one, two = prepare_live_results(app)
            app.test_run = TestRunLifecycle()
            app.allure_viewer = Viewer()
            await pilot.pause()
            try:
                await app.close_view()
                await asyncio.wait_for(entered.wait(), 10)
                dialog = app.screen
                assert not dialog.is_mounted
                body = dialog.query_one('#quit-message', Static)
                answer = dialog.query_one('#quit-answer', Input)
                answer.value = 'y'
                app.unit_result = IntegrationRunResult(0,
                    (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.show_unit_results()
                app.finish_test_run()
                assert dialog.message.startswith('Close Allure and return')
                assert str(body.content) == dialog.message
                assert answer.value == 'y'
            finally:
                app.test_run.finish()
                release.set()
            await pilot.pause()
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen.kind == 'test-results'
    asyncio.run(scenario())
