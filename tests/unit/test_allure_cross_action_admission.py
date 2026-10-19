"""Native Allure actions share admission before their workers start."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.testing.allure_viewer import ViewerLocation
from test_tui import fixture


@pytest.mark.parametrize('fail_refresh', [False, True])
@pytest.mark.parametrize('actions', [
    ('allure-restore-secondary', 'refresh-allure'),
    ('refresh-allure', 'allure-restore-secondary'),
    ('refresh-allure', 'refresh-allure'),
])
def test_cross_action_requests_admit_only_first_and_retry(tmp_path, monkeypatch, actions, fail_refresh):
    async def scenario():
        app = fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        attempts, archives = [], []
        async def prepare(*, for_restore=False):
            attempts.append(for_restore)
            entered.set()
            await release.wait()
        async def archive(source):
            archives.append(source)
            if fail_refresh and not source.primary and len(archives) == 1:
                raise RuntimeError('Synthetic refresh failure')
            return ViewerLocation('http://127.0.0.1:5252/archive.html')
        async def close():
            release.set()
        app.prepare_allure_reporting = prepare
        app.allure_source = lambda **kwargs: SimpleNamespace(
            storage=object(), primary=kwargs.get('primary', False))
        app.allure_viewer = SimpleNamespace(show_archive=archive, aclose=close)
        app.show_allure_location = lambda location: None
        app.notify = lambda *args, **kwargs: None
        monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda *args: 'sample')
        monkeypatch.setattr('api_agent.testing.history_restore.restore_missing_history', lambda *args, **kwargs: 2)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            resolved = app.session.result()
            monkeypatch.setattr(app.session, 'result', lambda **kwargs: replace(resolved, values={
                **resolved.values, 'ALLURE_SECONDARY_DIR': str(tmp_path),
                'ALLURE_S3_BUCKET': 'synthetic-bucket'}))
            await app.show('allure', 'Allure history', 'Recorded runs', (
                ('refresh-allure', 'Refresh history'),
                ('allure-restore-secondary', 'Restore missing secondary history')))
            await pilot.pause()
            try:
                # Do not yield: this is the interval before coroutine busy flags exist.
                await app.choose(actions[0])
                await app.choose(actions[1])
                workers = list(app.workers)
                assert len(workers) == 1
                await asyncio.wait_for(entered.wait(), 2)
                await app.choose(actions[1])
                assert list(app.workers) == workers
                assert attempts == [actions[0] == 'allure-restore-secondary']
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                failed = fail_refresh and actions[0] == 'refresh-allure'
                assert len(archives) == 1 and app.operation_status == (3 if failed else 0)
                expected = 'Could not open Allure history; check reporting setup and storage.' if failed else ''
                assert str(app.screen.query_one('#error', Static).render()) == expected
                await app.choose(actions[1])
                await app.workers.wait_for_complete()
                assert attempts == [actions[0] == 'allure-restore-secondary',
                                    actions[1] == 'allure-restore-secondary']
                assert len(archives) == 2 and app.operation_status == 0
            finally:
                release.set()
                await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_cancel_before_worker_start_releases_shared_admission(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        calls = []
        async def history():
            calls.append('history')
        app.show_allure_history = history
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('refresh-allure')
            worker, = list(app.workers)
            worker.cancel()
            await pilot.pause()
            assert worker.is_cancelled and calls == []
            await app.choose('refresh-allure')
            await app.workers.wait_for_complete()
            assert calls == ['history']
    asyncio.run(scenario())


def test_worker_scheduling_failure_does_not_reserve_admission(tmp_path, monkeypatch):
    app = fixture(tmp_path)
    async def operation():
        pytest.fail('Scheduling must not invoke the opener')
    def unavailable(*args, **kwargs):
        raise RuntimeError('Synthetic worker scheduling failure')
    monkeypatch.setattr(app, 'run_worker', unavailable)
    with pytest.raises(RuntimeError, match='Synthetic worker scheduling failure'):
        app.request_allure(operation)
    assert app._allure_worker is None and not app._allure_opening


def test_stopped_admission_does_not_schedule_or_create_coroutines(tmp_path, monkeypatch):
    app = fixture(tmp_path)
    app._allure_accepting = False
    async def operation():
        pytest.fail('Stopped admission must not invoke the opener')
    monkeypatch.setattr(app, 'run_worker', lambda *args, **kwargs: pytest.fail('Stopped admission scheduled work'))
    app.request_allure(operation)
    assert app._allure_worker is None


def test_cancelled_active_restore_keeps_admission_until_thread_drains(tmp_path, monkeypatch):
    from threading import Event
    async def scenario():
        app = fixture(tmp_path)
        entered, cancelled, release, finished = Event(), Event(), Event(), Event()
        calls = []
        async def prepare(**kwargs):
            pass
        async def refresh():
            calls.append('refresh')
        def restore(*args, cancelled, **kwargs):
            entered.set()
            while not cancelled():
                assert not release.wait(.01)
            # Acknowledge cancellation while retaining the owned storage call.
            cancellation_seen.set()
            assert release.wait(5)
            finished.set()
            return 0
        cancellation_seen = cancelled
        app.prepare_allure_reporting = prepare
        app.show_allure_history = refresh
        app.allure_source = lambda **kwargs: SimpleNamespace(storage=object())
        app.notify = lambda *args, **kwargs: None
        monkeypatch.setattr('api_agent.testing.collection.repository_label', lambda *args: 'sample')
        monkeypatch.setattr('api_agent.testing.history_restore.restore_missing_history', restore)
        async with app.run_test() as pilot:
            await pilot.pause()
            resolved = app.session.result()
            monkeypatch.setattr(app.session, 'result', lambda **kwargs: replace(resolved, values={
                **resolved.values, 'ALLURE_SECONDARY_DIR': str(tmp_path),
                'ALLURE_S3_BUCKET': 'synthetic-bucket'}))
            await app.show('allure', 'Allure history', 'Recorded runs', (
                ('refresh-allure', 'Refresh history'),
                ('allure-restore-secondary', 'Restore missing secondary history')))
            await pilot.pause()
            await app.choose('allure-restore-secondary')
            worker = app._allure_worker
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                worker.cancel()
                assert await asyncio.to_thread(cancelled.wait, 2)
                await app.choose('refresh-allure')
                assert app._allure_worker is worker and calls == []
                assert app._allure_opening and not finished.is_set()
                release.set()
                from textual.worker import WorkerCancelled
                with pytest.raises(WorkerCancelled):
                    await worker.wait()
                assert finished.is_set() and not app._allure_opening
                await app.choose('refresh-allure')
                await app.workers.wait_for_complete()
                assert calls == ['refresh']
            finally:
                release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('entry', ['menu', 'selected-shortcut'])
def test_other_ui_openers_share_queued_restore_gate(tmp_path, monkeypatch, entry):
    async def scenario():
        app = fixture(tmp_path)
        calls = []
        async def restore():
            calls.append('restore')
        async def history():
            calls.append('history')
        async def selected():
            calls.append('selected')
        app.restore_allure_secondary_history = restore
        app.show_allure_history = history
        app.open_selected_allure = selected
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'allure'
            monkeypatch.setattr(app.session, 'set_value', lambda *args, **kwargs: None)
            monkeypatch.setattr(app.session, 'result', lambda **kwargs: SimpleNamespace(errors=(), missing=()))
            await app.choose('allure-restore-secondary')
            if entry == 'menu':
                await app.configure_operation()
            else:
                await app.screen.action_view_allure()
            assert len(list(app.workers)) == 1
            await app.workers.wait_for_complete()
            assert calls == ['restore']
            if entry == 'menu':
                await app.configure_operation()
            else:
                await app.screen.action_view_allure()
            await app.workers.wait_for_complete()
            assert calls == ['restore', 'history' if entry == 'menu' else 'selected']
    asyncio.run(scenario())
