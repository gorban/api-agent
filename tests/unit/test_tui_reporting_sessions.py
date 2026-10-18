import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.testing.prerequisites import RecordingPrerequisites
from api_agent.tui.reporting import ReportingSessions
from test_reporting_registry import storage, tmp_path, CONFIG
from test_tui import fixture


def test_idle_and_publication_only_sessions_perform_no_allocation():
    factory = Mock()
    sessions = ReportingSessions(factory)
    sessions.acquire(SimpleNamespace(profiles=()))
    asyncio.run(sessions.aclose())
    factory.assert_not_called()


def test_two_tui_sessions_and_operation_keep_server_until_last_closure(storage):
    first, second = ReportingSessions(lambda *a, **k: storage.create()), ReportingSessions(lambda *a, **k: storage.create())
    plan = storage.create().plan
    identifier, = first.acquire(plan).container_ids
    second.acquire(plan)
    operation = storage.create(); operation.acquire()
    asyncio.run(first.aclose())
    assert storage.engine.records[identifier]['State']['Running']
    asyncio.run(second.aclose())
    assert storage.engine.records[identifier]['State']['Running']
    operation.release()
    assert not storage.engine.records[identifier]['State']['Running']
    assert first.managers == second.managers == {}
    with pytest.raises(RuntimeError, match='closing'): first.acquire(plan)


def test_session_reuses_handle_and_keeps_failed_release_until_retry(storage):
    factory = Mock(side_effect=lambda *a, **k: storage.create())
    sessions = ReportingSessions(factory)
    plan = storage.create().plan
    lease = sessions.acquire(plan)
    assert sessions.acquire(plan) is lease and factory.call_count == 1
    storage.engine.fail_stop = True
    with pytest.raises(CleanupPendingError) as caught: asyncio.run(sessions.aclose())
    assert len(sessions.managers) == 1 and caught.value.owners[0].lease is lease
    storage.engine.fail_stop = False
    asyncio.run(caught.value.retry())
    assert sessions.managers == {} and sessions.pending_cleanup is None


def test_failed_partial_session_acquisition_remains_recoverable(storage):
    manager = storage.create()
    sessions = ReportingSessions(lambda *a, **k: manager)
    storage.engine.fail_up = storage.engine.fail_stop = True
    with pytest.raises(Exception): sessions.acquire(manager.plan)
    assert list(sessions.managers.values()) == [manager]
    with pytest.raises(CleanupPendingError): asyncio.run(sessions.aclose())
    storage.engine.fail_stop = False
    storage.engine.records.clear()  # disposable fixture's independently verified removal
    asyncio.run(sessions.pending_cleanup.retry())
    assert not sessions.managers


