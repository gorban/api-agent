from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing import launched, recorded
from api_agent.providers.control import CleanupPendingError
from test_recorded_integration import fixture, fake_runner, Storage


def owner(log, *, failure=None):
    subject = SimpleNamespace(pending_cleanup=None)
    subject.prepare = lambda *args, **kwargs: log.append(('prepare', kwargs))
    def finish():
        log.append(('finish', {}))
        if failure:
            subject.pending_cleanup = failure
            raise failure
    subject.finish = finish
    return subject


def test_launch_prepares_once_and_releases_after_publication(tmp_path):
    context, test, config = fixture(tmp_path)
    log = []
    class Destination(Storage):
        def persist(self, *args):
            log.append(('persist', {}))
            super().persist(*args)
    def prepare(usage):
        log.append(('custom_prepare', {}))
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        prerequisites=owner(log), storage=Destination(), prepare=prepare, runner=fake_runner())
    assert result.persisted and result.exit_code == 0
    assert [name for name, _ in log] == ['prepare', 'custom_prepare', 'persist', 'finish']
    assert log[0][1] == {'accepted': True, 'no_std_in': False}


def test_multiple_repository_callbacks_do_not_reset_usage_or_reprepare(tmp_path):
    context, test, config = fixture(tmp_path)
    log = []
    def runner(*args, **hooks):
        hooks['before_run']()
        hooks['before_run']()
        return fake_runner()(*args, **hooks)
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        prerequisites=owner(log), storage=Storage(), runner=runner,
        prepare=lambda usage: usage.record_prompt(turn_id='turn', prompt_id='initial', kind='initial'))
    assert result.usage['prompt_count'] == 1
    assert [name for name, _ in log] == ['prepare', 'finish']


def test_rejected_start_does_not_create_owner_or_artifacts(tmp_path, monkeypatch):
    context, test, config = fixture(tmp_path)
    build = Mock()
    monkeypatch.setattr(launched, 'local_recording_prerequisites', build)
    with pytest.raises(PermissionError):
        launched.run_launched_integration(context, [test], config)
    build.assert_not_called()
    assert not (tmp_path / 'artifacts').exists()


def test_default_owner_receives_only_engine_environment(tmp_path, monkeypatch):
    context, test, config = fixture(tmp_path)
    context.pack_root = context.owner_root
    log, calls = [], []
    def factory(*args, **kwargs):
        calls.append(kwargs)
        return owner(log)
    monkeypatch.setattr(launched, 'local_recording_prerequisites', factory)
    monkeypatch.setattr(launched.os, 'environ', {'PATH': '/tools', 'DOCKER_HOST': 'unix:///test', 'SECRET': 'private'})
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        storage=Storage(), runner=fake_runner())
    assert result.exit_code == 0
    assert calls[0]['engine_environment'] == {'PATH': '/tools', 'DOCKER_HOST': 'unix:///test'}
    assert calls[0]['ownership_directory'] == context.owner_root / 'artifacts/prerequisites'
    assert calls[0]['cancel_event'] is not None


@pytest.mark.parametrize('primary', [False, True])
def test_cleanup_failure_retains_result_or_original_exception(tmp_path, monkeypatch, primary):
    context, test, config = fixture(tmp_path)
    pending = CleanupPendingError((), None)
    subject = owner([], failure=pending)
    if primary:
        failure = ValueError('original failure')
        monkeypatch.setattr(recorded, 'run_recorded_integration', Mock(side_effect=failure))
    with pytest.raises(ValueError if primary else CleanupPendingError) as error:
        launched.run_launched_integration(context, [test], config, accepted=True,
            prerequisites=subject, storage=Storage(), runner=fake_runner())
    assert error.value.recording_prerequisites is subject
    assert error.value.cleanup_pending is pending
    if primary:
        assert error.value is failure and error.value.recorded_result is None
    else:
        assert error.value.recorded_result.persisted


