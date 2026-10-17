"""Live Allure actions and notifications remain owned by the results operation."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets._footer import FooterKey
from textual.widgets._toast import Toast

from api_agent.providers.control import CleanupPendingError
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.inspection_controls import InspectionGroup
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.allure_viewer import ViewerLocation
from test_allure_links import REPORT, CDP, interactions, link_cells
from test_allure_navigation import Viewer, snapshot
from test_inspection_controls import handle
from test_tui import fixture


def prepare_live_results(app):
    one = TestIdentity('one', 'base', 'test.py', 'test.py::one')
    two = TestIdentity('two', 'base', 'test.py', 'test.py::two')
    app.mode = 'integration'
    app.unit_rows = {'one': ('RUNNING', 'one', '1s'), 'two': ('RUNNING', 'two', '1s')}
    app.show_unit_results(app.unit_progress())
    return one, two


def footer_actions(app):
    return {key.action for key in app.screen.query(FooterKey)}


@pytest.mark.parametrize('snapshot_first', [True, False], ids=['snapshot-before-outcome', 'outcome-before-snapshot'])
def test_first_completed_test_refreshes_footer_without_focus_change(tmp_path, snapshot_first):
    async def scenario():
        app = fixture(tmp_path)
        viewer = Viewer()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            one, _ = prepare_live_results(app)
            app.allure_viewer = viewer
            await pilot.pause()
            assert 'view_allure' not in footer_actions(app)
            if snapshot_first:
                app.receive_allure_snapshot(snapshot(), app._inspection_generation)
                await pilot.pause()
                assert 'view_allure' not in footer_actions(app)
            app.unit_event(RunnerEvent('test_finished', identity=one, status='passed', duration=1))
            if not snapshot_first:
                app.receive_allure_snapshot(snapshot(), app._inspection_generation)
            await pilot.pause()
            assert 'view_allure' in footer_actions(app)
            await pilot.press('a')
            await pilot.pause()
            assert len(viewer.calls) == 1 and viewer.calls[0][1] == 'one-result.json'
            assert app.unit_rows['two'][0] == 'RUNNING'
            assert 'end_inspection' not in footer_actions(app)
    asyncio.run(scenario())


@pytest.mark.parametrize('confirming', [False, True], ids=['results-visible', 'confirmation-open'])
@pytest.mark.parametrize('generation_finishes_last', [False, True], ids=['toast-before-completion', 'preview-finishes-after-completion'])
def test_final_results_keep_allure_toast_and_its_exact_link(tmp_path, monkeypatch, interactions, generation_finishes_last, confirming):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class PausedViewer(Viewer):
            async def show_preview(self, current, result_file, *, source=None):
                self.calls.append((current, result_file))
                entered.set()
                await release.wait()
                return ViewerLocation(REPORT, CDP)
        viewer = PausedViewer()
        monkeypatch.setattr(app, 'open_url', lambda url: interactions.append(('open', url)))
        async with app.run_test(size=(100, 45), notifications=True) as pilot:
            await pilot.pause()
            one, two = prepare_live_results(app)
            app.test_run = TestRunLifecycle()
            app.allure_viewer = viewer
            app.unit_event(RunnerEvent('test_finished', identity=one, status='passed', duration=1))
            app.receive_allure_snapshot(snapshot(), app._inspection_generation)
            await pilot.pause()
            current_screen = app.screen
            request = asyncio.create_task(app.open_selected_allure())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if not generation_finishes_last:
                    release.set()
                    await request
                    await pilot.pause()
                    notification = app.screen.query_one(Toast)._notification
                if confirming:
                    await pilot.press('c')
                    await pilot.pause()
                    await app.open_selected_allure()
                app.unit_event(RunnerEvent('test_finished', identity=two, status='passed', duration=2))
                app.unit_result = IntegrationRunResult(0, (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.test_run.finish()
                app.show_unit_results()
                if generation_finishes_last:
                    release.set()
                    await request
                await pilot.pause()
                if confirming:
                    assert app.screen is not current_screen
                    await pilot.press('escape')
                    await pilot.pause()
                toast = app.screen.query_one(Toast)
                assert REPORT in str(toast.render())
                assert app.screen is current_screen and app.screen.test_run_complete
                assert len(viewer.calls) == 1 and not viewer.closed
                if not generation_finishes_last:
                    assert toast._notification is notification
                assert interactions == []
                position, _ = link_cells(toast, REPORT)[-1]
                await pilot.click(toast, offset=position)
                await pilot.pause()
                assert interactions == [('copy', REPORT), ('open', REPORT)]
            finally:
                app.test_run.finish()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('browser_owned,allure_owned,recording_owned,expected', [
    (True, False, False, 'End browser inspection and return'),
    (False, True, False, 'Close Allure and return'),
    (False, False, True, 'Finish pending cleanup and return'),
    (True, True, False, 'End browser inspection, close Allure and return'),
    (True, False, True, 'End browser inspection, finish pending cleanup and return'),
    (False, True, True, 'Finish pending cleanup, close Allure and return'),
    (True, True, True, 'End browser inspection, finish pending cleanup, close Allure and return'),
], ids=['browser', 'allure', 'recording', 'browser-allure', 'browser-recording', 'allure-recording', 'all-owners'])
@pytest.mark.parametrize('accepted', [False, True], ids=['cancel', 'confirm'])
def test_leave_results_names_owned_resources_once_and_cleans_only_on_confirmation(tmp_path, browser_owned, allure_owned, recording_owned, expected, accepted):
    async def scenario():
        app = fixture(tmp_path)
        viewer, browser = Viewer(), handle()
        cleanup_calls = []
        async def retry():
            cleanup_calls.append('cleanup')
        recording = SimpleNamespace(pending_cleanup=CleanupPendingError((), retry))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            if browser_owned:
                app.inspections = InspectionGroup((browser,))
            if allure_owned:
                app.allure_viewer = viewer
            if recording_owned:
                app.recording_cleanup = recording
            app.show_unit_results()
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.message.startswith(expected)
            assert app.screen.message.count('literal y') == 1
            assert not viewer.closed and not browser.closed
            await pilot.press(*(('y', 'enter') if accepted else ('escape',)))
            await pilot.pause()
            assert app.screen.kind == ('menu' if accepted else 'test-results')
            assert browser.closed == (accepted and browser_owned)
            assert viewer.closed == (accepted and allure_owned)
            assert cleanup_calls == (['cleanup'] if accepted and recording_owned else [])
            assert (app.recording_cleanup is not None) == (recording_owned and not accepted)
    asyncio.run(scenario())


def test_leave_confirmation_uses_one_owner_snapshot_during_natural_browser_close(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        class ClosingInspection:
            reads = 0
            @property
            def active(self):
                self.reads += 1
                return self.reads == 1
        closing = ClosingInspection()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            app.show_unit_results()
            await pilot.pause()
            app.inspections = closing
            try:
                await app.close_view()
                await pilot.pause()
                assert app.screen.message.startswith('End browser inspection and return')
                assert closing.reads == 1
            finally:
                app.inspections = InspectionGroup()
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen.kind == 'test-results'
    asyncio.run(scenario())