def test_recording_session_joins_after_operation_and_shared_finish_releases_active_use(storage):
    sessions = ReportingSessions(lambda *a, **k: storage.create())
    operation = storage.create()
    events = []
    def join(plan):
        assert operation.lease is not None
        events.append('session')
        sessions.acquire(plan)
    subject = RecordingPrerequisites(prepare_dependencies=lambda: events.append('dependencies'),
        plan=lambda config: operation.plan, manager=lambda *args: operation, reporting_session=join)
    subject.prepare(('selected',), {**CONFIG, 'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'reports'}, accepted=True)
    identifier, = subject.lease.container_ids
    assert events == ['dependencies', 'session']
    subject.finish()
    assert operation.lease is None and storage.engine.records[identifier]['State']['Running']
    asyncio.run(sessions.aclose())
    assert not storage.engine.records[identifier]['State']['Running']


def test_tui_runner_callback_keeps_custom_runner_signatures_compatible(tmp_path):
    app = fixture(tmp_path)
    def narrow(context, tests, config): pass
    def explicit(context, tests, config, *, reporting_session): pass
    assert app.reporting_runner_options(narrow) == {}
    assert app.reporting_runner_options(explicit)['reporting_session'] == app.reporting_sessions.acquire
    assert app.reporting_runner_options(lambda **kwargs: None) == {}


def test_quit_releases_reporting_after_inspection_and_keeps_failed_receipt(tmp_path, storage):
    async def run():
        app = fixture(tmp_path)
        manager = storage.create()
        sessions = ReportingSessions(lambda *a, **k: manager)
        app.reporting_sessions = sessions
        identifier, = sessions.acquire(manager.plan).container_ids
        app.exit = Mock()
        app.notify = Mock()
        inspections = []
        async def end_inspection(**kwargs):
            inspections.append(manager.lease)
            return True
        app.end_inspection = end_inspection
        storage.engine.fail_stop = True
        await app.confirm_quit()
        app.exit.assert_not_called()
        assert app.pending_shutdown_cleanup is not None
        assert inspections == [manager.lease]
        storage.engine.fail_stop = False
        await app.pending_shutdown_cleanup.retry()
        assert app.pending_shutdown_cleanup is None and manager.lease is None
        await app.confirm_quit()
        app.exit.assert_called_once_with(3)
        assert not storage.engine.records[identifier]['State']['Running']
    asyncio.run(run())


def test_combined_shutdown_retry_retains_reporting_until_inspection_acknowledges(tmp_path, storage):
    async def run():
        app = fixture(tmp_path)
        manager = storage.create()
        app.reporting_sessions = ReportingSessions(lambda *a, **k: manager)
        app.reporting_sessions.acquire(manager.plan)
        failure = [True]
        async def retry():
            assert manager.lease is not None
            if failure[0]: raise RuntimeError('inspection still active')
        inspection = CleanupPendingError((object(),), retry)
        inspection.inspections = ('retained-browser',)
        inspection.inspection_group = object()
        app.pending_inspection_cleanup = inspection
        receipt = app.shutdown_cleanup_receipt()
        assert receipt.inspections == ('retained-browser',)
        with pytest.raises(RuntimeError): await receipt.retry()
        assert manager.lease is not None
        failure[0] = False
        await receipt.retry()
        assert manager.lease is None
    asyncio.run(run())


def test_noninteractive_launched_operation_lease_covers_publication(storage, monkeypatch, tmp_path):
    from api_agent.testing import launched
    from test_recorded_integration import fixture as recording_fixture, fake_runner, Storage
    context, test, config = recording_fixture(tmp_path)
    manager = storage.create()
    operation = RecordingPrerequisites(prepare_dependencies=lambda: None,
        plan=lambda config: manager.plan, manager=lambda *args: manager)
    class CheckedStorage(Storage):
        def persist(self, *args, **kwargs):
            assert manager.lease is not None
            assert storage.engine.records[manager.lease.container_ids[0]]['State']['Running']
            return super().persist(*args, **kwargs)
    result = launched.run_launched_integration(context, (test,), config, accepted=True,
        prerequisites=operation, runner=fake_runner(), storage=CheckedStorage())
    assert result.persisted and manager.lease is None
    assert not next(iter(storage.engine.records.values()))['State']['Running']


@pytest.mark.parametrize('values,expected', [
    ({'ALLURE_S3_BUCKET': 'reports', 'ALLURE_S3_USE_LOCALSTACK': True}, 1),
    ({'ALLURE_S3_BUCKET': 'reports', 'ALLURE_S3_USE_LOCALSTACK': False}, 0),
    ({'ALLURE_S3_BUCKET': 'reports', 'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_VIEW_SECONDARY': True}, 0),
    ({'ALLURE_S3_USE_LOCALSTACK': True}, 0),
])
def test_allure_explicit_managed_access_only_joins_session(tmp_path, values, expected):
    app = fixture(tmp_path)
    app.session.result = lambda: SimpleNamespace(values=values)
    app.reporting_sessions = SimpleNamespace(acquire=Mock())
    asyncio.run(app.prepare_allure_reporting())
    assert app.reporting_sessions.acquire.call_count == expected


def test_two_workbenches_normal_quit_leave_other_workbench_and_operation_live(tmp_path, storage):
    async def run():
        first, second = fixture(tmp_path), fixture(tmp_path)
        first.reporting_sessions = ReportingSessions(lambda *a, **k: storage.create())
        second.reporting_sessions = ReportingSessions(lambda *a, **k: storage.create())
        plan = storage.create().plan
        identifier, = first.reporting_sessions.acquire(plan).container_ids
        second.reporting_sessions.acquire(plan)
        operation = storage.create(); operation.acquire()
        first.exit, second.exit = Mock(), Mock()
        await first.confirm_quit()
        first.exit.assert_called_once()
        assert storage.engine.records[identifier]['State']['Running']
        await second.confirm_quit()
        second.exit.assert_called_once()
        assert storage.engine.records[identifier]['State']['Running']
        operation.release()
        assert not storage.engine.records[identifier]['State']['Running']
    asyncio.run(run())


def test_local_recording_adapter_forwards_session_environment_and_budget(storage, monkeypatch, tmp_path):
    from api_agent.runtime import dependencies, reporting_registry
    from api_agent.runtime.ownership import StackScope
    from api_agent.testing.prerequisites import local_recording_prerequisites
    monkeypatch.setattr(dependencies, 'active_environment', lambda root: True)
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda *args, **kwargs: None)
    managed = storage.create()
    monkeypatch.setattr(reporting_registry, 'SharedReportingManager', lambda *args, **kwargs: managed)
    callback = Mock()
    environment = {'PATH': '/usr/bin'}
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    owner = local_recording_prerequisites(context, scope=StackScope.from_roots(tmp_path, tmp_path),
        ownership_directory=tmp_path / 'operations', reporting_session=callback,
        engine_environment=environment, timeout=7)
    owner.prepare(('selected',), {**CONFIG, 'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'reports'}, accepted=True)
    assert callback.call_args.kwargs == {'engine_environment': environment, 'deadline_seconds': 7}
    assert callback.call_args.args[0].profiles == ('localstack',)
    owner.finish()


def test_cancelled_failed_shutdown_keeps_exported_retry_and_original_cancellation(tmp_path, storage):
    from threading import Event
    from api_agent.runtime.prerequisites import PrerequisiteError
    async def run():
        app = fixture(tmp_path)
        manager = storage.create()
        app.reporting_sessions = ReportingSessions(lambda *args, **kwargs: manager)
        lease = app.reporting_sessions.acquire(manager.plan)
        entered, proceed = Event(), Event()
        original = manager.release
        def fail_release(**kwargs):
            entered.set()
            assert proceed.wait(10)
            raise PrerequisiteError('synthetic failed stop during cancellation')
        manager.release = fail_release
        try:
            task = asyncio.create_task(app.close_reporting_sessions())
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError) as interrupted:
                await task
            receipt = app.pending_shutdown_cleanup
            assert receipt is not None and interrupted.value.cleanup_pending is receipt
            assert app.reporting_sessions.pending_cleanup is not None and manager.lease is lease
            manager.release = original
            await receipt.retry()
            assert manager.lease is None and not app.reporting_sessions.managers
            assert app.pending_shutdown_cleanup is None
        finally:
            proceed.set()
            manager.release = original
            await app.reporting_sessions.aclose()
    asyncio.run(run())
