"""A prerequisite provider remains on one loop across physical waves."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from api_agent.providers.loading import LoadedProvider
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.control import CleanupPendingError
from api_agent.testing.native_recovery import NativeRecoveryOwner, admitted_directory
from api_agent.testing.suite_prerequisite_provider import SuitePrerequisiteProvider


@pytest.fixture
def context(tmp_path):
    recovery = NativeRecoveryOwner(tmp_path)
    recovery.prepare()
    return SimpleNamespace(owner_root=tmp_path), recovery


class Contribution:
    capabilities = ProviderCapabilities(kind='test_prerequisites', operations=('prepare', 'dispose_workers'))
    def __init__(self):
        self.loops, self.starts, self.closed, self.disposals = [], 0, 0, 0
    async def aprepare(self, request, *, auth=None):
        self.loops.append(asyncio.get_running_loop())
        self.starts += 1
        assert admitted_directory() is not None
    async def adispose_workers(self, request, *, auth=None):
        assert request == {'confirmed': True}
        self.disposals += 1
    async def aclose(self):
        self.loops.append(asyncio.get_running_loop())
        self.closed += 1


def install(monkeypatch, contribution):
    import api_agent.testing.suite_prerequisite_provider as module
    async def load(*args, **kwargs):
        assert kwargs['kind'] == 'test_prerequisites'
        return LoadedProvider(contribution, 'synthetic_provider')
    monkeypatch.setattr(module, 'aload_provider', load)


def make(context, **config):
    owner, recovery = context
    return SuitePrerequisiteProvider(owner, {'E2E_PREREQUISITE_PROVIDER': 'example.py:create',
        'E2E_PREREQUISITE_TIMEOUT_SECONDS': 2, **config}, recovery.directory.parent, recovery)


def test_one_owner_loop_across_repeated_start_and_confirmed_disposal(context, monkeypatch):
    contribution = Contribution()
    install(monkeypatch, contribution)
    owner = make(context)
    with context[1].bind():
        owner.start()
        owner.start()
        owner.close(workers_disposed=True)
    owner.close()
    assert contribution.starts == contribution.closed == contribution.disposals == 1
    assert contribution.loops[0] is contribution.loops[1]
    with pytest.raises(ValueError, match='closed'):
        owner.start()
    assert admitted_directory() is None


def test_unstarted_owner_closes_without_loading(context):
    owner = make(context)
    asyncio.run(owner._dispose())
    owner.close()
    assert owner.closed and owner.loop is None


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), True, '1'])
def test_invalid_budgets_never_create_loop(context, value):
    with pytest.raises(ValueError, match='finite'):
        make(context, E2E_PREREQUISITE_TIMEOUT_SECONDS=value)


def test_cleanup_failure_keeps_provider_on_original_loop_for_retry(context, monkeypatch):
    contribution = Contribution()
    original = contribution.aclose
    async def fail_once():
        contribution.aclose = original
        raise ValueError('synthetic cleanup failure')
    contribution.aclose = fail_once
    install(monkeypatch, contribution)
    owner = make(context)
    with context[1].bind():
        owner.start()
        with pytest.raises(CleanupPendingError):
            owner.close()
        owner.close()
    assert contribution.closed == 1


def test_confirmed_disposal_can_unlock_an_already_closing_provider(context, monkeypatch):
    class AwaitingDisposal(Contribution):
        async def aclose(self):
            if not self.disposals:
                raise RuntimeError('worker disposal proof remains pending')
            self.closed += 1
    contribution = AwaitingDisposal()
    install(monkeypatch, contribution)
    owner = make(context)
    with context[1].bind():
        owner.start()
    with pytest.raises(CleanupPendingError):
        owner.close(workers_disposed=False)
    assert owner.provider.closed and not owner.closed
    owner.close(workers_disposed=True)
    assert owner.closed and contribution.disposals == contribution.closed == 1


def test_provider_without_optional_disposal_hook_closes_with_confirmed_workers(context, monkeypatch):
    class PrepareOnly(Contribution):
        capabilities = ProviderCapabilities(kind='test_prerequisites', operations=('prepare',))
    contribution = PrepareOnly()
    install(monkeypatch, contribution)
    owner = make(context)
    with context[1].bind():
        owner.start()
        owner.close(workers_disposed=True)
    assert contribution.closed == 1 and contribution.disposals == 0


def test_uncertain_loop_thread_disposal_retains_same_cleanup_for_retry(context, monkeypatch):
    contribution = Contribution()
    install(monkeypatch, contribution)
    owner = make(context)
    with context[1].bind():
        owner.start()
    thread = owner.thread
    original = thread.is_alive
    observations = iter([True])
    monkeypatch.setattr(thread, 'is_alive', lambda: next(observations, False) or original())
    with pytest.raises(CleanupPendingError) as failure:
        owner.close()
    assert not owner.closed and owner.loop is not None
    asyncio.run(failure.value.retry())
    assert owner.closed and contribution.closed == 1


def test_reused_controller_and_retained_inspection_do_not_restart_or_close(context, monkeypatch):
    from api_agent.testing import integration
    calls = []
    class Owner:
        def start(self, *, cancelled):
            calls.append(cancelled)
        def close(self, **kwargs):
            pytest.fail('an open inspection retains the suite owner')
    monkeypatch.setattr(integration._RUNNER_OPERATION, 'prerequisites', Owner(), raising=False)
    monkeypatch.setattr(integration._RUNNER_OPERATION, 'recovery', context[1], raising=False)
    cancellation = threading.Event()
    integration._prepare_suite_prerequisites(context[0], {'E2E_PREREQUISITE_PROVIDER': 'example.py:create'}, None, cancellation)
    integration._close_suite_prerequisites([SimpleNamespace(closed=False, _when_closed=lambda callback: None)])
    assert calls == [cancellation]


def test_failed_wave_retains_suite_cleanup_error_on_original_failure(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    case = TestIdentity('fixture::case', 'fixture', 'case.py', 'case.py::case',
                        tags=frozenset({'requires_ui_session'}))
    primary, cleanup = ValueError('wave failed'), RuntimeError('cleanup still owned')
    def wave(*args, **kwargs):
        raise primary
    def close(*args):
        raise cleanup
    monkeypatch.setattr(integration, '_run_integration_tests', wave)
    monkeypatch.setattr(integration, '_close_suite_prerequisites', close)
    with pytest.raises(ValueError, match='wave failed') as failure:
        integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2})
    assert failure.value is primary
    assert failure.value.confirmation_cleanup_errors == (cleanup,)


def test_failed_load_retains_rollback_owners_on_original_loop_until_disposed(context, monkeypatch):
    import api_agent.testing.suite_prerequisite_provider as module
    from api_agent.providers.loading import ProviderError
    loops = []
    class FailedOwner:
        async def aclose(self):
            loops.append(asyncio.get_running_loop())
            if len(loops) == 1:
                raise ValueError('rollback remains pending')
    failed = FailedOwner()
    async def load(*args, **kwargs):
        raise ProviderError('provider_cleanup_pending', retained_owners=(failed,))
    monkeypatch.setattr(module, 'aload_provider', load)
    owner = make(context)
    with pytest.raises(ProviderError, match='provider_cleanup_pending'):
        owner.start()
    with pytest.raises(CleanupPendingError):
        owner.close()
    assert owner.failed_owners == [failed]
    assert not owner.closed and owner.loop is loops[0]
    owner.close()
    assert owner.closed and not owner.failed_owners
    assert loops[0] is loops[1]


@pytest.mark.parametrize('stage', ['construct', 'start'])
def test_thread_launch_failure_closes_unused_loop_and_allows_retry(context, monkeypatch, stage):
    import api_agent.testing.suite_prerequisite_provider as module
    original = module.threading.Thread
    def fail(*args, **kwargs):
        if stage == 'construct':
            raise RuntimeError('cannot construct owner thread')
        thread = original(*args, **kwargs)
        thread.start = lambda: (_ for _ in ()).throw(RuntimeError('cannot start owner thread'))
        return thread
    contribution = Contribution()
    install(monkeypatch, contribution)
    owner = make(context)
    monkeypatch.setattr(module.threading, 'Thread', fail)
    with pytest.raises(RuntimeError, match='owner thread'):
        owner.start()
    assert owner.loop is owner.thread is None
    monkeypatch.setattr(module.threading, 'Thread', original)
    with context[1].bind():
        owner.start()
        owner.close()
    assert contribution.starts == contribution.closed == 1


def test_cancelled_observer_keeps_inflight_preparation_owned(context, monkeypatch):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Gated(Contribution):
        async def aprepare(self, request, *, auth=None):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            self.starts += 1
    contribution = Gated()
    install(monkeypatch, contribution)
    owner = make(context, E2E_PREREQUISITE_CLEANUP_TIMEOUT_SECONDS=0.02)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(owner.start, cancelled=cancelled)
            assert entered.wait(2)
            cancelled.set()
            assert waiting.result(timeout=0.5) is False
        with pytest.raises(CleanupPendingError):
            owner.close()
        assert not owner.closed and not owner.start_future.done()
    finally:
        release.set()
        owner.cleanup_timeout = 2
        owner.close()
    assert contribution.starts == contribution.closed == 1


def test_disposal_acknowledgement_timeout_reuses_one_owned_future(context, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Gated(Contribution):
        async def adispose_workers(self, request, *, auth=None):
            self.disposals += 1
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
    contribution = Gated()
    install(monkeypatch, contribution)
    owner = make(context, E2E_PREREQUISITE_CLEANUP_TIMEOUT_SECONDS=0.02)
    try:
        with context[1].bind():
            owner.start()
        with pytest.raises(CleanupPendingError):
            owner.close(workers_disposed=True)
        assert entered.is_set()
        first = owner.acknowledgement_future
        with pytest.raises(CleanupPendingError):
            owner.close()
        assert owner.acknowledgement_future is first
        assert contribution.disposals == 1 and contribution.closed == 0
    finally:
        release.set()
        owner.cleanup_timeout = 2
        owner.close()
    assert contribution.disposals == contribution.closed == 1


def test_preparation_timeout_retains_startup_until_cleanup(context, monkeypatch):
    release = threading.Event()
    class Gated(Contribution):
        async def aprepare(self, request, *, auth=None):
            while not release.is_set():
                await asyncio.sleep(0.005)
    contribution = Gated()
    install(monkeypatch, contribution)
    owner = make(context, E2E_PREREQUISITE_TIMEOUT_SECONDS=0.02)
    try:
        with pytest.raises(CleanupPendingError):
            owner.start()
        assert not owner.start_future.done()
    finally:
        release.set()
        owner.close()
    assert contribution.closed == 1


def test_completed_provider_timeout_is_not_misreported_as_observer_timeout(context, monkeypatch):
    class Failed(Contribution):
        async def aprepare(self, request, *, auth=None):
            raise TimeoutError('provider readiness expired')
    contribution = Failed()
    install(monkeypatch, contribution)
    owner = make(context)
    with pytest.raises(TimeoutError, match='provider readiness expired'):
        owner.start()
    owner.close()
    assert contribution.closed == 1


def test_native_binding_rejects_closed_owner(context):
    recovery = context[1]
    recovery.closed = True
    with pytest.raises(ValueError, match='closed'):
        with recovery.bind():
            pytest.fail('closed owner must not bind')


def test_actual_broker_resource_survives_three_physical_confirmation_waves(tmp_path, monkeypatch):
    from api_agent.testing import integration
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.suite_prerequisite_broker import SuitePrerequisiteBroker, read_descriptor, borrow_prerequisite
    from api_agent.testing.collection import TestIdentity
    counts = {'start': 0, 'stop': 0}
    leases = []
    class Resource:
        def acquire(self, *, deadline):
            counts['start'] += 1
            return {'endpoint': 'controlled'}
        def release(self):
            counts['stop'] += 1
    class Handler:
        def prepare(self, scope, *, deadline):
            return 'c' * 64, Resource
    class Provider(Contribution):
        async def aprepare(self, request, *, auth=None):
            self.broker = SuitePrerequisiteBroker(request['suite_directory'], {'example': Handler()}, owner_budget=2)
            self.broker.start()
        async def aclose(self):
            self.broker.close(timeout=2)
    install(monkeypatch, Provider())
    case = TestIdentity('fixture::case', 'fixture', 'case.py', 'case.py::case',
                        tags=frozenset({'requires_ui_session'}))
    statuses = iter(['failed', 'passed', 'passed'])
    def wave(context, tests, **options):
        options['before_run']()
        descriptor = read_descriptor(options['suite_directory'])
        lease = borrow_prerequisite(descriptor, 'example', {'account_scope': 'a' * 64}, timeout=2)
        leases.append(lease.lease_id)
        assert lease.value == {'endpoint': 'controlled'}
        assert counts['stop'] == 0
        lease.release()
        result = TestResult(case, next(statuses))
        options['on_result'](result)
        return integration.IntegrationRunResult(int(result.status != 'passed'), (result,), '')
    monkeypatch.setattr(integration, '_run_integration_tests', wave)
    result = integration.run_integration_tests(SimpleNamespace(owner_root=tmp_path), [case], accepted=True,
        config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 2, 'E2E_PREREQUISITE_PROVIDER': 'example.py:create'})
    assert result.tests[0].status == 'passed'
    assert len(leases) == len(set(leases)) == 3
    assert counts == {'start': 1, 'stop': 1}


def test_task_scoped_borrower_does_not_cross_concurrent_attempts():
    from api_agent.runtime.prerequisite_context import bind_prerequisite_borrower, prerequisite_borrower
    async def exercise():
        entered = asyncio.Event()
        async def one():
            with bind_prerequisite_borrower('client-one', 'scope-one'):
                entered.set()
                await asyncio.sleep(0)
                assert prerequisite_borrower() == ('client-one', 'scope-one')
        async def two():
            await entered.wait()
            assert prerequisite_borrower() is None
            with bind_prerequisite_borrower('client-two', 'scope-two'):
                assert prerequisite_borrower() == ('client-two', 'scope-two')
        await asyncio.gather(one(), two())
        assert prerequisite_borrower() is None
    asyncio.run(exercise())


@pytest.mark.parametrize('event', [
    {}, {'phase': 'unknown', 'seconds': 1}, {'phase': 'prepare', 'seconds': float('nan')},
    {'phase': 'prepare', 'seconds': -1}, {'phase': 'prepare', 'seconds': True},
    {'phase': 'prepare', 'seconds': 1, 'count': True},
    {'phase': 'prepare', 'seconds': 1, 'reuse': -1},
    {'phase': 'prepare', 'seconds': 1, 'endpoint': 'secret'},
])
def test_event_admission_rejects_content_and_invalid_numbers(context, event):
    owner = make(context)
    with pytest.raises(ValueError, match='Invalid'):
        owner._event(event)
    assert owner.events == ()


def test_provider_passes_thread_safe_snapshot_callback_to_contribution(context, monkeypatch):
    import api_agent.testing.suite_prerequisite_provider as module
    class DiagnosticContribution(Contribution):
        async def aprepare(self, request, *, auth=None):
            self.callback({'phase': 'prepare', 'seconds': 0.25, 'miss': 1})
        async def aclose(self):
            self.callback({'phase': 'cleanup', 'seconds': 0.5, 'pending': 0})
    contribution = DiagnosticContribution()
    async def load(_location, _config, services, **kwargs):
        contribution.callback = services['prerequisite_event']
        return LoadedProvider(contribution, 'synthetic_provider')
    monkeypatch.setattr(module, 'aload_provider', load)
    owner = make(context)
    owner.start()
    snapshot = owner.events
    snapshot[0]['miss'] = 99
    owner.close()
    assert owner.events == ({'phase': 'prepare', 'seconds': 0.25, 'miss': 1},
                            {'phase': 'cleanup', 'seconds': 0.5, 'pending': 0})


def test_cancelled_load_retains_rollback_on_original_loop(context, monkeypatch):
    import api_agent.testing.suite_prerequisite_provider as module
    loops = []
    class Rollback:
        async def aclose(self):
            loops.append(asyncio.get_running_loop())
    class CancelledLoad(asyncio.CancelledError):
        retained_owners = (Rollback(),)
    async def load(*args, **kwargs):
        loops.append(asyncio.get_running_loop())
        raise CancelledLoad()
    monkeypatch.setattr(module, 'aload_provider', load)
    owner = make(context)
    from concurrent.futures import CancelledError
    with pytest.raises(CancelledError):
        owner.start()
    owner.close()
    assert owner.closed and len(loops) == 2 and loops[0] is loops[1]