def test_runner_failure_without_cleanup_failure_still_releases(tmp_path, monkeypatch):
    context, test, config = fixture(tmp_path)
    log = []
    monkeypatch.setattr(recorded, 'run_recorded_integration', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        launched.run_launched_integration(context, [test], config, accepted=True, prerequisites=owner(log))
    assert [name for name, _ in log] == ['finish']


def test_post_acquisition_cancellation_publishes_before_rollback(tmp_path, monkeypatch):
    from threading import Event
    from api_agent.testing.prerequisites import RecordingPrerequisites
    from api_agent.testing import integration
    from api_agent.runtime import prerequisites as runtime
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.ownership import StackScope
    from test_prerequisites import Engine, Clock
    context, test, config = fixture(tmp_path)
    context.base_root = context.owner_root
    clock, engine, cancelled = Clock(), Engine(), Event()
    scope = StackScope.from_roots(context.base_root, context.owner_root)
    config['ALLURE_S3_USE_LOCALSTACK'] = True
    manager = runtime.PrerequisiteManager(plan_compose(config, scope), scope,
        tmp_path / 'ownership', command_runner=engine.run, readiness_probe=lambda *args: True,
        now=clock.now, sleep=clock.sleep, identity=lambda pid: 'birth')
    engine.owner = manager
    acquire = manager.acquire
    def interrupted():
        lease = acquire()
        cancelled.set()
        return lease
    monkeypatch.setattr(manager, 'acquire', interrupted)
    subject = RecordingPrerequisites(prepare_dependencies=lambda: None,
        plan=lambda config: manager.plan, manager=lambda *args: manager, cancel_event=cancelled)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kw: [test])
    monkeypatch.setattr(integration.subprocess, 'Popen', Mock(side_effect=AssertionError('No child after cancellation')))
    calls = []
    class Destination(Storage):
        def persist(self, directory, manifest):
            assert manager.lease is not None
            assert all(row['State']['Running'] for row in engine.records.values())
            assert manifest['status'] == 'interrupted'
            calls.append('published')
            super().persist(directory, manifest)
    completed = launched.run_launched_integration(context, [test], config, accepted=True,
        prerequisites=subject, storage=Destination(), cancel_event=cancelled)
    assert completed.persisted and completed.exit_code == 130
    assert calls == ['published']
    assert manager.lease is None and not any(row['State']['Running'] for row in engine.records.values())


@pytest.mark.parametrize('injected', [False, True])
def test_requirement_validation_precedes_owner_acquisition(tmp_path, monkeypatch, injected):
    context, test, config = fixture(tmp_path)
    subject = owner([])
    plan = SimpleNamespace(validate=Mock(side_effect=ValueError('invalid requirements')))
    planner, factory = Mock(return_value=plan), Mock(return_value=subject)
    monkeypatch.setattr(launched, 'plan_integration_requirements', planner)
    monkeypatch.setattr(launched, 'local_recording_prerequisites', factory)
    with pytest.raises(ValueError, match='invalid requirements'):
        launched.run_launched_integration(context, [test], config, accepted=True,
            prerequisites=subject if injected else None)
    planner.assert_called_once_with((test,))
    plan.validate.assert_called_once_with(context, config)
    factory.assert_not_called()


@pytest.mark.parametrize('requires_api', [False, True])
def test_default_owner_uses_single_validated_requirement_plan(tmp_path, monkeypatch, requires_api):
    context, test, config = fixture(tmp_path)
    context.pack_root = context.owner_root
    plan = SimpleNamespace(validate=Mock(), requires_pack_api=requires_api)
    planner, factory = Mock(return_value=plan), Mock(return_value=owner([]))
    monkeypatch.setattr(launched, 'plan_integration_requirements', planner)
    monkeypatch.setattr(launched, 'local_recording_prerequisites', factory)
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        storage=Storage(), runner=fake_runner())
    assert result.exit_code == 0
    planner.assert_called_once_with((test,))
    plan.validate.assert_called_once_with(context, config)
    expected = {'llm', 'api', 'upstream', 'access'} if requires_api else {'llm'}
    assert factory.call_args.kwargs['provider_kinds'] == frozenset(expected)
