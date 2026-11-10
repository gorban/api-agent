"""Joined runtime ownership regressions use real SDK and HTTP boundaries."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_ui_runtime import operation
from test_workflow_observations import config,collector,definition
from test_operation_traces import spans_from,attrs
from test_studio_retained_owner import Automated


class Lease:
    """An acknowledged release has one effect, independent of lifecycle callbacks."""
    timeout=2
    def __init__(self):
        self.held=False
        self.releases=0
        self.blocked=False
    def now(self):return 0
    def acquire(self,**kwargs):self.held=True
    def release(self,**kwargs):
        if not self.held:return
        if self.blocked:raise RuntimeError('synthetic release pending')
        self.held=False
        self.releases+=1


def test_actual_ui_owner_retains_trace_only_failed_lifespan_without_failing_fixture(operation,monkeypatch,tmp_path,caplog):
    import httpx
    from api_agent.testing import provider_setup
    from api_agent.testing.studio_http import agent_endpoint,studio_trace_pending
    from api_agent.testing.studio_tracing import StudioTraceOwner
    from api_agent.testing.workflow_capture import capture_sources
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(tmp_path/'events'))
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        trace=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),Automated(),collector_factory=lambda _:lease)
        lease=Lease()
        @asynccontextmanager
        async def backend(values):
            async with agent_endpoint(trace.provider,'sample','http://localhost:9443','x'*32,tracing=trace) as url:
                yield {'agent_url':url,'access_token':'x'*32}
        ui=operation.create()
        ui.backend_factory=backend
        loop=asyncio.new_event_loop()
        retained=None
        try:
            loop.run_until_complete(ui.start())
            url=operation.prepared_factory.call_args.args[0]['AGENT_DJANGO_URL']
            async def request():
                async with httpx.AsyncClient(timeout=60) as client:
                    response=await client.get(url+'/app/agent/models',headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32})
                    assert response.status_code==200
            loop.run_until_complete(request())
            runtime=trace._sessions['automated'].runtime
            shutdown=runtime._provider.shutdown
            runtime._provider.shutdown=Mock(side_effect=RuntimeError('synthetic pending'))
            retained=provider_setup.finish_ui_browser_inspection(loop,None,owners=(ui,),close_loop=False)
            assert retained in provider_setup._inspection_providers
            assert retained.loop is loop and retained._owners==[ui]
            assert studio_trace_pending(ui._backend_cleanup)
            assert ui._nontrace_settled and lease.held and lease.releases==0
            assert not runtime.disposal_confirmed and not trace._sessions['automated'].closed
            assert 'original UI owner and loop are retained' in caplog.text
            runtime._provider.shutdown=shutdown
            retained.close_loop=False
            retained.close()
            assert runtime.disposal_confirmed and not lease.held and lease.releases==1
            assert not ui._backend_cleanup and retained.closed
        finally:
            if retained in provider_setup._inspection_providers:provider_setup._inspection_providers.remove(retained)
            if not loop.is_closed():loop.close()


@pytest.mark.parametrize('primary',['none','error','cancel'])
def test_real_endpoint_pending_disposal_preserves_body_error_and_receipt(primary):
    import httpx
    from api_agent.testing.studio_http import agent_endpoint,studio_trace_pending
    from api_agent.testing.studio_tracing import StudioTraceOwner
    from api_agent.testing.workflow_capture import capture_sources
    from api_agent.providers.control import CleanupPendingError
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        lease=Lease()
        trace=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),Automated(),collector_factory=lambda _:lease)
        original=ValueError('body') if primary=='error' else asyncio.CancelledError() if primary=='cancel' else None
        async def run():
            receipt=None
            try:
                async with agent_endpoint(trace.provider,'sample','http://localhost:9443','x'*32,tracing=trace) as url:
                    async with httpx.AsyncClient(timeout=60) as client:
                        assert (await client.get(url+'/app/agent/models',headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32})).status_code==200
                    runtime=trace._sessions['automated'].runtime
                    shutdown=runtime._provider.shutdown
                    runtime._provider.shutdown=Mock(side_effect=RuntimeError('pending'))
                    if original is not None:raise original
            except BaseException as error:
                if original is not None:
                    assert error is original
                    receipt=error.cleanup_pending
                else:
                    assert isinstance(error,CleanupPendingError)
                    receipt=error
            assert studio_trace_pending(receipt)
            assert lease.held and lease.releases==0
            runtime._provider.shutdown=shutdown
            await receipt.retry()
            assert not lease.held and lease.releases==1 and runtime.disposal_confirmed
        asyncio.run(run())


def test_direct_sdk_cancelled_startup_retains_partial_cleanup_owner(monkeypatch):
    from api_agent.telemetry.tracing import TraceRuntime
    from opentelemetry.sdk.trace import TracerProvider
    cancellation=asyncio.CancelledError()
    monkeypatch.setattr(TracerProvider,'get_tracer',Mock(side_effect=cancellation))
    runtime=TraceRuntime(config(False,False))
    async def run():
        with pytest.raises(asyncio.CancelledError) as caught:await runtime.astart()
        assert caught.value is cancellation
        assert runtime._provider is runtime._binding.signal_owner
        assert cancellation.cleanup_owner in runtime._provider.owners
        from api_agent.telemetry.tracing import TraceDeliveryError
        with pytest.raises(TraceDeliveryError):await runtime.aclose()
        assert runtime.disposal_confirmed and cancellation.cleanup_owner.disposed
        assert runtime._binding._closed
    asyncio.run(run())

@pytest.mark.parametrize('background',[False,True])
@pytest.mark.parametrize('failed',[False,True])
@pytest.mark.parametrize('trace,log',[(False,False),(False,True),(True,False),(True,True)])
def test_inactive_wire_evidence_finalizes_fallback_once_after_early_result(monkeypatch,tmp_path,background,failed,trace,log):
    import time
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.tracing import TraceRuntime
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.inference_capture import bind_boundary,capture_inference,capture_transport_diagnostics
    from api_agent.telemetry.trace_ownership import settle_trace_ownership
    from api_agent.providers.llm import LLMResult,Message,TextContent,Usage
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.generation_attempts import begin_transport_attempt,finish_transport_attempt
    from api_agent.testing.authority_scope import authority_scope
    from test_trace_ownership_transport import request
    from test_setup_transport import Registry,CAPABILITY
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(tmp_path/'events'))
    class Inactive(Registry):
        async def complete(self,binding,value):
            bind_boundary(EmissionBoundary(ContentPolicy(True)));settle_trace_ownership()
            token=begin_transport_attempt(1)
            capture_transport_diagnostics({'version':1,'response_id':'native-response','finish_reasons':['stop'],'max_output_tokens':321})
            capture_inference('output',{'text':'visible native answer'},'synthetic')
            result=LLMResult(Message('assistant',(TextContent('visible native answer'),)),Usage(7,2))
            if failed:
                error=ProviderAccessError('unavailable','synthetic failure',usage=Usage(7,2))
                finish_transport_attempt(token,error=error)
                raise error
            finish_transport_attempt(token,result=result)
            return result
    with collector() as (target,received):
        settings=config(log,trace,TRACE_EXPORT_ENDPOINT=target)
        runtime=TraceRuntime(settings,model_identities=('synthetic-model',))
        async def run():
            await runtime.astart()
            adapter=WorkflowObservationFactory(settings,boundary=runtime.boundary,trace=runtime).create(definition())
            adapter.observe(dict(event='scope_start',scope_id='visit',owner='Make',kind='node'))
            server=SetupServer(Inactive(),CAPABILITY,timeout=5,**({'authority':EmissionBoundary(ContentPolicy(True))} if background else {}))
            remote=RemoteIntegrationLLM(await server.start())
            entered,release=asyncio.Event(),asyncio.Event()
            publish=server._publish_evidence
            async def delayed(*args,**kwargs):
                entered.set()
                await release.wait()
                return await publish(*args,**kwargs)
            if background:server._publish_evidence=delayed
            try:
                with authority_scope(('workflow',)) as scope,bind_workflow_visit(adapter,'visit'):
                    task=asyncio.create_task(asyncio.to_thread(remote.complete,request()))
                    if background:
                        await asyncio.wait_for(entered.wait(),3)
                        if failed:
                            with pytest.raises(ProviderAccessError):await asyncio.wait_for(task,3)
                        else:await asyncio.wait_for(task,3)
                        terminal=time.time_ns()
                        assert len(adapter.journal._call_ids)==1
                        assert not adapter._call_fields and not adapter._call_payloads
                        release.set()
                    else:
                        if failed:
                            with pytest.raises(ProviderAccessError):await task
                        else:await task
                        terminal=time.time_ns()
                    await asyncio.to_thread(scope.finish_evidence)
                assert not adapter._call_fields and not adapter._call_payloads
                assert adapter._remote_pending==0
            finally:
                release.set();await asyncio.to_thread(remote.close);await server.aclose()
            adapter.observe(dict(event='scope_end',scope_id='visit',outcome='passed'))
            adapter.close();await runtime.aclose()
            return terminal
        terminal=asyncio.run(run())
    calls=[span for span in spans_from(received) if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(calls)==1
    fields=attrs(calls[0])
    assert fields['gen_ai.response.id']=={'stringValue':'native-response'}
    assert fields['gen_ai.request.max_tokens']=={'intValue':'321'}
    assert calls[0].end_time_unix_nano<=terminal
    assert ('langfuse.observation.output' in fields)==trace
    assert ('gen_ai.output.messages' in fields)==trace


def test_trace_lease_release_failure_retains_effect_until_explicit_retry():
    from api_agent.testing.studio_tracing import _TraceSession,StudioTraceCleanupPending
    from unittest.mock import AsyncMock
    lease=Lease()
    runtime=SimpleNamespace(astart=AsyncMock(),aclose=AsyncMock())
    session=_TraceSession(runtime,lease)
    async def run():
        await session.start()
        lease.blocked=True
        with pytest.raises(StudioTraceCleanupPending):await session.aclose()
        assert lease.held and lease.releases==0 and not session.closed
        lease.blocked=False
        await session.aclose();await session.aclose()
        assert not lease.held and lease.releases==1 and session.closed
    asyncio.run(run())
