"""Trace delivery outcomes never masquerade as outstanding resource disposal."""
import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.api.application import Application, Runtime
from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.local_run import LocalRunOwner
from api_agent.runtime.operation import OperationCleanup
from api_agent.telemetry.tracing import TraceDeliveryError, TraceError, TraceRuntime, TRACE_DELIVERY_WARNING


def tracing(provider):
    config = SimpleNamespace(errors=(), missing=(), values={'TRACE_MODE': 'OTEL'})
    return TraceRuntime(config, factory=lambda *args: (provider, Mock()))


async def finish_cleanup_or_release_test_trace(local, trace):
    finishing = asyncio.create_task(local.finish_cleanup())
    try:
        done, _ = await asyncio.wait({finishing}, timeout=.5)
        assert done, 'Acknowledged trace disposal must finish local cleanup'
        await finishing
    finally:
        if not finishing.done():
            # Keep a failed regression from stranding the deliberate retry loop.
            trace.aclose = AsyncMock(return_value=True)
            await asyncio.wait_for(finishing, 1)


def test_failed_delivery_stops_joined_local_operation_and_releases_lease(caplog):
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=False), shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        runtime = Runtime('synthetic', tracing=trace, timeout=.05)
        manager = Mock()
        operation = OperationCleanup(manager, runtime, None, object(), False, .05)
        events = []
        local = LocalRunOwner(SimpleNamespace(), lambda: None, on_event=events.append)
        local._session = SimpleNamespace(runtime=runtime)
        local._stack.push(operation.retry)

        await finish_cleanup_or_release_test_trace(local, trace)

        assert local.snapshot.phase == 'stopped' and local.pending_cleanup is None
        assert runtime.pending_cleanup is None
        assert runtime.cleanup_warnings == (TRACE_DELIVERY_WARNING,)
        assert {'runtime', 'lease'} <= operation.done
        manager.release.assert_called_once_with(retain=False)
        provider.force_flush.assert_called_once()
        provider.shutdown.assert_called_once()
        assert [event.text for event in events if event.kind == 'log'] == [TRACE_DELIVERY_WARNING + '\n']
        await local.aclose()
        assert len([event for event in events if event.kind == 'log']) == 1
        with pytest.raises(TraceDeliveryError) as failure:
            await trace.aclose()
        assert failure.value.disposal_confirmed
        assert trace.shutdown() is False
    asyncio.run(exercise())
    assert [record.message for record in caplog.records] == [TRACE_DELIVERY_WARNING]
    assert caplog.records[0].levelname == 'ERROR'


def test_asgi_shutdown_completes_with_fixed_delivery_warning(caplog):
    async def exercise():
        provider = Mock(force_flush=Mock(side_effect=RuntimeError('synthetic secret diagnostic')),
                        shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        runtime = Runtime('synthetic', tracing=trace)
        app = Application(runtime, None)
        send = AsyncMock()

        await app({'type': 'lifespan'}, AsyncMock(return_value={'type': 'lifespan.shutdown'}), send)

        send.assert_awaited_once_with({'type': 'lifespan.shutdown.complete'})
        assert runtime.cleanup_warnings == (TRACE_DELIVERY_WARNING,)
        await runtime.aclose()
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())
    assert [record.message for record in caplog.records] == [TRACE_DELIVERY_WARNING]
    assert 'synthetic secret' not in caplog.text


@pytest.mark.parametrize('failure', [RuntimeError('synthetic secret'),
                                     BaseException('synthetic secret'),
                                     asyncio.CancelledError('synthetic secret')])
def test_shutdown_exception_remains_pending_then_retries_disposal_without_export_replay(caplog, monkeypatch, failure):
    thread_failure = Mock()
    monkeypatch.setattr('threading.excepthook', thread_failure)
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=True),
                        shutdown=Mock(side_effect=[failure, None]))
        trace = tracing(provider)
        await trace.astart()
        runtime = Runtime('synthetic', tracing=trace)

        with pytest.raises(CleanupPendingError) as pending:
            await runtime.aclose()
        assert not trace._shutdown_complete
        assert runtime.cleanup_warnings == ()
        await pending.value.retry()

        assert trace._shutdown_complete and runtime.pending_cleanup is None
        provider.force_flush.assert_called_once()
        assert provider.shutdown.call_count == 2
        assert runtime.cleanup_warnings == ()
    asyncio.run(exercise())
    assert caplog.records == []
    thread_failure.assert_not_called()


