"""Real navigation/viewer owners with disposable engine and thread barriers."""
import asyncio
from threading import Event
from unittest.mock import Mock

import pytest

from api_agent.runtime.tasks import offload
from api_agent.testing.allure_source import PreviewSnapshot
from api_agent.testing.allure_viewer import ManagedAllureViewer, ViewerLocation
from api_agent.tui.reporting import ReportingSessions
from test_allure_viewer import run
from test_reporting_registry import storage, tmp_path
from test_tui import fixture


class Barrier:
    def __init__(self, manager):
        self.manager = manager
        self.entered, self.proceed, self.finished = Event(), Event(), Event()
        self.leased = []

    def wait(self):
        self.leased.append(self.manager.lease is not None)
        self.entered.set()
        assert self.proceed.wait(10)
        self.leased.append(self.manager.lease is not None)
        self.finished.set()


class Browser:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class ControlledViewer(ManagedAllureViewer):
    def __init__(self, barrier, phase):
        super().__init__()
        self.barrier, self.phase = barrier, phase
        self.browser = Browser()

    async def _ready(self):
        pass

    async def _archive(self, *args):
        pass

    async def _report(self, *args, **kwargs):
        return 'http://127.0.0.1:5252/controlled'

    async def _open(self, url):
        self._browser = self.browser
        if self.phase == 'open':
            await offload(self.barrier.wait)
        return ViewerLocation(url)


@pytest.mark.parametrize('operation', ['history', 'preview'])
@pytest.mark.parametrize('phase', ['preparation', 'source', 'open'])
@pytest.mark.parametrize('leave', ['quit', 'back', 'end-inspection'])
def test_leaving_drains_owned_opening_before_reporting_release(tmp_path, storage, run, operation, phase, leave):
    manifest, files, result_file = run
    async def scenario():
        app = fixture(tmp_path, fields={
            'ALLURE_S3_BUCKET': {'type': 'str', 'default': 'allure-results'},
            'ALLURE_S3_USE_LOCALSTACK': {'type': 'bool', 'default': True}})
        manager = storage.create()
        sessions = ReportingSessions(lambda *args, **kwargs: manager)
        app.reporting_sessions = sessions
        barrier = Barrier(manager)
        unsafe_releases = []
        release = manager.release
        def checked_release(**kwargs):
            if not barrier.finished.is_set():
                unsafe_releases.append(True)
                raise RuntimeError('reporting release attempted before opener drain acknowledgement')
            return release(**kwargs)
        manager.release = checked_release
        created = []
        acquire = sessions.acquire
        def prepare(*args, **kwargs):
            lease = acquire(*args, **kwargs)
            if phase == 'preparation': barrier.wait()
            return lease
        sessions.acquire = prepare
        class Source:
            repo = manifest['repo']
            def history(self, **kwargs):
                if phase == 'source': barrier.wait()
                return [manifest]
        app.allure_source = Source
        app.selected_allure_result = lambda screen: (PreviewSnapshot(files, manifest, {}, False), result_file)
        def viewer():
            if app.allure_viewer is None:
                app.allure_viewer = ControlledViewer(barrier, phase)
                created.append(app.allure_viewer)
            return app.allure_viewer
        app._allure = viewer
        app.show_allure_location = Mock()
        app.exit = Mock()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'integration'
            await app.show('allure', 'History')
            opening = asyncio.create_task(app.show_allure_history() if operation == 'history'
                                          else app.open_selected_allure())
            assert await asyncio.to_thread(barrier.entered.wait, 5)
            if leave == 'quit': closing = asyncio.create_task(app.confirm_quit())
            elif leave == 'back': closing = asyncio.create_task(app.close_view())
            else: closing = asyncio.create_task(app.end_inspection())
            try:
                done, _ = await asyncio.wait({closing}, timeout=.05)
                assert not done, 'close acknowledged while a storage/provider owner was still draining'
                assert manager.lease is not None
                count = len(created)
                await app.show_allure_history()
                await app.open_selected_allure()
                assert len(created) == count, 'new opener admitted while closure was pending'
            finally:
                barrier.proceed.set()
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(opening, 5)
            assert barrier.leased == [True, True] and not unsafe_releases
            assert app.allure_viewer is None and not app.allure_cleanup_active()
            if phase == 'open':
                assert all(item.browser.closed and item._browser is None for item in created)
            if leave == 'quit':
                assert manager.lease is None
                await app.show_allure_history()
                await app.open_selected_allure()
                assert len(created) == count
                app.exit.assert_called_once()
            else:
                assert manager.lease is not None
                await app.show_allure_history()
                assert len(created) == count + 1
                assert await app.close_allure()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['history', 'preview'])
