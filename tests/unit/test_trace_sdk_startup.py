"""Partial real SDK allocations retain retryable disposal receipts."""
import asyncio
from pathlib import Path
from threading import Event, enumerate as threads
from unittest.mock import Mock

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import _sdk_factory
from api_agent.telemetry.tracing_providers import admit_binding

# Waits for an expected event; they bound only a hung test on a slow host.
EVENTUAL = 30
BASE = Path(__file__).resolve().parents[2]
BUNDLED_PROVIDER = str(BASE / 'src/api_agent/telemetry/trace_adapters/provider.py') + ':create_tracing_provider'


@pytest.fixture
def sessions(monkeypatch):
    from requests import Session
    allocated = []
    def create():
        session = Session()
        session.close = Mock(wraps=session.close)
        allocated.append(session)
        return session
    monkeypatch.setattr('requests.Session', create)
    return allocated


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('failure', ['provider', 'processor', 'registration', 'tracer'])
async def test_builtin_startup_failure_keeps_real_sdk_resources_owned(monkeypatch, sessions, backend, failure):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic startup failure')
    target, method = {
        'provider': (TracerProvider, '__init__'),
        'processor': (BatchSpanProcessor, '__init__'),
        'registration': (TracerProvider, 'add_span_processor'),
        'tracer': (TracerProvider, 'get_tracer'),
    }[failure]
    monkeypatch.setattr(target, method, fail)
    binding = admit_binding({'TRACE_PROVIDER': BUNDLED_PROVIDER, 'JAEGER_VIEWER_PORT': 16686, 'TRACE_MODE': backend, 'TRACE_EXPORT_ENDPOINT': 'http://unused.invalid/traces',
        'LANGFUSE_PUBLIC_KEY': 'synthetic-public', 'LANGFUSE_SECRET_KEY': 'synthetic-secret'},
        owner_root=BASE, boundary=EmissionBoundary())
    before = set(threads())

    with pytest.raises(RuntimeError, match='synthetic startup'):
        await binding.open({})
    assert len(binding._signals.owners) == 1
    owner = binding._signals.owners[0]
    allocated_workers = {worker for worker in set(threads()) - before
                         if worker.name == 'OtelBatchSpanRecordProcessor'}
    if failure in ('registration', 'tracer'):
        assert allocated_workers
    await binding.aclose()
    await binding.aclose()

    assert owner.disposed
    assert sessions[0].close.call_count == 1
    assert all(not worker.is_alive() for worker in allocated_workers)


def test_direct_factory_failure_exposes_receipt_and_rolls_back(monkeypatch, sessions):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic tracer failure')
    monkeypatch.setattr('opentelemetry.sdk.trace.TracerProvider.get_tracer', fail)

    with pytest.raises(RuntimeError) as caught:
        _sdk_factory('http://unused.invalid/traces', {}, {})

    assert caught.value.cleanup_owner.disposed
    assert sessions[0].close.call_count == 1
    caught.value.cleanup_owner.shutdown()
    assert sessions[0].close.call_count == 1


def test_direct_factory_failure_retains_failed_cleanup_for_retry(monkeypatch):
    session = Mock()
    session.close.side_effect = [RuntimeError('close pending'), None]
    monkeypatch.setattr('requests.Session', lambda: session)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic tracer failure')
    monkeypatch.setattr('opentelemetry.sdk.trace.TracerProvider.get_tracer', fail)

    with pytest.raises(RuntimeError, match='synthetic tracer') as caught:
        _sdk_factory('http://unused.invalid/traces', {}, {})
    owner = caught.value.cleanup_owner
    assert not owner.disposed
    assert 'session' not in owner.acknowledged

    owner.shutdown()
    owner.shutdown()

    assert owner.disposed
    assert session.close.call_count == 2


def test_owner_is_registered_before_session_allocation(monkeypatch):
    registered = []
    def create():
        assert len(registered) == 1
        raise RuntimeError('synthetic session failure')
    monkeypatch.setattr('requests.Session', create)

    with pytest.raises(RuntimeError) as caught:
        _sdk_factory('http://unused.invalid/traces', {}, {}, own=registered.append)

    assert caught.value.cleanup_owner is registered[0]
    registered[0].shutdown()
    assert registered[0].disposed


