"""Async owners remain available through final quota and S3 publication."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.contracts import TestResult
from api_agent.testing.owned import run_owned_integration
from test_recorded_integration import fixture


class Rig:
    def __init__(self, config):
        self.config = config
        self.events = []
        self.loop = None
        self.closed = False
        self.instant = datetime(2028, 1, 1, tzinfo=timezone.utc)
        self.snapshots = iter((10, 12))

    @asynccontextmanager
    async def open(self, *, retain, validate=None):
        assert retain
        self.loop = asyncio.get_running_loop()
        if validate is not None:
            validate(SimpleNamespace(values=self.config))
        self.events.append('services-start')
        try:
            yield SimpleNamespace(config=SimpleNamespace(values=self.config),
                                  runtime=SimpleNamespace(emissions=None))
        finally:
            self.events.append('services-release')

    def session(self, owned, usage):
        rig = self
        class Session:
            async def start(self):
                assert asyncio.get_running_loop() is rig.loop
                rig.events.append('auth')
                usage.record_prompt(turn_id='turn', prompt_id='prompt', kind='initial')
            async def current_obo(self):
                assert not rig.closed
                return SimpleNamespace(access_token='synthetic-token')
            async def stop(self):
                rig.events.append('refresh-stop')
                rig.closed = True
        return Session()

    def gateway(self, owned):
        from api_agent.telemetry.usage import QuotaSnapshot
        rig = self
        class Gateway:
            async def astart(self):
                assert asyncio.get_running_loop() is rig.loop
                rig.events.append('gateway-start')
            async def fetch(self, token):
                assert token == 'synthetic-token' and not rig.closed
                rig.events.append('quota')
                return QuotaSnapshot(next(rig.snapshots), 'window', rig.instant + timedelta(hours=1), rig.instant)
            async def aclose(self):
                rig.events.append('gateway-close')
        return Gateway()

    def persist(self, directory, manifest):
        assert self.closed
        self.events.append('persist')

    def runner(self, context, tests, **hooks):
        hooks['before_run']()
        self.events.append('pytest-drained')
        result = IntegrationRunResult(0, tuple(TestResult(test, 'passed') for test in tests), '')
        try:
            hooks['after_run'](result)
        except Exception:
            return IntegrationRunResult(1, result.tests, '')
        return result


def test_owned_order_and_fresh_configuration(tmp_path):
    context, test, config = fixture(tmp_path)
    fresh = {**config, 'ALLURE_S3_BUCKET': 'fresh-bucket'}
    rig = Rig(fresh)
    lines = []
    async def scenario():
        result = await run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=rig.session, gateway_factory=rig.gateway,
            runner=rig.runner, storage=rig, usage_output=lines.append)
        assert result.persisted and result.exit_code == 0
        assert result.usage['pct_point_delta'] == 2
        assert result.usage['pct_used_per_prompt'] == '2.000'
    asyncio.run(scenario())
    assert rig.events == ['services-start', 'auth', 'gateway-start', 'quota',
                         'pytest-drained', 'quota', 'refresh-stop', 'gateway-close', 'persist', 'services-release']
    assert lines[0] == 'Starting quota used: 10.000%'
    assert 'Final quota used: 12.000%' in lines[1]


def test_attended_without_quota_owner(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    async def scenario():
        result = await run_owned_integration(context, [test], config, operation=rig,
            accepted=True, runner=rig.runner, storage=SimpleNamespace(persist=lambda *a: None))
        assert result.usage['pct_point_delta'] is None
    asyncio.run(scenario())
    assert rig.events == ['services-start', 'pytest-drained', 'services-release']


def test_rejects_before_starting_any_owner(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    async def scenario():
        with pytest.raises(PermissionError):
            await run_owned_integration(context, [test], config, operation=rig)
        with pytest.raises(ValueError, match='supplied together'):
            await run_owned_integration(context, [test], config, operation=rig,
                accepted=True, session_factory=rig.session)
    asyncio.run(scenario())
    assert rig.events == []


def test_cancellation_waits_for_workload_finalization_and_storage(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    entered, cancelled = Event(), Event()
    def runner(context, tests, **hooks):
        hooks['before_run']()
        entered.set()
        assert hooks['cancel_event'].wait(3)
        cancelled.set()
        result = IntegrationRunResult(130, tuple(TestResult(test, 'cancelled') for test in tests), '', True)
        hooks['after_run'](result)
        return result
    async def scenario():
        task = asyncio.create_task(run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=rig.session, gateway_factory=rig.gateway,
            runner=runner, storage=rig))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert rig.events[-5:] == ['quota', 'refresh-stop', 'gateway-close', 'persist', 'services-release']
    asyncio.run(scenario())


def test_uncaught_runner_failure_still_stops_async_owners(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        raise RuntimeError('synthetic runner failure')
    async def scenario():
        with pytest.raises(RuntimeError, match='synthetic runner failure'):
            await run_owned_integration(context, [test], config, operation=rig,
                accepted=True, session_factory=rig.session, gateway_factory=rig.gateway, runner=runner, storage=rig)
    asyncio.run(scenario())
    assert rig.events[-3:] == ['refresh-stop', 'gateway-close', 'services-release']


@pytest.mark.parametrize('phase', ['login', 'initial-quota', 'final-quota'])
def test_cancellation_interrupts_preflight_and_quota_without_releasing_gate(tmp_path, phase):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    async def scenario():
        entered, never = asyncio.Event(), asyncio.Event()
        def session_factory(owned, usage):
            session = rig.session(owned, usage)
            original = session.start
            async def start():
                if phase == 'login':
                    entered.set()
                    await never.wait()
                await original()
            session.start = start
            return session
        def gateway_factory(owned):
            gateway = rig.gateway(owned)
            original = gateway.fetch
            calls = 0
            async def fetch(token):
                nonlocal calls
                calls += 1
                if (phase == 'initial-quota' and calls == 1
                        or phase == 'final-quota' and calls == 2):
                    entered.set()
                    await never.wait()
                return await original(token)
            gateway.fetch = fetch
            return gateway
        def runner(context, tests, **hooks):
            try:
                hooks['before_run']()
            except Exception:
                pass
            cancelled = hooks['cancel_event'].is_set()
            result = IntegrationRunResult(130 if cancelled else 0,
                tuple(TestResult(test, 'cancelled' if cancelled else 'passed') for test in tests), '', cancelled)
            hooks['after_run'](result)
            return result
        task = asyncio.create_task(run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=session_factory, gateway_factory=gateway_factory,
            runner=runner, storage=rig))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert not never.is_set()
        assert rig.events[-4:] == ['refresh-stop', 'gateway-close', 'persist', 'services-release']
    asyncio.run(scenario())


def test_cleanup_attempts_both_clients_and_records_failure(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    def session_factory(owned, usage):
        session = rig.session(owned, usage)
        original = session.stop
        async def stop():
            await original()
            raise RuntimeError('synthetic refresh cleanup failure')
        session.stop = stop
        return session
    async def scenario():
        result = await run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=session_factory, gateway_factory=rig.gateway,
            runner=rig.runner, storage=rig)
        assert result.exit_code == 1 and result.persisted
        assert result.manifest['status'] == 'failed'
    asyncio.run(scenario())
    assert rig.events[-4:] == ['refresh-stop', 'gateway-close', 'persist', 'services-release']


def test_primary_runner_error_survives_cleanup_error(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    def session_factory(owned, usage):
        session = rig.session(owned, usage)
        original = session.stop
        async def stop():
            await original()
            raise RuntimeError('secondary cleanup failure')
        session.stop = stop
        return session
    def runner(context, tests, **hooks):
        hooks['before_run']()
        raise ValueError('primary runner failure')
    async def scenario():
        with pytest.raises(ValueError, match='primary runner failure'):
            await run_owned_integration(context, [test], config, operation=rig,
                accepted=True, session_factory=session_factory, gateway_factory=rig.gateway,
                runner=runner, storage=rig)
    asyncio.run(scenario())
    assert rig.events[-3:] == ['refresh-stop', 'gateway-close', 'services-release']


def test_changed_execution_requirements_rejected_before_services(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig({**config, 'NO_STD_IN': True})
    async def scenario():
        with pytest.raises(PermissionError):
            await run_owned_integration(context, [test], config, operation=rig, accepted=True)
    asyncio.run(scenario())
    assert rig.events == []


def test_final_snapshot_after_cancel_is_bounded(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    async def scenario():
        entered, never = asyncio.Event(), asyncio.Event()
        def gateway_factory(owned):
            gateway = rig.gateway(owned)
            async def fetch(token):
                entered.set()
                await never.wait()
            gateway.fetch = fetch
            return gateway
        task = asyncio.create_task(run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=rig.session, gateway_factory=gateway_factory,
            runner=rig.runner, storage=rig, quota_timeout=.02))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 1)
        assert rig.events[-4:] == ['refresh-stop', 'gateway-close', 'persist', 'services-release']
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before-preparation', 'after-login'])
def test_cancel_signal_prevents_auth_or_gateway_start(tmp_path, when):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    signal = Event()
    if when == 'before-preparation':
        signal.set()
    def session_factory(owned, usage):
        session = rig.session(owned, usage)
        original = session.start
        async def start():
            await original()
            signal.set()
        session.start = start
        return session
    def runner(context, tests, **hooks):
        try:
            hooks['before_run']()
        except Exception:
            pass
        result = IntegrationRunResult(130, tuple(TestResult(test, 'cancelled') for test in tests), '', True)
        hooks['after_run'](result)
        return result
    async def scenario():
        result = await run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=session_factory, gateway_factory=rig.gateway,
            cancel_event=signal, runner=runner, storage=SimpleNamespace(persist=lambda *a: None))
        assert result.exit_code == 130 and result.persisted
        assert result.usage['quota_unavailable_reason'] == 'missing_snapshot'
    asyncio.run(scenario())
    assert 'gateway-start' not in rig.events
    assert ('auth' in rig.events) == (when == 'after-login')
    assert rig.events[-1] == 'services-release'


def test_default_runner_collection_failure_is_recorded(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    persisted = []
    async def scenario():
        result = await run_owned_integration(context, [test], config, operation=rig,
            accepted=True, storage=SimpleNamespace(persist=lambda *a: persisted.append(a)))
        assert result.exit_code == 1 and result.persisted
        assert result.execution.tests[0].status == 'error'
    asyncio.run(scenario())
    assert len(persisted) == 1 and rig.events == ['services-start', 'services-release']


def test_worker_error_during_cancel_does_not_mask_cancellation(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    entered = Event()
    def runner(context, tests, **hooks):
        hooks['before_run']()
        entered.set()
        assert hooks['cancel_event'].wait(2)
        raise RuntimeError('secondary worker failure')
    async def scenario():
        task = asyncio.create_task(run_owned_integration(context, [test], config, operation=rig,
            accepted=True, session_factory=rig.session, gateway_factory=rig.gateway,
            runner=runner, storage=rig))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert rig.events[-3:] == ['refresh-stop', 'gateway-close', 'services-release']


def test_unhooked_cleanup_failure_reaches_caller(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    def session_factory(owned, usage):
        session = rig.session(owned, usage)
        async def stop():
            rig.events.append('refresh-stop')
            raise RuntimeError('owned cleanup failed')
        session.stop = stop
        return session
    def runner(context, tests, **hooks):
        hooks['before_run']()
        return IntegrationRunResult(1, tuple(TestResult(test, 'error') for test in tests), '')
    async def scenario():
        with pytest.raises(RuntimeError, match='owned cleanup failed'):
            await run_owned_integration(context, [test], config, operation=rig,
                accepted=True, session_factory=session_factory, gateway_factory=rig.gateway,
                runner=runner, storage=rig)
    asyncio.run(scenario())
    assert rig.events[-3:] == ['refresh-stop', 'gateway-close', 'services-release']


def test_first_cancel_during_final_cleanup_is_propagated_after_drain(tmp_path):
    context, test, config = fixture(tmp_path)
    rig = Rig(config)
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        def session_factory(owned, usage):
            session = rig.session(owned, usage)
            original = session.stop
            async def stop():
                entered.set()
                await finish.wait()
                await original()
            session.stop = stop
            return session
        def runner(context, tests, **hooks):
            hooks['before_run']()
            return IntegrationRunResult(1, tuple(TestResult(test, 'error') for test in tests), '')
        async def work():
            await run_owned_integration(context, [test], config, operation=rig,
                accepted=True, session_factory=session_factory, gateway_factory=rig.gateway,
                runner=runner, storage=rig)
            rig.events.append('continued-after-cancel')
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert 'continued-after-cancel' not in rig.events
    asyncio.run(scenario())
    assert rig.events[-3:] == ['refresh-stop', 'gateway-close', 'services-release']
