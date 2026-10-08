"""Shared test presentation works for each test mode without live execution."""
import asyncio
import pytest
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.tui.test_results import progress_summary, ordered_result_rows


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_shared_progress_and_stable_failure_order(mode):
    rows = {'pass1': ('PASSED', 'pass1', '1s'),
            'fail1': ('FAILED', 'fail1', '2s'),
            'skip1': ('SKIPPED', 'skip1', '0s'),
            'pass2': ('PASSED', 'pass2', '3s'),
            'skip2': ('SKIPPED', 'skip2', '0s'),
            'fail2': ('ERROR', 'fail2', '4s')}
    assert progress_summary(mode, rows).plain.startswith(f'Running selected {mode} tests… 2 passed / 2 failed')
    assert '2 skipped' in progress_summary(mode, rows).plain
    assert [key for key, _ in ordered_result_rows(rows, completed=False)] == list(rows)
    assert [key for key, _ in ordered_result_rows(rows, completed=True)] == ['fail1', 'fail2', 'skip1', 'skip2', 'pass1', 'pass2']


def test_stop_handshake_waits_until_backend_finishes():
    async def scenario():
        run = TestRunLifecycle()
        request = asyncio.create_task(run.stop_and_wait())
        await asyncio.sleep(0)
        assert run.cancel_event.is_set()
        assert run.active and not request.done()
        run.finish()
        await request
        assert not run.active
    asyncio.run(scenario())