def test_canceled_close_exports_retry_while_opener_drains(tmp_path, storage, run, operation):
    manifest, files, result_file = run
    async def scenario():
        app = fixture(tmp_path)
        manager = storage.create()
        app.reporting_sessions = ReportingSessions(lambda *args, **kwargs: manager)
        app.reporting_sessions.acquire(manager.plan)
        barrier = Barrier(manager)
        async def prepare(): await offload(barrier.wait)
        app.prepare_allure_reporting = prepare
        app.selected_allure_result = lambda screen: (PreviewSnapshot(files, manifest, {}, False), result_file)
        async with app.run_test() as pilot:
            await pilot.pause()
            opening = asyncio.create_task(app.show_allure_history() if operation == 'history'
                                          else app.open_selected_allure())
            assert await asyncio.to_thread(barrier.entered.wait, 5)
            try:
                cleanup = app.begin_allure_cleanup()
                cleanup.timeout = .01
                waiting = asyncio.Event()
                wait_cleanup = app.wait_allure_cleanup
                async def observed(owner):
                    waiting.set()
                    return await wait_cleanup(owner)
                app.wait_allure_cleanup = observed
                closing = asyncio.create_task(app.on_unmount())
                await waiting.wait()
                closing.cancel()
                with pytest.raises(asyncio.CancelledError) as interrupted:
                    await closing
                assert interrupted.value.cleanup_pending is not None
                assert app.inspection_cleanup_active() and manager.lease is not None
                receipt = app.pending_inspection_cleanup
                shutdown = app.pending_shutdown_cleanup
                assert receipt is not None and shutdown is not None
                assert cleanup in receipt.owners
                before = opening.cancelling()
                from api_agent.providers.control import CleanupPendingError
                with pytest.raises(CleanupPendingError):
                    await shutdown.retry()
                assert app.pending_shutdown_cleanup is shutdown and manager.lease is not None
                assert opening.cancelling() == before == 1
            finally:
                barrier.proceed.set()
            await opening
            await app.pending_shutdown_cleanup.retry()
            assert manager.lease is None and app.pending_shutdown_cleanup is None
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['history', 'preview'])
def test_generation_revoked_at_preparation_completion_prevents_viewer_creation(tmp_path, storage, run, operation):
    manifest, files, name = run
    async def scenario():
        app = fixture(tmp_path)
        app.reporting_sessions = ReportingSessions(lambda *args, **kwargs: storage.create())
        app.selected_allure_result = lambda screen: (PreviewSnapshot(files, manifest, {}, False), name)
        async def prepare():
            await offload(app.reporting_sessions.acquire, storage.create().plan)
            app.stop_allure_admission()
        app.prepare_allure_reporting = prepare
        app._allure = Mock(side_effect=AssertionError('stale preparation created a viewer'))
        async with app.run_test() as pilot:
            await pilot.pause()
            await (app.show_allure_history() if operation == 'history' else app.open_selected_allure())
            app._allure.assert_not_called()
            assert await app.close_allure()
    asyncio.run(scenario())


@pytest.mark.parametrize('secondary', [False, True])
def test_quit_permanently_closes_noop_reporting_admission(tmp_path, run, secondary):
    manifest, files, name = run
    async def scenario():
        app = fixture(tmp_path, fields={
            'ALLURE_S3_BUCKET': {'type': 'str', 'default': 'reports'},
            'ALLURE_S3_USE_LOCALSTACK': {'type': 'bool', 'default': secondary},
            'ALLURE_VIEW_SECONDARY': {'type': 'bool', 'default': secondary}})
        app.selected_allure_result = lambda screen: (PreviewSnapshot(files, manifest, {}, False), name)
        app._allure = Mock(side_effect=AssertionError('Quit admitted a new viewer'))
        app.exit = Mock()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.confirm_quit()
            await app.show_allure_history()
            await app.open_selected_allure()
            app._allure.assert_not_called()
            assert not app.reporting_sessions.managers
    asyncio.run(scenario())


def test_external_opening_cancellation_keeps_original_outcome(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def prepare():
            entered.set()
            await proceed.wait()
        app.prepare_allure_reporting = prepare
        async with app.run_test() as pilot:
            await pilot.pause()
            opening = asyncio.create_task(app.show_allure_history())
            await entered.wait()
            opening.cancel()
            with pytest.raises(asyncio.CancelledError): await opening
            assert not app._allure_tasks and await app.close_allure()
    asyncio.run(scenario())


def test_generation_revoked_during_history_mount_skips_preparation(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            show = app.show
            async def show_then_invalidate(*args, **kwargs):
                await show(*args, **kwargs)
                app.stop_allure_admission()
            app.show = show_then_invalidate
            app.prepare_allure_reporting = Mock(side_effect=AssertionError('stale mount prepared reporting'))
            await app.show_allure_history()
            app.prepare_allure_reporting.assert_not_called()
            assert await app.close_allure()
    asyncio.run(scenario())


def test_cleanup_preserves_replacement_viewer_for_its_own_close(tmp_path):
    from test_allure_navigation import Viewer
    async def scenario():
        app = fixture(tmp_path)
        replacement = Viewer()
        class Replacing(Viewer):
            async def aclose(self):
                await super().aclose()
                app.allure_viewer = replacement
        original = Replacing()
        app.allure_viewer = original
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not await app.close_allure()
            assert app.inspection_cleanup_active()
            assert original.closed and app.allure_viewer is replacement and not replacement.closed
            assert await app.close_allure()
            assert replacement.closed and app.allure_viewer is None
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['history', 'preview'])
def test_close_observer_cannot_acknowledge_while_preparation_task_is_owned(tmp_path, storage, run, operation):
    manifest, files, name = run
    async def scenario():
        app = fixture(tmp_path)
        manager = storage.create()
        app.reporting_sessions = ReportingSessions(lambda *args, **kwargs: manager)
        app.reporting_sessions.acquire(manager.plan)
        barrier = Barrier(manager)
        async def prepare(): await offload(barrier.wait)
        app.prepare_allure_reporting = prepare
        app.selected_allure_result = lambda screen: (PreviewSnapshot(files, manifest, {}, False), name)
        async with app.run_test() as pilot:
            await pilot.pause()
            opening = asyncio.create_task(app.show_allure_history() if operation == 'history'
                                          else app.open_selected_allure())
            assert await asyncio.to_thread(barrier.entered.wait, 5)
            cleanup = app.begin_allure_cleanup()
            cleanup.timeout = .01
            try:
                assert not await app.close_allure()
                assert 'runtime' not in cleanup.done
                assert not opening.done() and manager.lease is not None
                assert cleanup in app.inspection_cleanup_receipt().owners
            finally:
                barrier.proceed.set()
            await opening
            assert await app.close_allure()
    asyncio.run(scenario())
