"""Live Allure actions and notifications remain owned by the results operation."""
import asyncio

import pytest
from textual.widgets._footer import FooterKey
from textual.widgets._toast import Toast

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


@pytest.mark.parametrize('generation_finishes_last', [False, True], ids=['toast-before-completion', 'preview-finishes-after-completion'])
def test_final_results_keep_allure_toast_and_its_exact_link(tmp_path, monkeypatch, interactions, generation_finishes_last):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class PausedViewer(Viewer):
            async def show_preview(self, current, result_file):
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
                app.unit_event(RunnerEvent('test_finished', identity=two, status='passed', duration=2))
                app.unit_result = IntegrationRunResult(0, (TestResult(one, 'passed', 1), TestResult(two, 'passed', 2)), '')
                app.test_run.finish()
                app.show_unit_results()
                if generation_finishes_last:
                    release.set()
                    await request
                await pilot.pause()
                toast = app.screen.query_one(Toast)
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


@pytest.mark.parametrize('owned,expected', [
    ('browser', 'End browser inspection and return'),
    ('allure', 'Close Allure and return'),
    ('both', 'End browser inspection and close Allure and return'),
])
@pytest.mark.parametrize('accepted', [False, True], ids=['cancel', 'confirm'])
def test_leave_results_names_owned_resources_once_and_cleans_only_on_confirmation(tmp_path, owned, expected, accepted):
    async def scenario():
        app = fixture(tmp_path)
        viewer, browser = Viewer(), handle()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_result = IntegrationRunResult(0, (), '')
            if owned != 'allure':
                app.inspections = InspectionGroup((browser,))
            if owned != 'browser':
                app.allure_viewer = viewer
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
            assert browser.closed == (accepted and owned != 'allure')
            assert viewer.closed == (accepted and owned != 'browser')
    asyncio.run(scenario())
