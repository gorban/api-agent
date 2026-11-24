"""The shared S3 prerequisite: settings, built-in test provider and Run local companion."""
import os
import time
from types import SimpleNamespace

import pytest

from api_agent.runtime import shared_s3 as s3
from api_agent.runtime.compose import ComposeError
from api_agent.runtime.ownership import StackScope

LOCAL = {'S3_USE_LOCALSTACK': True, 'LOCALSTACK_PORT': 4566, 'S3_REGION': 'us-east-1',
         'S3_ENDPOINT': 'http://localhost:4566', 'S3_EXTRA_BUCKETS': 'application-data'}
EXTERNAL = {'S3_USE_LOCALSTACK': False, 'S3_ENDPOINT': 'https://s3.example', 'S3_REGION': 'eu-west-1'}


@pytest.fixture
def scope(tmp_path):
    return StackScope.from_roots(tmp_path, tmp_path)


class FakeManager:
    def __init__(self, plan, events, **options):
        self.plan, self.events, self.options = plan, events, options
        self.fail_acquire = self.fail_release = False

    def acquire(self):
        self.events.append('s3 acquire')
        if self.fail_acquire:
            raise RuntimeError('acquire failed')

    def release(self):
        self.events.append('s3 release')
        if self.fail_release:
            raise RuntimeError('release failed')


@pytest.fixture
def managers(monkeypatch):
    created, events = [], []

    def factory(plan, **options):
        created.append(FakeManager(plan, events, **options))
        return created[-1]
    monkeypatch.setattr(s3, 'shared_s3_manager', factory)
    return SimpleNamespace(created=created, events=events)


def test_only_the_shared_settings_are_selected():
    values = {**LOCAL, 'ALLURE_S3_BUCKET': 'allure-results', 'ALLURE_S3_PREFIX': 'p', 'UNRELATED': 1}

    assert s3.s3_config(values) == {**LOCAL, 'ALLURE_S3_BUCKET': 'allure-results'}


def test_shared_plan_leases_only_localstack_with_every_bucket(scope):
    plan = s3.shared_s3_plan({**LOCAL, 'ALLURE_S3_BUCKET': 'allure-results', 'TRACE_MODE': 'OTEL'}, scope)

    assert plan.profiles == plan.services == ('localstack',)
    assert plan.environment['SA_S3_BUCKETS'] == 'allure-results,application-data'


def test_external_s3_plans_nothing(scope):
    assert not s3.shared_s3_plan(EXTERNAL, scope).enabled


def test_invalid_bucket_names_are_rejected_before_any_start(scope):
    with pytest.raises(ComposeError, match='valid S3 bucket names'):
        s3.shared_s3_plan({**LOCAL, 'S3_EXTRA_BUCKETS': 'Bad_Bucket'}, scope)
    with pytest.raises(ComposeError, match='comma-separated'):
        s3.shared_s3_plan({**LOCAL, 'S3_EXTRA_BUCKETS': 7}, scope)


@pytest.mark.parametrize(('values', 'expected'), [
    ({}, {'S3_ENDPOINT': 'http://localhost:4566', 'S3_REGION': 'us-east-1'}),
    ({'LOCALSTACK_PORT': 4600, 'S3_EXTRA_BUCKETS': 'a-bucket, b-bucket', 'ALLURE_S3_BUCKET': 'allure-results',
      'S3_REGION': 'eu-west-1'},
     {'S3_ENDPOINT': 'http://localhost:4600', 'S3_REGION': 'eu-west-1'}),
    (EXTERNAL, {'S3_ENDPOINT': 'https://s3.example', 'S3_REGION': 'eu-west-1'}),
    ({'S3_USE_LOCALSTACK': False}, {'S3_REGION': 'us-east-1'}),
])
def test_consumers_receive_the_endpoint_and_region(values, expected):
    assert s3.s3_environment(values) == expected


def services(tmp_path, values, **options):
    return {'owner_root': tmp_path, 'cancelled': lambda: False, 'deadline': time.monotonic() + 30,
            's3': values, **options}


def test_builtin_provider_leases_for_the_run_and_yields_the_connection(tmp_path, managers):
    with s3.shared_s3(services(tmp_path, LOCAL)) as environment:
        assert environment == {'S3_ENDPOINT': 'http://localhost:4566', 'S3_REGION': 'us-east-1'}
        assert managers.events == ['s3 acquire']
    assert managers.events == ['s3 acquire', 's3 release']
    options = managers.created[0].options
    assert 0 < options['deadline_seconds'] <= 30 and options['cancelled']() is False


