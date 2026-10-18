"""Management actions and shutdown keep their exact UI-owned cleanup duties."""
import asyncio
from types import SimpleNamespace

from test_tui import fixture


def test_restore_action_has_one_owned_worker_while_busy(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        called = []
        async def restore():
            called.append('restore')
        app.restore_allure_secondary_history = restore
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('allure-restore-secondary')
            await app.workers.wait_for_complete()
            assert called == ['restore']
            app._allure_opening = True
            await app.choose('allure-restore-secondary')
            await app.workers.wait_for_complete()
            assert called == ['restore']
            app._allure_opening = False
    asyncio.run(scenario())


def test_failed_log_disposal_retains_reporting_until_shutdown_retry(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        events = []
        class Reader:
            retry = False
            async def aclose(self):
                events.append('reader')
                if not self.retry:
                    raise RuntimeError('synthetic pending log reader')
        reader = Reader()
        async def close_sessions():
            events.append('reporting')
        sessions = SimpleNamespace(aclose=close_sessions)
        app.container_log_screen = reader
        app.reporting_sessions = sessions

        assert not await app.close_reporting_sessions()
        pending = app.pending_shutdown_cleanup
        assert reader in pending.owners and sessions in pending.owners
        assert events == ['reader']
        assert app.container_log_screen is reader

        reader.retry = True
        await pending.retry()
        assert events == ['reader', 'reader', 'reporting']
        assert app.container_log_screen is None
        assert app.pending_shutdown_cleanup is None
    asyncio.run(scenario())


def test_stale_stop_mark_cannot_select_a_stopped_container(tmp_path):
    import pytest
    from api_agent.runtime.containers import Container
    app = fixture(tmp_path)
    app.containers = (Container('stopped-id', 'stopped', 'image', 'exited', False),)
    app.container_marked = {'stopped-id'}

    with pytest.raises(ValueError, match='running container'):
        app.request_container_stop()


def test_log_action_opens_the_highlighted_stopped_container(tmp_path):
    from textual.widgets import TextArea
    from api_agent.runtime.containers import Container
    from test_container_management import app_for
    async def scenario():
        app = app_for(tmp_path, [Container('a' * 64, 'stopped', 'image', 'exited', False)],
                      loader=lambda **kwargs: 'bounded original output')
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('container-logs')
            await pilot.pause()
            assert app.screen.query_one(TextArea).text == 'bounded original output'
            await app.screen.action_close()
            await pilot.pause()
            assert app.screen.kind == 'containers'
    asyncio.run(scenario())


def test_quit_outside_log_page_preserves_the_application_action(tmp_path, monkeypatch):
    from textual.app import App
    async def scenario():
        app = fixture(tmp_path)
        calls = []
        async def inherited(subject):
            calls.append(subject)
        monkeypatch.setattr(App, 'action_quit', inherited)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.action_quit()
            assert calls == [app]
    asyncio.run(scenario())
