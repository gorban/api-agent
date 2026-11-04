"""Managed integration collectors precede inference and outlive exporter flush."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.prerequisites import local_trace_prerequisites
from api_agent.testing.setup_backend import _SetupOwner


BASE = Path(__file__).resolve().parents[2]
PROVIDER = str(BASE / 'src/api_agent/telemetry/trace_adapters/provider.py') + ':create_tracing_provider'


def trace_config(mode, target=None):
    from api_agent.config.resolve import ConfigResult
    from api_agent.telemetry.tracing_providers import effective_selection
    endpoint = target if target is not None else ('http://localhost:4318/v1/traces' if mode == 'OTEL' else 'http://localhost:3000/api/public/otel/v1/traces')
    values = dict(TRACE_PROVIDER=PROVIDER, TRACE_MODE=mode, TRACE_EXPORT_ENDPOINT=endpoint, JAEGER_VIEWER_PORT=16686)
    selected = effective_selection(ConfigResult(values, {}, (), (), ()), environ={})
    return dict(selected.values) if selected is not None else values


def trace_context(tmp_path):
    from test_pack_composition import pack
    from api_agent.runtime.resource_pack import load_launch_context
    return load_launch_context(BASE, pack(tmp_path, 'application'))


@pytest.mark.parametrize('mode,target,profiles', [
    ('LangFuse', None, ('langfuse',)),
    ('LangFuse', 'http://localhost:3000/api/public/otel/v1/traces', ('langfuse',)),
    ('LangFuse', 'http://127.0.0.1:3000/api/public/otel/v1/traces', ('langfuse',)),
    ('OTEL', None, ('otel',)),
    ('OTEL', 'http://127.0.0.1:4318/v1/traces', ('otel',)),
    ('LangFuse', 'https://tracing.example.test', ()),
    ('OTEL', 'https://tracing.example.test/v1/traces', ()),
    ('', None, ()),
])
def test_trace_plan_selects_only_conventional_collector(monkeypatch, tmp_path, mode, target, profiles):
    from threading import Event
    from api_agent.runtime import prerequisites
    context = trace_context(tmp_path)
    config = dict(trace_config(mode, target), ALLURE_S3_USE_LOCALSTACK=True, RUN_UI=True,
                  LANGFUSE_PUBLIC_KEY='public-test', LANGFUSE_SECRET_KEY='secret-test')
    if target is not None:
        config['TRACE_EXPORT_ENDPOINT'] = target
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
    assert directory == context.owner_root / 'artifacts' / 'prerequisites'
    assert plan.environment['SA_SCOPE'] == scope.id
    assert not made.call_args.kwargs['cancelled']()
    cancelled.set()
    assert made.call_args.kwargs['cancelled']()
    made.return_value.acquire.assert_not_called()


def test_trace_plan_without_cancel_event(monkeypatch, tmp_path):
    from api_agent.runtime import prerequisites
    made = Mock()
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', made)
    context = trace_context(tmp_path)
    local_trace_prerequisites(context, trace_config('OTEL'))
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
    monkeypatch.setattr(remote_inference, 'RemoteInferenceTrace', lambda *args, **kwargs: trace)
    backend = SimpleNamespace(config=trace_config('LangFuse'), owner_root=tmp_path,
        unattended_index_source=None, password_manager_registry=None, interaction=None,
        quota_observer=None, authority=None, capture_sources={},
        trace_prerequisites_factory=Mock(return_value=manager))
    return _SetupOwner(backend), manager, trace


def test_shared_provider_scrub_notice_reaches_its_own_capture(monkeypatch, tmp_path):
    from api_agent.providers import scrubbed_response
    captured = []
    monkeypatch.setattr(scrubbed_response._LOG, 'handlers', [])
    monkeypatch.setattr(scrubbed_response._LOG, 'level', scrubbed_response._LOG.level)
    scrubbed_response.configure_diagnostics('info')

    async def run():
        owner, _, _ = await setup_owner(monkeypatch, tmp_path, [])
        owner._stderr_sink = captured.append
        async def describe():
            return await asyncio.to_thread(scrubbed_response.scrub_response,
                {'optional': 'synthetic-secret'}, schema={}, secrets=('synthetic-secret',),
                operation='model_discovery')
        owner.provider._describe = describe
        clean, _ = await owner.describe()
        assert clean == {'optional': ''}
        await owner.aclose()

    asyncio.run(run())
    assert captured == ['Response scrubbed: operation=model_discovery fields=1 omitted_events=0\n']
    assert 'synthetic-secret' not in ''.join(captured)


def test_inference_notice_is_not_copied_to_other_tests_shared_setup(monkeypatch, tmp_path, capsys):
    from api_agent.providers import scrubbed_response
    captured = []
    monkeypatch.setattr(scrubbed_response._LOG, 'handlers', [])
    monkeypatch.setattr(scrubbed_response._LOG, 'level', scrubbed_response._LOG.level)
    scrubbed_response.configure_diagnostics('info')

    async def run():
        owner, _, _ = await setup_owner(monkeypatch, tmp_path, [])
        owner._stderr_sink = captured.append
        async def complete(request):
            return await asyncio.to_thread(scrubbed_response.scrub_response,
                {'optional': 'synthetic-secret'}, schema={}, secrets=('synthetic-secret',),
                operation='inference')
        owner.provider._complete = complete
        clean, _ = await owner.complete(object())
        assert clean == {'optional': ''}
        await owner.aclose()

    asyncio.run(run())
    assert captured == []
    assert 'Response scrubbed: operation=inference fields=1' in capsys.readouterr().err


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


@pytest.mark.parametrize('failure', [RuntimeError('not ready'), TimeoutError('private-timeout-detail'),
                                     RuntimeError('unregistered-private-detail')])
def test_failed_collector_readiness_does_not_block_inference(monkeypatch, tmp_path, capsys, failure):
    log = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        manager.acquire.side_effect = failure
        await owner.ensure_ready()
        assert await owner.complete('request') == 'Ok'
        assert await owner.complete('request') == 'Ok'
        manager.acquire.assert_called_once()
        trace.astart.assert_not_called()
        await owner.aclose()
        return owner
    owner = asyncio.run(run())
    assert log == ['provider-ready', 'inference', 'inference', 'provider-close', 'trace-flush', 'release']
    diagnostic = capsys.readouterr().err
    assert diagnostic.count('Tracing failed during collector setup') == 1
    assert str(failure) not in diagnostic
    owner.provider._ensure.assert_awaited_once()


def test_first_inference_collector_failure_skips_trace_start_without_skipping_request(monkeypatch, tmp_path, capsys):
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, [])
        manager.acquire.side_effect = RuntimeError('unregistered-private-detail')
        assert await owner.complete('request') == 'Ok'
        owner.provider._complete.assert_awaited_once_with('request')
        trace.astart.assert_not_awaited()
        await owner.aclose()
    asyncio.run(run())
    diagnostic = capsys.readouterr().err
    assert 'Tracing failed during collector setup:' in diagnostic
    assert 'unregistered-private-detail' not in diagnostic


def test_collector_failure_diagnostic_scrubs_secrets(monkeypatch, tmp_path, capsys):
    from api_agent.runtime.prerequisites import PrerequisiteError
    captured = []

    async def run():
        owner, manager, _ = await setup_owner(monkeypatch, tmp_path, [])
        owner._stderr_sink = captured.append
        owner.provider.emissions.register_secret('synthetic-collector-secret')
        manager.acquire.side_effect = PrerequisiteError(
            'Existing prerequisite stack is incompatible; synthetic-collector-secret',
            local_details='private engine diagnostic')
        await owner.ensure_ready()
        assert await owner.complete('request') == 'Ok'
        owner.provider._ensure.assert_awaited_once()
        await owner.aclose()

    asyncio.run(run())
    text = capsys.readouterr().err
    assert 'Existing prerequisite stack is incompatible' in text
    assert 'synthetic-collector-secret' not in text
    assert 'private engine diagnostic' not in text
    assert captured == [text]


def test_trace_sdk_failure_does_not_block_inference(monkeypatch, tmp_path, capsys):
    from api_agent.telemetry.tracing import TraceError

    async def run():
        owner, _, trace = await setup_owner(monkeypatch, tmp_path, [])
        trace.astart.side_effect = TraceError('Tracing initialization failed.')
        await owner.ensure_ready()
        assert await owner.complete('request') == 'Ok'
        assert await owner.complete('request') == 'Ok'
        trace.astart.assert_awaited_once()
        await owner.aclose()

    asyncio.run(run())
    assert 'Tracing failed during SDK initialization' in capsys.readouterr().err


def test_unrecordable_trace_failure_is_fatal(monkeypatch, tmp_path):
    import sys

    async def run():
        owner, manager, _ = await setup_owner(monkeypatch, tmp_path, [])
        manager.acquire.side_effect = RuntimeError('not ready')
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stderr', SimpleNamespace(write=lambda text: 0))
            with pytest.raises(OSError, match='Could not record tracing failure'):
                await owner.ensure_ready()
        owner.provider._ensure.assert_not_called()
        await owner.aclose()

    asyncio.run(run())


def test_textual_no_count_writer_retains_actionable_trace_notice_without_failing_request(monkeypatch, tmp_path):
    from textual.app import App
    from api_agent.runtime.prerequisites import PrerequisiteError
    from api_agent.runtime.prerequisite_diagnostics import incompatible_stack_message
    captured = []
    notice = incompatible_stack_message(SimpleNamespace(service='langfuse-web', profile='langfuse',
        initialization_changes=('LANGFUSE_ADMIN_PASSWORD',), id='a' * 64),
        configuration_changed=True, executable='docker', project='sa-test')

    async def run():
        async with App().run_test():
            import sys
            assert type(sys.stderr).__module__ == 'textual.app'
            owner, manager, trace = await setup_owner(monkeypatch, tmp_path, [])
            owner._stderr_sink = captured.append
            manager.acquire.side_effect = PrerequisiteError(notice)
            assert await owner.complete('request') == 'Ok'
            owner.provider._complete.assert_awaited_once_with('request')
            trace.astart.assert_not_awaited()
            await owner.aclose()
    asyncio.run(run())
    assert len(captured) == 1
    assert '!' * 72 in captured[0]
    assert 'ACTION REQUIRED:' in captured[0]
    assert 'Continuing without tracing.' in captured[0]


@pytest.mark.parametrize('captured', [False, True])
def test_short_trace_diagnostic_write_remains_fatal_with_or_without_capture(monkeypatch, tmp_path, captured):
    import sys
    retained = []
    async def run():
        owner, manager, _ = await setup_owner(monkeypatch, tmp_path, [])
        if captured:
            owner._stderr_sink = retained.append
        manager.acquire.side_effect = RuntimeError('not ready')
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stderr', SimpleNamespace(write=lambda text: 0))
            with pytest.raises(OSError, match='Could not record tracing failure'):
                await owner.ensure_ready()
        owner.provider._ensure.assert_not_called()
        await owner.aclose()
    asyncio.run(run())
    assert bool(retained) is captured


def test_no_count_writer_without_an_owned_capture_remains_unverified(monkeypatch, tmp_path):
    import sys
    async def run():
        owner, manager, _ = await setup_owner(monkeypatch, tmp_path, [])
        manager.acquire.side_effect = RuntimeError('not ready')
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stderr', SimpleNamespace(write=lambda text: None))
            with pytest.raises(OSError, match='Could not record tracing failure'):
                await owner.ensure_ready()
        owner.provider._ensure.assert_not_called()
        await owner.aclose()
    asyncio.run(run())


def test_collector_cancellation_retains_its_identity(monkeypatch, tmp_path):
    from api_agent.runtime.prerequisites import PrerequisiteCancelled

    async def run():
        owner, manager, _ = await setup_owner(monkeypatch, tmp_path, [])
        cancellation = PrerequisiteCancelled('cancelled')
        manager.acquire.side_effect = cancellation
        with pytest.raises(PrerequisiteCancelled) as caught:
            await owner.ensure_ready()
        assert caught.value is cancellation
        owner.provider._ensure.assert_not_called()
        await owner.aclose()

    asyncio.run(run())


def test_unfinished_exporter_keeps_collector_until_cleanup_retry(monkeypatch, tmp_path):
    log = []
    async def run():
        owner, manager, trace = await setup_owner(monkeypatch, tmp_path, log)
        trace.aclose.side_effect = [RuntimeError('still closing'), None]
        from api_agent.testing.trace_cleanup import OptionalTraceCleanupPending
        with pytest.raises(OptionalTraceCleanupPending):
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
        from api_agent.testing.trace_cleanup import OptionalTraceCleanupPending
        with pytest.raises(OptionalTraceCleanupPending):
            await owner.aclose()
        await owner.aclose()
        return manager, trace
    manager, trace = asyncio.run(run())
    assert manager.release.call_count == 2
    assert trace.aclose.await_count == 2


@pytest.mark.parametrize('mode,target,expected', [
    ('LangFuse', 'http://localhost:3000/api/public/otel/v1/traces', 90),
    ('OTEL', 'http://localhost:4318/v1/traces', 90),
    ('LangFuse', 'https://tracing.example.test', 5),
    ('', None, 5),
])
def test_backend_budgets_exporter_and_managed_collector_disposal(tmp_path, mode, target, expected):
    from api_agent.testing.setup_backend import SetupBackend
    config = trace_config(mode, target)
    if target is not None:
        config['TRACE_EXPORT_ENDPOINT'] = target
    backend = SetupBackend(config, tmp_path, trace_prerequisites_factory=Mock())
    assert backend.cleanup_timeout == expected
    assert backend.thread is None