def test_builtin_provider_defaults_to_the_shared_localstack(tmp_path, managers):
    with s3.shared_s3(services(tmp_path, None)) as environment:
        assert environment['S3_ENDPOINT'] == 'http://localhost:4566'
    assert managers.events == ['s3 acquire', 's3 release']


def test_builtin_provider_releases_when_the_run_fails(tmp_path, managers):
    with pytest.raises(RuntimeError, match='boom'):
        with s3.shared_s3(services(tmp_path, LOCAL)):
            raise RuntimeError('boom')

    assert managers.events == ['s3 acquire', 's3 release']


def test_builtin_provider_settles_a_failed_acquisition(tmp_path, monkeypatch, managers):
    original = s3.shared_s3_manager

    def failing(plan, **options):
        manager = original(plan, **options)
        manager.fail_acquire = True
        return manager
    monkeypatch.setattr(s3, 'shared_s3_manager', failing)

    with pytest.raises(RuntimeError, match='acquire failed'):
        with s3.shared_s3(services(tmp_path, LOCAL)):
            pytest.fail('body must not run')

    assert managers.events == ['s3 acquire', 's3 release']


def test_a_failed_release_after_a_failed_acquisition_keeps_the_acquire_error(tmp_path, monkeypatch, managers):
    original = s3.shared_s3_manager

    def failing(plan, **options):
        manager = original(plan, **options)
        manager.fail_acquire = manager.fail_release = True
        return manager
    monkeypatch.setattr(s3, 'shared_s3_manager', failing)

    with pytest.raises(RuntimeError, match='acquire failed') as caught:
        with s3.shared_s3(services(tmp_path, LOCAL)):
            pytest.fail('body must not run')

    assert any('release failed' in note for note in caught.value.__notes__)


def test_builtin_provider_starts_nothing_for_external_s3(tmp_path, managers):
    with s3.shared_s3(services(tmp_path, EXTERNAL)) as environment:
        assert environment == {'S3_ENDPOINT': 'https://s3.example', 'S3_REGION': 'eu-west-1'}

    assert managers.created == []


def test_the_process_engine_environment_is_limited_to_engine_keys(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'unix:///engine.sock')
    monkeypatch.setenv('API_KEY', 'secret')

    environment = s3.engine_environment_from_process()

    assert environment['DOCKER_HOST'] == 'unix:///engine.sock' and 'API_KEY' not in environment


class Primary:
    extra = 'delegated'

    def __init__(self, events):
        self.events = events
        self.fail_acquire = self.fail_release = False

    @property
    def lease(self):
        return 'primary lease'

    def acquire(self, **options):
        self.events.append(('primary acquire', options))
        if self.fail_acquire:
            raise RuntimeError('primary acquire failed')
        return 'primary lease'

    def release(self, *, retain=False, **options):
        self.events.append(('primary release', retain, options))
        if self.fail_release:
            raise RuntimeError('primary release failed')
        return ('stopped',)


@pytest.fixture
def clean_environment(monkeypatch):
    for name in ('S3_ENDPOINT', 'S3_REGION', 'S3_APPLICATION_BUCKET'):
        monkeypatch.delenv(name, raising=False)


def companion(managers, scope, values=LOCAL):
    primary = Primary(managers.events)
    return primary, s3.with_shared_s3(primary, values, scope)


def test_run_local_exports_the_running_endpoint_and_application_bucket(managers, scope, clean_environment):
    primary, manager = companion(managers, scope)

    assert manager.acquire(_deadline=5) == 'primary lease'

    assert (os.environ['S3_ENDPOINT'], os.environ['S3_REGION'], os.environ['S3_APPLICATION_BUCKET']) == (
        'http://localhost:4566', 'us-east-1', 'application-data')
    assert managers.events == ['s3 acquire', ('primary acquire', {'_deadline': 5})]
    assert manager.lease == 'primary lease' and manager.extra == 'delegated'


def test_release_restores_the_environment_and_leaves_both_leases(managers, scope, monkeypatch, clean_environment):
    monkeypatch.setenv('S3_REGION', 'ap-south-1')
    primary, manager = companion(managers, scope)
    manager.acquire()
    managers.events.clear()

    assert manager.release(retain=True, _deadline=7) == ('stopped',)

    assert managers.events == [('primary release', True, {'_deadline': 7}), 's3 release']
    assert os.environ['S3_REGION'] == 'ap-south-1'
    assert 'S3_ENDPOINT' not in os.environ and 'S3_APPLICATION_BUCKET' not in os.environ


