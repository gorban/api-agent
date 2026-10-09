from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.prerequisites import PrerequisiteCancelled, ServiceLease
from api_agent.testing.prerequisites import RecordingPrerequisites, local_recording_prerequisites


CONFIG = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'allure-results',
          'ALLURE_S3_USE_LOCALSTACK': True, 'TRACE_MODE': 'LangFuse'}
LEASE = ServiceLease('invocation', 'scope', ('localstack',), ('owned',), ('reused',))


def owner(*, cancel_event=None):
    manager = Mock(lease=LEASE)
    manager.acquire.return_value = LEASE
    deps, plan = Mock(), Mock(return_value='plan')
    factory = Mock(return_value=manager)
    return RecordingPrerequisites(prepare_dependencies=deps, plan=plan, manager=factory,
                                  cancel_event=cancel_event), manager, deps, plan, factory


def prepare(subject, **kwargs):
    return subject.prepare(('selected',), CONFIG, accepted=True, **kwargs)


def test_inert_validated_storage_only_and_idempotent():
    subject, manager, deps, plan, factory = owner()
    deps.assert_not_called()
    factory.assert_not_called()
    with pytest.raises(PermissionError):
        subject.prepare(('selected',), CONFIG)
    deps.assert_not_called()
    assert prepare(subject) is LEASE
    assert prepare(subject) is LEASE
    deps.assert_called_once_with()
    plan.assert_called_once_with({'ALLURE_S3_BUCKET': 'allure-results', 'ALLURE_S3_USE_LOCALSTACK': True})
    assert subject.lease.owned_ids == ('owned',)
    assert subject.lease.reused_ids == ('reused',)
    subject.finish()
    subject.finish()
    manager.release.assert_called_once_with(retain=True)
    assert subject.owned_manager is None


@pytest.mark.parametrize('config,tests,error', [
    ({**CONFIG, 'TUI_MODE': 'unit'}, ('selected',), PermissionError),
    ({**CONFIG, 'COLLECT_ONLY': True}, ('selected',), PermissionError),
    ({**CONFIG, 'ALLURE_S3_BUCKET': ''}, ('selected',), ValueError),
    (CONFIG, (), ValueError),
])
def test_rejection_before_dependencies(config, tests, error):
    subject, _, deps, _, factory = owner()
    with pytest.raises(error):
        subject.prepare(tests, config, accepted=True)
    deps.assert_not_called()
    factory.assert_not_called()


def test_cancellation_before_and_after_dependency_preparation():
    event = Event()
    subject, _, deps, _, factory = owner(cancel_event=event)
    event.set()
    with pytest.raises(PrerequisiteCancelled):
        prepare(subject)
    deps.assert_not_called()
    event.clear()
    deps.side_effect = event.set
    with pytest.raises(PrerequisiteCancelled):
        prepare(subject)
    factory.assert_not_called()


def test_partial_acquisition_and_post_acquisition_cancellation_rollback():
    event = Event()
    subject, manager, _, _, _ = owner(cancel_event=event)
    manager.acquire.side_effect = RuntimeError('failed')
    with pytest.raises(RuntimeError, match='failed'):
        prepare(subject)
    manager.release.assert_called_once_with(retain=False)
    manager.reset_mock()
    manager.acquire.side_effect = lambda: (event.set(), LEASE)[1]
    with pytest.raises(PrerequisiteCancelled):
        prepare(subject)
    manager.release.assert_not_called()
    assert subject.lease is LEASE
    subject.finish()
    manager.release.assert_called_once_with(retain=False)


def test_release_failure_retains_owner_and_async_retry():
    subject, manager, _, _, _ = owner()
    prepare(subject)
    manager.release.side_effect = [RuntimeError('failed'), None]
    with pytest.raises(CleanupPendingError) as caught:
        subject.finish()
    assert subject.owned_manager is manager
    assert caught.value.owners == (manager,)
    with pytest.raises(CleanupPendingError):
        prepare(subject)
    with pytest.raises(CleanupPendingError):
        subject.finish()
    asyncio.run(caught.value.retry())
    assert subject.pending_cleanup is None
    assert subject.owned_manager is None
    assert manager.release.call_count == 2


def test_acquire_primary_failure_preserved_with_cleanup_retry():
    subject, manager, _, _, _ = owner()
    manager.acquire.side_effect = ValueError('primary')
    manager.release.side_effect = [RuntimeError('cleanup'), None]
    with pytest.raises(ValueError, match='primary') as caught:
        prepare(subject)
    assert caught.value.cleanup_pending is subject.pending_cleanup
    asyncio.run(subject.pending_cleanup.retry())
    assert manager.release.call_args.kwargs == {'retain': False}


def test_cancellation_after_plan_before_manager():
    event = Event()
    subject, _, _, plan, factory = owner(cancel_event=event)
    plan.side_effect = lambda config: event.set()
    with pytest.raises(PrerequisiteCancelled):
        prepare(subject)
    factory.assert_not_called()


def test_factory_reports_only_active_interpreter(monkeypatch, tmp_path):
    import api_agent.runtime.dependencies as dependencies
    import api_agent.runtime.compose as compose
    import api_agent.runtime.prerequisites as prerequisites
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path / 'pack', external_pack=True)
    active, ensure, plan, manager = Mock(return_value=True), Mock(return_value=None), Mock(), Mock()
    manager.return_value.acquire.return_value = LEASE
    monkeypatch.setattr(dependencies, 'active_environment', active)
    monkeypatch.setattr(dependencies, 'ensure_environment', ensure)
    monkeypatch.setattr(compose, 'plan_compose', plan)
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', manager)
    subject = local_recording_prerequisites(context, scope='scope', ownership_directory=tmp_path,
                                          argv=('launch',), timeout=5)
    ensure.assert_not_called()
    prepare(subject)
    active.assert_called_once_with(context.pack_root / '.venv')
    ensure.assert_called_once_with(context, ('launch',), groups=('reports',))
    assert manager.call_args.kwargs['deadline_seconds'] == 5
    subject.finish()
    active.return_value = False
    with pytest.raises(RuntimeError, match='interpreter_handoff_required'):
        prepare(subject)
    active.return_value = True
    ensure.return_value = 0
    with pytest.raises(RuntimeError, match='interpreter_handoff_required'):
        prepare(subject)


def test_invalid_timeout():
    with pytest.raises(ValueError):
        RecordingPrerequisites(prepare_dependencies=Mock(), plan=Mock(), manager=Mock(), timeout=0)


def test_failed_acquisition_without_lease_retries_manager_release():
    subject, manager, _, _, _ = owner()
    manager.lease = None
    manager.acquire.side_effect = RuntimeError('primary')
    manager.release.side_effect = [RuntimeError('cleanup'), None]
    with pytest.raises(RuntimeError, match='primary'):
        prepare(subject)
    asyncio.run(subject.pending_cleanup.retry())
    assert manager.release.call_count == 2
    assert subject.owned_manager is None
