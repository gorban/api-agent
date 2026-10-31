"""Managed integration collectors precede inference and outlive exporter flush."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.prerequisites import local_trace_prerequisites
from api_agent.testing.setup_backend import _SetupOwner


@pytest.mark.parametrize('mode,target,profiles', [
    ('LangFuse', None, ('langfuse',)),
    ('LangFuse', 'http://localhost:3000', ('langfuse',)),
    ('LangFuse', 'http://127.0.0.1:3000/', ('langfuse',)),
    ('OTEL', None, ('otel',)),
    ('OTEL', 'http://127.0.0.1:4318/v1/traces', ('otel',)),
    ('LangFuse', 'https://tracing.example.test', ()),
    ('OTEL', 'https://tracing.example.test/v1/traces', ()),
    ('', None, ()),
])
def test_trace_plan_selects_only_conventional_collector(monkeypatch, tmp_path, mode, target, profiles):
    from threading import Event
    from api_agent.runtime import prerequisites
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    config = dict(TRACE_MODE=mode, ALLURE_S3_USE_LOCALSTACK=True, RUN_UI=True,
                  LANGFUSE_PUBLIC_KEY='public-test', LANGFUSE_SECRET_KEY='secret-test')
    if target is not None:
        config['TRACE_TARGET'] = target
    made = Mock()
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', made)
    cancelled = Event()

    owner = local_trace_prerequisites(context, config, cancel_event=cancelled)

    if not profiles:
        assert owner is None
        made.assert_not_called()
        return
    assert owner is made.return_value
    plan, scope, directory = made.call_args.args
    assert plan.profiles == profiles
    assert 'localstack' not in plan.services
    assert directory == tmp_path / 'artifacts' / 'prerequisites'
    assert plan.environment['SA_SCOPE'] == scope.id
    assert not made.call_args.kwargs['cancelled']()
    cancelled.set()
    assert made.call_args.kwargs['cancelled']()
    made.return_value.acquire.assert_not_called()


def test_trace_plan_without_cancel_event(monkeypatch, tmp_path):
    from api_agent.runtime import prerequisites
    made = Mock()
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', made)
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    local_trace_prerequisites(context, {'TRACE_MODE': 'OTEL'})
    assert made.call_args.kwargs['cancelled']() is False


async def setup_owner(monkeypatch, tmp_path, log):
    from api_agent.testing import provider_setup
    from api_agent.telemetry import remote_inference
    async def record(name, result=None):
        log.append(name)
        return result
    provider = SimpleNamespace(emissions=EmissionBoundary(),
        _ensure=AsyncMock(), _complete=AsyncMock(), _close=AsyncMock(),
        _finish_execution=AsyncMock())
    trace = SimpleNamespace(astart=AsyncMock(), aclose=AsyncMock())
    async def ready(): return await record('provider-ready')
    async def complete(request): return await record('inference', 'Ok')
    async def close(): return await record('provider-close')
    async def finish(): return await record('provider-finish')
    async def start_trace(): return await record('trace-start')
    async def flush(): return await record('trace-flush')
    provider._ensure.side_effect = ready
    provider._complete.side_effect = complete
    provider._close.side_effect = close
    provider._finish_execution.side_effect = finish
    trace.astart.side_effect = start_trace
    trace.aclose.side_effect = flush
    manager = Mock(timeout=120, now=time.monotonic,
                   acquire=Mock(side_effect=lambda **kwargs: log.append('acquire')),
                   release=Mock(side_effect=lambda **kwargs: log.append('release')))
    monkeypatch.setattr(provider_setup, 'IntegrationLLM', lambda *args, **kwargs: provider)
    monkeypatch.setattr(remote_inference, 'RemoteInferenceTrace', lambda *args: trace)
    backend = SimpleNamespace(config={'TRACE_MODE': 'LangFuse'}, owner_root=tmp_path,
        unattended_index_source=None, password_manager_registry=None, interaction=None,
        quota_observer=None, authority=None, capture_sources={},
        trace_prerequisites_factory=Mock(return_value=manager))
    return _SetupOwner(backend), manager, trace


@pytest.mark.parametrize('publication', [False, True])
def test_collector_ready_before_inference_and_released_after_flush(monkeypatch, tmp_path, publication):
    log = []
    progress = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        from api_agent.providers.progress import progress_sink, report_timeout
        with progress_sink(progress.append):
            async with report_timeout('Parent setup', 3):
                await owner.ensure_ready()
        assert await owner.complete('request') == 'Ok'
        if publication:
            await owner.finish_execution()
        else:
            await owner.aclose()
        return manager
    manager = asyncio.run(run())
    readiness = [row for row in progress if row.name == 'Integration trace collector readiness']
    assert readiness[0].parent_id == progress[0].id
    assert 0 < readiness[0].timeout <= 3
    assert [(row.name, row.active) for row in readiness] == [
        ('Integration trace collector readiness', True),
        ('Integration trace collector readiness', False)]
    assert 0 < manager.acquire.call_args_list[0].kwargs['_deadline'] - manager.now() <= 3
    assert log[:5] == ['acquire', 'provider-ready', 'acquire', 'trace-start', 'inference']
    assert log[-2:] == ['trace-flush', 'release']
    manager.release.assert_called_once_with(retain=False)


def test_failed_readiness_prevents_authentication_and_inference(monkeypatch, tmp_path):
    log = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        manager.acquire.side_effect = RuntimeError('not ready')
        with pytest.raises(RuntimeError, match='not ready'):
            await owner.ensure_ready()
        await owner.aclose()
        return owner
    owner = asyncio.run(run())
    assert log == ['provider-close', 'trace-flush', 'release']
    owner.provider._ensure.assert_not_called()
    owner.provider._complete.assert_not_called()


def test_unfinished_exporter_keeps_collector_until_cleanup_retry(monkeypatch, tmp_path):
    log = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        trace.aclose.side_effect = [RuntimeError('still closing'), None]
        with pytest.raises(RuntimeError, match='still closing'):
            await owner.aclose()
        manager.release.assert_not_called()
        await owner.aclose()
        return manager
    manager = asyncio.run(run())
    manager.release.assert_called_once_with(retain=False)


def test_failed_collector_release_is_retried(monkeypatch, tmp_path):
    log = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        manager.release.side_effect = [RuntimeError('still owned'), None]
        with pytest.raises(RuntimeError, match='still owned'):
            await owner.aclose()
        await owner.aclose()
        return manager, trace
    manager, trace = asyncio.run(run())
    assert manager.release.call_count == 2
    assert trace.aclose.await_count == 2


@pytest.mark.parametrize('mode,target,expected', [
    ('LangFuse', 'http://localhost:3000', 90),
    ('OTEL', 'http://localhost:4318/v1/traces', 90),
    ('LangFuse', 'https://tracing.example.test', 5),
    ('', None, 5),
])
def test_backend_budgets_exporter_and_managed_collector_disposal(tmp_path, mode, target, expected):
    from api_agent.testing.setup_backend import SetupBackend
    config = {'TRACE_MODE': mode}
    if target is not None:
        config['TRACE_TARGET'] = target
    backend = SetupBackend(config, tmp_path, trace_prerequisites_factory=Mock())
    assert backend.cleanup_timeout == expected
    assert backend.thread is None
