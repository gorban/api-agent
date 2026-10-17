"""Result shortcuts update at teardown without a focus/key event."""
import asyncio
import threading
from time import monotonic
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


@pytest.mark.parametrize('mode,status,startup_delay', [
    (mode,status,0) for mode in ('unit','integration','e2e')
    for status in ('passed','failed','skipped')
] + [('integration','passed',.3)])
def test_completion_immediately_presents_back_to_selection(tmp_path, mode, status, startup_delay):
    identity = TestIdentity('base::t.py::test_one', 'base', 't.py', 't.py::test_one')
    started, release, startup = threading.Event(), threading.Event(), threading.Event()

    def runner(*args, **kwargs):
        assert startup.wait(10), 'Fixture startup barrier was not released'
        started.set()
        assert release.wait(20), 'Fixture runner was not released'
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
                await wait_until(pilot, lambda: bool(app.screen_stack) and getattr(app.screen,'kind',None) == 'tests')
                asyncio.get_running_loop().call_later(startup_delay,startup.set)
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
                await wait_until(pilot, started.is_set)
                assert app.test_run_active()
                screen = app.screen
                table = screen.query_one(DataTable)
                assert app.focused is table
                assert not any(key.key == 'b' for key in screen.query(FooterKey))
                release.set()
                async with asyncio.timeout(10):
                    await app.test_run.stopped.wait()
                await wait_until(pilot, lambda: any(key.key == 'b' for key in screen.query(FooterKey)))
                assert app.screen is screen and app.focused is table
                assert app.unit_result.tests[0].status == status
                assert not app.test_run_active()
                back = [key for key in screen.query(FooterKey) if key.key == 'b']
                assert len(back) == 1 and back[0].description == 'Back to selection'
                assert not back[0].has_class('-disabled')
                await pilot.press('b')
                await wait_until(pilot, lambda: bool(app.screen_stack) and getattr(app.screen,'kind',None) == 'tests')
                assert app.selection.selected_ids == {identity.id}
            finally:
                startup.set()
                release.set()
                if app.test_run_active():
                    async with asyncio.timeout(10):
                        await app.test_run.stopped.wait()
    asyncio.run(scenario())


async def wait_until(pilot, condition):
    """Wait for rendered fixture state, without a key/focus event or expiry race."""
    deadline=monotonic()+10
    while not condition():
        assert monotonic()<deadline, 'Expected fixture state was not reached'
        await pilot.pause(.02)