@pytest.mark.asyncio
async def test_cancelled_startup_and_close_retain_same_worker_receipt(monkeypatch, sessions):
    entered, release = Event(), Event()
    def fail(*args, **kwargs):
        entered.set()
        assert release.wait(EVENTUAL)
        raise RuntimeError('synthetic tracer failure')
    monkeypatch.setattr('opentelemetry.sdk.trace.TracerProvider.get_tracer', fail)
    binding = admit_binding({'TRACE_PROVIDER': BUNDLED_PROVIDER, 'JAEGER_VIEWER_PORT': 16686, 'TRACE_MODE': 'OTEL', 'TRACE_EXPORT_ENDPOINT': 'http://unused.invalid/traces'},
        owner_root=BASE, boundary=EmissionBoundary())
    opening = asyncio.create_task(binding.open({}))
    assert await asyncio.to_thread(entered.wait, EVENTUAL)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    owner = binding._signals.owners[0]
    closing = asyncio.create_task(binding.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not owner.disposed
        assert sessions[0].close.call_count == 0
    finally:
        release.set()
    await binding.aclose()
    await binding.aclose()
    assert owner.disposed
    assert sessions[0].close.call_count == 1


@pytest.mark.asyncio
async def test_actual_export_drain_precedes_session_close(sessions):
    owner, tracer = _sdk_factory('http://unused.invalid/traces', {}, {})
    entered, release = Event(), Event()
    response = Mock(status_code=200)
    def post(*args, **kwargs):
        entered.set()
        assert release.wait(EVENTUAL)
        return response
    sessions[0].post = post
    tracer.start_span('synthetic').end()
    assert await asyncio.to_thread(entered.wait, EVENTUAL)
    closing = asyncio.create_task(asyncio.to_thread(owner.shutdown))
    await asyncio.sleep(0)
    try:
        assert not owner.disposed
        assert sessions[0].close.call_count == 0
    finally:
        release.set()
    await closing
    owner.shutdown()
    assert owner.disposed
    assert sessions[0].close.call_count == 1
    response.close.assert_called_once()


@pytest.mark.parametrize('phase', ['exporter', 'processor', 'provider'])
def test_failed_sdk_shutdown_phase_retains_receipt_until_acknowledged(sessions, phase):
    owner, tracer = _sdk_factory('http://unused.invalid/traces', {}, {})
    component = getattr(owner, phase)
    original = component.shutdown
    attempts = []
    def shutdown():
        attempts.append(None)
        if len(attempts) == 1:
            original()
            raise RuntimeError('synthetic pending acknowledgement')
        original()
    component.shutdown = shutdown

    with pytest.raises(RuntimeError, match='pending acknowledgement'):
        owner.shutdown()
    assert not owner.disposed
    assert phase not in owner.acknowledged
    assert sessions[0].close.call_count == 0

    owner.shutdown()
    owner.shutdown()

    assert owner.disposed
    assert phase in owner.acknowledged
    assert sessions[0].close.call_count == 1
    assert len(attempts) >= 2


def test_export_drain_timeout_does_not_close_session(monkeypatch, sessions):
    owner, tracer = _sdk_factory('http://unused.invalid/traces', {}, {})
    condition = owner.exporter._condition
    original = condition.wait_for
    monkeypatch.setattr(condition, 'wait_for', lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match='disposal remains pending'):
        owner.shutdown()
    assert not owner.disposed
    assert 'exporter' not in owner.acknowledged
    assert sessions[0].close.call_count == 0

    monkeypatch.setattr(condition, 'wait_for', original)
    owner.shutdown()
    assert owner.disposed
    assert sessions[0].close.call_count == 1


def test_registration_failure_retains_empty_owner_without_allocating(monkeypatch, sessions):
    registered = []
    def reject(owner):
        registered.append(owner)
        raise RuntimeError('synthetic registration failure')

    with pytest.raises(RuntimeError) as caught:
        _sdk_factory('http://unused.invalid/traces', {}, {}, own=reject)

    assert caught.value.cleanup_owner is registered[0]
    assert sessions == []
    registered[0].shutdown()
    assert registered[0].disposed
    assert registered[0].force_flush()


def test_batch_overflow_reports_to_current_owner_without_unbounded_queue(monkeypatch, sessions):
    from types import SimpleNamespace
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    monkeypatch.setattr(BatchSpanProcessor, 'on_end', lambda self, span: None)
    owner, tracer = _sdk_factory('http://unused.invalid/traces', {}, {})
    loss = Mock()
    owner.trace_loss_owner = loss
    try:
        owner.processor.on_end(SimpleNamespace(context=None))
        owner.processor.on_end(SimpleNamespace(context=SimpleNamespace(trace_flags=SimpleNamespace(sampled=False))))
        span = SimpleNamespace(context=SimpleNamespace(trace_flags=SimpleNamespace(sampled=True)))
        for _ in range(1025):
            owner.processor.on_end(span)
        loss.record.assert_called_once_with()
    finally:
        owner.shutdown()


@pytest.mark.parametrize('value', ['true', ' TRUE ', 'TrUe'])
@pytest.mark.parametrize('registered', [False, True])
def test_disabled_sdk_conflict_precedes_owner_callback_session_and_sdk(monkeypatch, value, registered):
    import api_agent.telemetry.tracing as tracing
    monkeypatch.setenv('OTEL_SDK_DISABLED', value)
    owner_factory = Mock(wraps=tracing._TraceSDKOwner)
    own = Mock() if registered else None
    session = Mock()
    provider = Mock()
    monkeypatch.setattr(tracing, '_TraceSDKOwner', owner_factory)
    monkeypatch.setattr('requests.Session', session)
    monkeypatch.setattr('opentelemetry.sdk.trace.TracerProvider', provider)

    with pytest.raises(tracing.TraceError, match='disable flag conflicts') as caught:
        tracing._sdk_factory('http://unused.invalid/traces', {}, {}, own=own)

    assert not hasattr(caught.value, 'cleanup_owner')
    owner_factory.assert_not_called()
    session.assert_not_called()
    provider.assert_not_called()
    if own is not None:
        own.assert_not_called()