def test_delivery_warning_does_not_acknowledge_failed_shutdown():
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=False),
                        shutdown=Mock(side_effect=[RuntimeError('synthetic'), None]))
        trace = tracing(provider)
        await trace.astart()
        with pytest.raises(TraceError) as failure:
            await trace.aclose()
        assert not isinstance(failure.value, TraceDeliveryError)
        assert not trace._shutdown_complete
        with pytest.raises(TraceDeliveryError):
            await trace.aclose()
        provider.force_flush.assert_called_once()
        assert provider.shutdown.call_count == 2
    asyncio.run(exercise())


def test_cancelled_close_and_timeout_keep_single_live_worker_pending():
    entered, release = Event(), Event()
    def flush(**kwargs):
        entered.set()
        assert release.wait(2)
        return False
    async def exercise():
        provider = Mock(force_flush=Mock(side_effect=flush), shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        closing = asyncio.create_task(trace.aclose(timeout=.02))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            worker = trace._worker
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            with pytest.raises(TraceError) as pending:
                await trace.aclose(timeout=.01)
            assert not isinstance(pending.value, TraceDeliveryError)
            assert trace._worker is worker and worker.is_alive()
            assert not trace._shutdown_complete
            provider.shutdown.assert_not_called()
        finally:
            release.set()
            with pytest.raises(TraceDeliveryError):
                await trace.aclose(timeout=1)
        provider.force_flush.assert_called_once()
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())


def test_delivery_error_from_other_owner_still_retains_cleanup():
    async def exercise():
        child = SimpleNamespace(aclose=AsyncMock(side_effect=TraceDeliveryError('synthetic')))
        runtime = Runtime('synthetic', api_provider=child)
        with pytest.raises(CleanupPendingError):
            await runtime.aclose()
        assert id(child) not in runtime._cleaned
        assert runtime.cleanup_warnings == ()
        child.aclose.side_effect = None
        await runtime.aclose()
    asyncio.run(exercise())


def test_pending_shutdown_status_persists_until_acknowledged_without_flicker():
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=False),
                        shutdown=Mock(side_effect=[RuntimeError('synthetic'), RuntimeError('synthetic'), None]))
        trace = tracing(provider)
        await trace.astart()
        runtime = Runtime('synthetic', tracing=trace, timeout=.05)
        operation = OperationCleanup(Mock(), runtime, None, object(), False, .05)
        events = []
        local = LocalRunOwner(SimpleNamespace(), lambda: None, on_event=events.append)
        local._session = SimpleNamespace(runtime=runtime)
        local.CLEANUP_RETRY_SECONDS = .001
        local._stack.push(operation.retry)

        await finish_cleanup_or_release_test_trace(local, trace)

        assert [event.snapshot.phase for event in events if event.snapshot is not None] == [
            'stopping', 'cleanup-pending', 'stopped']
        assert provider.shutdown.call_count == 3
        provider.force_flush.assert_called_once()
        assert local.pending_cleanup is None and runtime.pending_cleanup is None
        assert local._logs['agent'] == TRACE_DELIVERY_WARNING + '\n'
    asyncio.run(exercise())


def test_delivery_failure_stays_visible_after_later_flush_reports_success():
    async def exercise():
        provider = Mock(force_flush=Mock(side_effect=[False, True, True]), shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        assert trace.flush() is False
        assert trace.flush() is False
        with pytest.raises(TraceDeliveryError):
            await trace.aclose()
        assert trace.disposal_confirmed
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())


def test_shutdown_completion_at_awaiter_timeout_does_not_invent_delivery_failure(monkeypatch):
    async def exercise():
        trace = tracing(Mock())
        trace._shutdown_complete = True
        monkeypatch.setattr(trace, 'shutdown', Mock(return_value=False))
        assert await trace.aclose()
    asyncio.run(exercise())


def test_worker_export_abort_has_safe_delivery_outcome_after_acknowledged_shutdown(monkeypatch):
    thread_failure = Mock()
    monkeypatch.setattr('threading.excepthook', thread_failure)
    async def exercise():
        provider = Mock(force_flush=Mock(side_effect=BaseException('synthetic secret')),
                        shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        with pytest.raises(TraceDeliveryError) as failure:
            await trace.aclose()
        assert str(failure.value) == TRACE_DELIVERY_WARNING
        assert trace.disposal_confirmed
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())
    thread_failure.assert_not_called()


@pytest.mark.parametrize('failure', [RuntimeError('synthetic publication failure'),
                                     asyncio.CancelledError('synthetic publication cancellation'),
                                     BaseException('synthetic publication abort')])