def test_a_configured_application_bucket_is_not_replaced(managers, scope, monkeypatch, clean_environment):
    monkeypatch.setenv('S3_APPLICATION_BUCKET', 'my-bucket')
    _, manager = companion(managers, scope)

    manager.acquire()

    assert os.environ['S3_APPLICATION_BUCKET'] == 'my-bucket'
    manager.release()
    assert os.environ['S3_APPLICATION_BUCKET'] == 'my-bucket'


def test_no_application_bucket_is_exported_without_extra_buckets(managers, scope, clean_environment):
    _, manager = companion(managers, scope, {**LOCAL, 'S3_EXTRA_BUCKETS': ''})

    manager.acquire()

    assert 'S3_APPLICATION_BUCKET' not in os.environ
    manager.release()


def test_external_s3_exports_the_configured_endpoint_without_leasing(managers, scope, clean_environment):
    primary, manager = companion(managers, scope, EXTERNAL)

    manager.acquire()

    assert managers.created == []
    assert (os.environ['S3_ENDPOINT'], os.environ['S3_REGION']) == ('https://s3.example', 'eu-west-1')
    assert 'S3_APPLICATION_BUCKET' not in os.environ
    manager.release()
    assert 'S3_ENDPOINT' not in os.environ


def test_a_failed_primary_acquisition_gives_back_the_s3_lease_and_environment(managers, scope, clean_environment):
    primary, manager = companion(managers, scope)
    primary.fail_acquire = True

    with pytest.raises(RuntimeError, match='primary acquire failed'):
        manager.acquire()

    assert managers.events[-1] == 's3 release'
    assert 'S3_ENDPOINT' not in os.environ


def test_a_failed_primary_acquisition_restores_the_environment_without_an_s3_lease(managers, scope, clean_environment):
    primary, manager = companion(managers, scope, EXTERNAL)
    primary.fail_acquire = True

    with pytest.raises(RuntimeError, match='primary acquire failed'):
        manager.acquire()

    assert managers.created == [] and 'S3_ENDPOINT' not in os.environ


def test_a_failed_s3_acquisition_never_starts_the_primary(managers, scope, clean_environment, monkeypatch):
    _, manager = companion(managers, scope)
    managers.created[0].fail_acquire = True

    with pytest.raises(RuntimeError, match='acquire failed'):
        manager.acquire()

    assert managers.events == ['s3 acquire']


def test_a_failed_rollback_keeps_the_cleanup_duty_of_the_failure(managers, scope, clean_environment):
    primary, manager = companion(managers, scope)
    primary.fail_acquire = True
    pending = object()
    failure = RuntimeError('release failed')
    failure.cleanup_pending = pending
    managers.created[0].release = lambda: (_ for _ in ()).throw(failure)

    with pytest.raises(RuntimeError, match='primary acquire failed') as caught:
        manager.acquire()

    assert caught.value.cleanup_pending is pending


def test_release_attempts_both_leases_and_reports_the_first_failure(managers, scope, clean_environment):
    primary, manager = companion(managers, scope)
    manager.acquire()
    primary.fail_release = True
    managers.created[0].fail_release = True

    with pytest.raises(RuntimeError, match='primary release failed'):
        manager.release()

    assert 's3 release' in managers.events
    assert 'S3_ENDPOINT' not in os.environ


def test_a_release_without_an_acquisition_leaves_the_environment_alone(managers, scope, monkeypatch,
                                                                         clean_environment):
    monkeypatch.setenv('S3_REGION', 'ap-south-1')
    _, manager = companion(managers, scope)

    assert manager.release() == ('stopped',)

    assert managers.events == [('primary release', False, {}), 's3 release']
    assert os.environ['S3_REGION'] == 'ap-south-1' and 'S3_ENDPOINT' not in os.environ


def test_a_failing_s3_release_is_reported_after_the_primary_released(managers, scope, clean_environment):
    primary, manager = companion(managers, scope)
    manager.acquire()
    managers.created[0].fail_release = True

    with pytest.raises(RuntimeError, match='release failed'):
        manager.release()

    assert ('primary release', False, {}) in managers.events
