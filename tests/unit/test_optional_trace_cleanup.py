"""Actual binding failure retains cleanup independently of a passed E2E result."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.trace_cleanup import optional_trace_cleanup, OptionalTraceCleanupPending
from test_integration_runner import fixture_repo,context
from test_workflow_observations import collector


@pytest.mark.parametrize('debug',[False,True])
@pytest.mark.parametrize('failure',['trace','provider','logger','delivery'])
@pytest.mark.parametrize('signal',['traces','metrics'])
def test_actual_sdk_binding_pending_disposal_keeps_success_only_for_trace(monkeypatch,tmp_path,debug,failure,signal):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.testing.integration import run_integration_tests
    from api_agent.testing.collection import collect_tests
    state=SimpleNamespace(blocked=True)
    class Provider:
        def __init__(self,*args,**kwargs):
            self.emissions=EmissionBoundary()
            self.closed=False
        async def _close(self):
            if state.blocked and failure=='provider':raise RuntimeError('synthetic-sensitive-provider')
        async def _finish_execution(self):
            if state.blocked and failure=='provider':raise RuntimeError('synthetic-sensitive-provider')
    monkeypatch.setattr(provider_setup,'IntegrationLLM',Provider)
    made=[]
    managers=[]
    original_start=SetupBackend.start
    original_failure=_SetupOwner._trace_failure
    def trace_failure(owner,stage,error):
        if state.blocked and failure=='logger':raise OSError('synthetic logger unavailable')
        return original_failure(owner,stage,error)
    monkeypatch.setattr(_SetupOwner,'_trace_failure',trace_failure)
    def start(backend):
        result=original_start(backend)
        made.append(backend)
        async def open_trace():
            backend.registry._create_owner()
            source=backend.registry._owner
            lease=SimpleNamespace(held=True,releases=0)
            def release(**kwargs):
                if lease.held:
                    lease.held=False
                    lease.releases+=1
            manager=SimpleNamespace(release=Mock(side_effect=release),lease=lease)
            source.trace_prerequisites=manager
            managers.append(manager)
            if signal=='metrics':
                from api_agent.telemetry.tracing import TraceRuntime
                from api_agent.telemetry.tracing_providers import admit_binding
                from api_agent.testing.workflow_capture import capture_config
                settings=capture_config(backend.config,{})
                runtime=TraceRuntime(settings,boundary=source.provider.emissions,
                    binding=admit_binding(settings,owner_root=None,boundary=source.provider.emissions))
                source.trace.runtime=runtime
            await source.trace.astart()
            sdk=(source.trace.runtime._binding.handles.meter_provider.exporter if signal=='metrics'
                 else source.trace.runtime._provider)
            shutdown=sdk.shutdown
            def pending(*args,**kwargs):
                if state.blocked:raise RuntimeError('synthetic-sensitive-exporter')
                return shutdown(*args,**kwargs)
            sdk.shutdown=pending
            if failure=='delivery':
                sdk.shutdown=shutdown
                source.trace.runtime._provider.force_flush=lambda **kwargs:False
        asyncio.run_coroutine_threadsafe(open_trace(),backend.loop).result(timeout=10)
        return result
    monkeypatch.setattr(SetupBackend,'start',start)
    base=fixture_repo(tmp_path/'base')
    ctx=context(base)
    selected=collect_tests(ctx,'integration')
    published=[]
    with collector() as (endpoint,received):
        from pathlib import Path
        provider = Path(__file__).resolve().parents[2]/'src/api_agent/telemetry/trace_adapters/provider.py'
        settings={'TRACE_PROVIDER':str(provider)+':create_tracing_provider','JAEGER_VIEWER_PORT':16686,'DEBUG':debug,'TRACE_MODE':'OTEL','TRACE_EXPORT_ENDPOINT':endpoint,
                  'TRACE_METRICS':signal=='metrics','TRACE_METRICS_ENDPOINT':endpoint+'/v1/metrics'}
        try:
            if failure in ('trace','delivery'):
                result=run_integration_tests(ctx,selected,accepted=True,config=settings,after_run=published.append)
                assert result.exit_code==0 and [test.status for test in result.tests]==['passed']
                assert published
                if failure=='trace':
                    assert 'Tracing cleanup remains pending' in result.output
                    assert made[0] in result.inspections
                else:
                    assert not made[0].pending
                    assert made[0].closed
                    assert managers[0].lease.releases==1 and not managers[0].lease.held
                    return
            else:
                with pytest.raises(CleanupPendingError) as caught:
                    run_integration_tests(ctx,selected,accepted=True,config=settings,after_run=published.append)
                assert not optional_trace_cleanup(caught.value)
                assert caught.value.execution.exit_code==1 and not published
            backend=made[0]
            assert backend.pending and not backend.closed and backend.thread.is_alive()
            assert managers[0].lease.releases==0 and managers[0].lease.held
            if failure=='trace':assert optional_trace_cleanup(backend._receipt())
        finally:
            state.blocked=False
            for backend in made:backend.close()
    assert all(backend.closed and not backend.thread.is_alive() for backend in made)
    assert all(manager.lease.releases==1 and not manager.lease.held for manager in managers)


def test_custom_error_cannot_self_authorize_optional_cleanup(tmp_path):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    backend=SetupBackend({},tmp_path)
    origin=object.__new__(_SetupOwner)
    origin._nontrace_settled=origin._trace_cleanup_failed=False
    backend.registry=SimpleNamespace(_owner=origin)
    forged=OptionalTraceCleanupPending((backend,),backend.aclose,origin=origin,backend=backend)
    assert not optional_trace_cleanup(forged)
    assert not optional_trace_cleanup(backend._receipt(forged))


@pytest.mark.parametrize('reverse',[False,True])
def test_mixed_provider_failure_has_priority_over_optional_trace_cleanup(tmp_path,reverse):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.testing.setup_pool import SetupPool
    from api_agent.testing.trace_cleanup import admit_optional_trace_receipt
    backend=SetupBackend({},tmp_path)
    origin=object.__new__(_SetupOwner)
    origin._nontrace_settled=origin._trace_cleanup_failed=True
    backend.registry=SimpleNamespace(_owner=origin)
    trace=admit_optional_trace_receipt(backend,origin)
    genuine=RuntimeError('provider cleanup pending')
    def fail(error):
        def close():raise error
        return SimpleNamespace(close=close)
    pool=SetupPool({},tmp_path)
    pool.owners=[fail(trace),fail(genuine)]
    if reverse:pool.owners.reverse()
    with pytest.raises(RuntimeError) as caught:pool.close()
    assert caught.value is genuine