def test_retained_cleanup_survives_hostile_pending_publication(monkeypatch, failure):
    async def exercise():
        local = LocalRunOwner(SimpleNamespace(), lambda: None)
        local.CLEANUP_RETRY_SECONDS = .001
        callback = AsyncMock(side_effect=[RuntimeError('synthetic disposal failure'), None])
        local._stack.push(callback)
        retained = []
        def reject_pending_publication():
            assert local.pending_cleanup is not None
            retained.append(local.pending_cleanup)
            raise failure
        publisher = Mock(side_effect=reject_pending_publication)
        monkeypatch.setattr(local, '_publish_cleanup_pending', publisher)

        await asyncio.wait_for(local.finish_cleanup(), 1)

        publisher.assert_called_once()
        assert len(retained) == 1
        assert callback.await_count == 2
        assert local.pending_cleanup is None
        assert local.snapshot.phase == 'stopped'
        assert local._stack.callbacks == []
    asyncio.run(exercise())


def test_retried_stopped_publication_does_not_repeat_acknowledged_delivery_warning(monkeypatch, caplog):
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=False), shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        runtime = Runtime('synthetic', tracing=trace, timeout=.05)
        manager = Mock()
        operation = OperationCleanup(manager, runtime, None, object(), False, .05)
        events = []
        local = LocalRunOwner(SimpleNamespace(), lambda: None, on_event=events.append)
        local._session = SimpleNamespace(runtime=runtime)
        local._stack.push(operation.retry)
        original_publish = local._publish
        stopped_attempts = []
        def fail_first_stopped_publication(phase, **options):
            if phase == 'stopped':
                stopped_attempts.append(phase)
                if len(stopped_attempts) == 1:
                    raise RuntimeError('synthetic stopped publication failure')
            original_publish(phase, **options)
        monkeypatch.setattr(local, '_publish', fail_first_stopped_publication)

        with pytest.raises(RuntimeError, match='synthetic stopped publication failure'):
            await local.aclose()
        assert local.pending_cleanup is None
        assert runtime.cleanup_warnings == (TRACE_DELIVERY_WARNING,)
        await local.aclose()

        assert stopped_attempts == ['stopped', 'stopped']
        assert local.snapshot.phase == 'stopped'
        assert [event.text for event in events if event.kind == 'log'] == [TRACE_DELIVERY_WARNING + '\n']
        manager.release.assert_called_once_with(retain=False)
        provider.force_flush.assert_called_once()
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())
    assert [record.message for record in caplog.records] == [TRACE_DELIVERY_WARNING]


@pytest.mark.parametrize('delivered', [False, True])
def test_closed_flush_preserves_acknowledged_delivery_outcome_without_replay(delivered):
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=delivered), shutdown=Mock())
        trace = tracing(provider)
        await trace.astart()
        if delivered:
            assert await trace.aclose()
        else:
            with pytest.raises(TraceDeliveryError):
                await trace.aclose()
        assert trace.disposal_confirmed

        assert trace.flush() is delivered
        assert trace.flush() is delivered

        provider.force_flush.assert_called_once()
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())


def test_closed_flush_remains_false_while_successful_export_awaits_shutdown_acknowledgement():
    entered, release = Event(), Event()
    def shutdown():
        entered.set()
        assert release.wait(2)
    async def exercise():
        provider = Mock(force_flush=Mock(return_value=True), shutdown=Mock(side_effect=shutdown))
        trace = tracing(provider)
        await trace.astart()
        try:
            assert trace.shutdown(timeout=.01) is False
            assert entered.wait(1)
            worker = trace._worker
            assert not trace.disposal_confirmed

            assert trace.flush() is False
            assert trace.flush() is False

            assert trace._worker is worker and worker.is_alive()
            provider.force_flush.assert_called_once()
            provider.shutdown.assert_called_once()
        finally:
            release.set()
            assert await trace.aclose(timeout=1)
        assert trace.flush() is True
        provider.force_flush.assert_called_once()
        provider.shutdown.assert_called_once()
    asyncio.run(exercise())


def test_closed_disabled_tracing_flush_is_inert_success_without_factory_calls():
    async def exercise():
        factory = Mock()
        config = SimpleNamespace(errors=(), missing=(), values={'TRACE_MODE': ''})
        trace = TraceRuntime(config, factory=factory)
        await trace.astart()
        assert await trace.aclose()

        assert trace.flush() is True
        assert trace.flush() is True

        factory.assert_not_called()
    asyncio.run(exercise())
