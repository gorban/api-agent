"""Result shortcuts update at teardown without a focus/key event."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable
from textual.widgets._footer import FooterKey

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.runner import UnitRunResult
from test_tui import fixture


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('status', ['passed', 'failed', 'skipped'])
def test_completion_immediately_presents_back_to_selection(tmp_path, mode, status):
    identity = TestIdentity('base::t.py::test_one', 'base', 't.py', 't.py::test_one')
    started, release = threading.Event(), threading.Event()

    def runner(*args, **kwargs):
        started.set()
        assert release.wait(5)
        tests = (TestResult(identity, status, .1, 'synthetic reason' if status != 'passed' else ''),)
        code = int(status == 'failed')
        if mode == 'unit':
            return UnitRunResult(code, tests, (), '', require_full_coverage=False)
        return SimpleNamespace(execution=IntegrationRunResult(code, tests, ''),
                               persisted=True, exit_code=code)

    async def scenario():
        app = fixture(tmp_path, mode, collector=lambda *args: [identity])
        app.unit_runner = app.integration_runner = runner
        async with app.run_test(size=(130, 40)) as pilot:
            try:
                await pilot.pause(.2)
                if mode == 'unit':
                    app.start_unit_run()
                else:
                    # E2E execution is pending; exercise its shared result path
                    # with a synthetic runner, without bypassing a real gate.
                    app.test_run = TestRunLifecycle()
                    app.collection_cancel = app.test_run.cancel_event
                    app.unit_rows = {identity.id: ('PENDING', identity.id, '')}
                    app.show_unit_results(app.unit_progress())
                    app.run_worker(app._run_integrations())
                await pilot.pause(.2)
                assert started.is_set() and app.test_run_active()
                screen = app.screen
                table = screen.query_one(DataTable)
                assert app.focused is table
                assert not any(key.key == 'b' for key in screen.query(FooterKey))
                release.set()
                await pilot.pause(.5)
                assert app.screen is screen and app.focused is table
                assert app.unit_result.tests[0].status == status
                assert not app.test_run_active()
                back = [key for key in screen.query(FooterKey) if key.key == 'b']
                assert len(back) == 1 and back[0].description == 'Back to selection'
                assert not back[0].has_class('-disabled')
                await pilot.press('b')
                await pilot.pause(.2)
                assert app.screen.kind == 'tests'
                assert app.selection.selected_ids == {identity.id}
            finally:
                release.set()
    asyncio.run(scenario())
