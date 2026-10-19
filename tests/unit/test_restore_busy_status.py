"""Restore admission and persistent progress in the native workbench."""
import asyncio
from types import SimpleNamespace
from dataclasses import replace

import pytest
from textual.color import Color
from textual.widgets import Static

from api_agent.tui.app import ButtonFooter
from api_agent.testing.allure_viewer import ViewerLocation
from test_tui import fixture


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('size', [(100, 35), (40, 12), (40, 35), (60, 20), (80, 24)])
def test_restore_rapid_actions_share_worker_and_retry_clears_error(tmp_path, monkeypatch, failure, size):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []
        async def prepare(**kwargs):
            attempts.append('prepare')
            entered.set()
            await release.wait()
            if failure and len(attempts) == 1:
                raise RuntimeError('Synthetic reporting failure')
        async def archive(source):
            return ViewerLocation('http://127.0.0.1:5252/archive.html')
        app.prepare_allure_reporting = prepare
        app.allure_source = lambda **kwargs: SimpleNamespace(storage=object())
        async def close():
            release.set()
        app.allure_viewer = SimpleNamespace(show_archive=archive, aclose=close)
        app.show_allure_location = lambda location: None
        app.notify = lambda *args, **kwargs: None
        monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda *args: 'sample')
        def restore(*args, **kwargs):
            return 2
        monkeypatch.setattr('api_agent.testing.history_restore.restore_missing_history', restore)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            resolved = app.session.result()
            monkeypatch.setattr(app.session, 'result', lambda **kwargs: replace(resolved, values={
                **resolved.values, 'ALLURE_SECONDARY_DIR': str(tmp_path),
                'ALLURE_S3_BUCKET': 'synthetic-bucket'}))
            await app.show('allure', 'Allure history', 'Recorded runs', (
                ('allure-restore-secondary', 'Restore missing secondary history'),))
            await pilot.pause()
            app.screen.query_one('#error', Static).update('Previous failure')
            # No yield between submissions: exercise the pre-worker-start window.
            await app.choose('allure-restore-secondary')
            await app.choose('allure-restore-secondary')
            workers = list(app.workers)
            assert len(workers) == 1
            await asyncio.wait_for(entered.wait(), 2)
            await pilot.pause()
            status = app.screen.query_one('#allure-loading', Static)
            assert status.display and str(status.render()) == 'Checking finalized secondary history…'
            assert status.styles.color == Color.parse('darkorange')
            footer = app.screen.query_one(ButtonFooter)
            assert 0 <= status.region.y < status.region.bottom <= footer.region.y < size[1]
            assert str(app.screen.query_one('#error', Static).render()) == ''
            await app.choose('allure-restore-secondary')
            assert list(app.workers) == workers
            app._restore_allure_progress(app._allure_generation, 1, 2)
            assert str(status.render()) == 'Restoring secondary history: 1/2 finalized runs'
            await pilot.pause()
            assert status.region.bottom <= footer.region.y
            release.set()
            await app.workers.wait_for_complete()
            assert attempts == ['prepare']
            assert app.operation_status == (3 if failure else 0)
            expected_error = 'Could not restore Allure history; check reporting setup and storage.' if failure else ''
            assert str(app.screen.query_one('#error', Static).render()) == expected_error
            assert not status.display and str(status.render()) == ''
            entered.clear()
            await app.choose('allure-restore-secondary')
            await app.workers.wait_for_complete()
            assert attempts == ['prepare', 'prepare'] and app.operation_status == 0
            assert str(app.screen.query_one('#error', Static).render()) == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('leave', ['close', 'quit'])
def test_restore_leave_drains_thread_before_viewer_disposal(tmp_path, monkeypatch, leave):
    from threading import Event
    async def scenario():
        app = fixture(tmp_path)
        entered, stopped, release, finished = Event(), Event(), Event(), Event()
        events = []
        async def prepare(**kwargs):
            pass
        async def close():
            assert finished.is_set()
            events.append('disposed')
        def restore(*args, cancelled, **kwargs):
            entered.set()
            while not cancelled():
                assert not release.wait(.01)
            stopped.set()
            assert release.wait(5)
            finished.set()
            raise RuntimeError('Synthetic cancelled storage operation')
        app.prepare_allure_reporting = prepare
        app.allure_source = lambda **kwargs: SimpleNamespace(storage=object())
        app.allure_viewer = SimpleNamespace(aclose=close)
        app.exit = lambda *args, **kwargs: events.append('exit')
        app.show_menu = lambda: None
        monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda *args: 'sample')
        monkeypatch.setattr('api_agent.testing.history_restore.restore_missing_history', restore)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            resolved = app.session.result()
            monkeypatch.setattr(app.session, 'result', lambda **kwargs: replace(resolved, values={
                **resolved.values, 'ALLURE_SECONDARY_DIR': str(tmp_path),
                'ALLURE_S3_BUCKET': 'synthetic-bucket'}))
            await app.show('allure', 'Allure history', 'Recorded runs', (
                ('allure-restore-secondary', 'Restore missing secondary history'),))
            await pilot.pause()
            await app.choose('allure-restore-secondary')
            assert await asyncio.to_thread(entered.wait, 2)
            closing = asyncio.create_task(app.close_view() if leave == 'close' else app.confirm_quit())
            try:
                assert await asyncio.to_thread(stopped.wait, 2)
                assert not closing.done() and events == []
                assert not app._allure_loading
                release.set()
                await asyncio.wait_for(closing, 3)
                assert events == (['disposed'] if leave == 'close' else ['disposed', 'exit'])
                assert finished.is_set() and not app._allure_tasks
                assert not app._allure_opening and app.allure_viewer is None
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)
    asyncio.run(scenario())
