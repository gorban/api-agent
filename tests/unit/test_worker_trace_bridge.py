"""Bounded admission never authorizes a second local generation on uncertainty."""
import asyncio
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.telemetry.tracing_providers import admit_binding
from api_agent.telemetry.operation import operation
from api_agent.workflows.observations import WorkflowObservationFactory
from api_agent.testing.provider_setup import _trace_publish
from test_workflow_observations import collector, config, definition
from test_operation_traces import spans_from, attrs


@pytest.mark.parametrize('state', ['granted', 'inactive', None])
def test_exact_owner_call_receipt_and_closed_receipts(state):
    from api_agent.telemetry.worker_trace import WorkerTraceOffer, ownership, _MISSING
    class Loss:
        count=0
        def record(self): self.count+=1
    owner=type('Owner',(),{'trace_losses':Loss()})()
    released=[]
    receipt=WorkerTraceOffer(owner,'one',{},lambda:released.append(True))
    receipt.receipt(state)
    with receipt.completion():
        assert ownership(object(),'one') is _MISSING
        assert ownership(owner,'two') is _MISSING
        assert ownership(owner,'one')==state
        assert ownership(owner,'one')==state
    assert released==[True]
    assert owner.trace_losses.count==int(state is None)
    receipt.receipt('inactive')
    assert receipt.state==state
    receipt.close()
    with receipt.completion(): assert ownership(owner,'one') is _MISSING
    assert released==[True]


@pytest.mark.parametrize('saturated', [False, True])
def test_actual_sdk_unknown_handoff_never_duplicates_generation(saturated):
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.remote_inference import RemoteInferenceTrace
    from test_trace_ownership_transport import OwnershipRegistry, request
    from test_setup_transport import CAPABILITY
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        boundary=EmissionBoundary(ContentPolicy(False))
        binding=admit_binding(settings,owner_root=None,boundary=boundary)
        runtime=TraceRuntime(settings,boundary=boundary,binding=binding,model_identities=('synthetic-model',))
        metrics=[]
        binding.record_call=lambda *args,**kwargs:metrics.append((args,kwargs))
        async def run():
            await runtime.astart()
            remote=RemoteInferenceTrace(settings,EmissionBoundary(ContentPolicy(False)))
            await remote.astart()
            registry=OwnershipRegistry(remote)
            server=SetupServer(registry,CAPABILITY,timeout=5)
            client=SetupClient(await server.start(),timeout=5)
            adapter=WorkflowObservationFactory(settings,boundary=boundary,trace=runtime).create(definition())
            pending=[]
            try:
                with operation(runtime):
                    adapter.observe(dict(event='scope_start',scope_id='graph',owner='GRAPH',kind='graph'))
                    adapter.observe(dict(event='scope_start',scope_id='visit',parent_id='graph',owner='Make',kind='node'))
                    if saturated:
                        pending=[adapter.remote_offer(str(uuid4()),'visit') for _ in range(256)]
                        assert adapter._remote_pending==256
                    call_id=str(uuid4())
                    offer=adapter.remote_offer(call_id,'visit')
                    assert bool(offer.kwargs) is not saturated
                    result=await asyncio.to_thread(client.complete,request(),observation_call_id=call_id,**offer.kwargs)
                    assert client.trace_ownership==('granted' if not saturated else None)
                    # Missing or lost ACK is never promoted to explicit inactive.
                    offer.receipt(None)
                    _trace_publish(offer,lambda:adapter.record_call(call_id=call_id,visit_id='visit',
                        selection='synthetic-model',usage={'input_tokens':7,'output_tokens':3},
                        transport_attempts=client.transport_attempts,outcome='completed',elapsed=.01))
                    assert adapter._remote_pending==(256 if saturated else 0)
                    if saturated:
                        gate=asyncio.Semaphore(8)
                        async def complete_pending(item,index):
                            async with gate:
                                queued=SetupClient(client._descriptor,worker_id='pending-'+str(index),timeout=5)
                                try:
                                    await asyncio.to_thread(queued.complete,request(),
                                        observation_call_id=item.call_id,**item.kwargs)
                                    item.receipt(queued.trace_ownership)
                                    assert item.state=='granted'
                                    _trace_publish(item,lambda:adapter.record_call(call_id=item.call_id,visit_id='visit',
                                        selection='synthetic-model',usage={'input_tokens':7,'output_tokens':3},
                                        transport_attempts=queued.transport_attempts,outcome='completed',elapsed=.01))
                                finally:
                                    await asyncio.to_thread(queued.close)
                        await asyncio.gather(*(complete_pending(item,index) for index,item in enumerate(pending)))
                        assert adapter._remote_pending==0
                    adapter.observe(dict(event='scope_end',scope_id='visit',outcome='passed'))
                    adapter.observe(dict(event='scope_end',scope_id='graph',outcome='passed'))
                assert runtime.losses.count==1
            finally:
                for item in pending:item.close()
                assert adapter._remote_pending==0
                adapter.close()
                await asyncio.to_thread(client.close)
                await server.aclose()
                await remote.aclose()
                await runtime.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    generations=[span for span in spans if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(generations)==(257 if saturated else 1)
    assert len(metrics)==(257 if saturated else 1)
    request_span=next(span for span in spans if span.name=='request')
    assert attrs(request_span)['api_agent.trace.complete']=={'boolValue':False}
    assert attrs(request_span)['api_agent.trace.loss_count']=={'intValue':'1'}
    if saturated:
        assert sum(not span.parent_span_id for span in generations)==1  # Honest orphan on rejected offer.
    else:
        node=next(span for span in spans if span.name=='node Make')
        assert generations[0].parent_span_id==node.span_id


def test_disabled_owner_does_not_create_receipts_or_parent_context():
    settings=config(mode='')
    runtime=TraceRuntime(settings)
    adapter=WorkflowObservationFactory(settings,boundary=runtime.boundary,trace=runtime).create(definition())
    for index in range(257):
        assert adapter.remote_offer(str(uuid4()),'missing') is None
    assert adapter._remote_pending==0 and runtime.losses.count==0
    assert not adapter._spans


@pytest.mark.parametrize('source_first', [False,True])
def test_final_dispatch_limit_wins_without_rewriting_previous_attempt(source_first):
    from api_agent.providers.generation_attempts import TransportAttemptLedger
    from api_agent.providers.control import OperationalError
    from api_agent.providers.llm import LLMRequest,CompletionRequest,GenerationOptions,Message,TextContent,LLMResult,Usage
    from api_agent.workflows.attribution import bind_workflow_visit
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        runtime=TraceRuntime(settings,model_identities=('sample',))
        request=LLMRequest('sample',CompletionRequest((Message('user',(TextContent('question'),)),),
            generation=GenerationOptions(max_output_tokens=200000)))
        result=LLMResult(Message('assistant',(TextContent('answer'),)),Usage(1,2))
        ledger=TransportAttemptLedger(2)
        initial=ledger.begin(1)
        ledger.finish(initial,error=OperationalError('network'),retry_delay_seconds=0)
        final=ledger.begin(2)
        ledger.finish(final,result=result)
        async def run():
            await runtime.astart()
            adapter=WorkflowObservationFactory(settings,boundary=runtime.boundary,trace=runtime).create(definition())
            adapter.observe(dict(event='scope_start',scope_id='visit',owner='Make',kind='node'))
            def source():
                with bind_workflow_visit(adapter,'visit'):
                    adapter.provider_fields('cause',final,request,result)
            if source_first:source()
            # Deliberately insert final facts before earlier dispatch facts.
            adapter.provider_diagnostics('cause',final,{'version':1,'max_output_tokens':512})
            adapter.provider_diagnostics('cause',initial,{'version':1,'max_output_tokens':1024})
            if not source_first:source()
            adapter.record_call(call_id=str(uuid4()),visit_id='visit',selection='sample',
                usage={'input_tokens':1,'output_tokens':2},outcome='completed',transport_attempts=ledger.snapshot())
            adapter.observe(dict(event='scope_end',scope_id='visit',outcome='passed'))
            adapter.close()
            await runtime.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    generation=next(span for span in spans if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'})
    assert attrs(generation)['gen_ai.request.max_tokens']=={'intValue':'512'}
    physical=sorted((span for span in spans if span.name=='transport attempt'),key=lambda span:int(attrs(span)['api_agent.transport_index']['intValue']))
    assert [attrs(span)['gen_ai.request.max_tokens'] for span in physical]==[{'intValue':'1024'},{'intValue':'512'}]
    assert 'gen_ai.response.finish_reasons' not in attrs(generation)


@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
def test_host_shared_provider_source_fields_survive_authenticated_handoff(trace,log):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.setup_transport import SetupClient,SetupServer
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.llm import LLMResult,Message,TextContent,Usage
    from api_agent.telemetry.remote_inference import RemoteInferenceTrace
    from api_agent.telemetry.trace_ownership import settle_trace_ownership
    from api_agent.workflows.inference_capture import bind_boundary,bind_inference_trace,capture_inference
    from test_trace_ownership_transport import request
    from test_setup_transport import Registry,CAPABILITY
    with collector() as (target,received):
        settings=config(log,trace,TRACE_EXPORT_ENDPOINT=target)
        boundary=EmissionBoundary(ContentPolicy(log),secrets=['synthetic-credential'])
        worker=TraceRuntime(settings,boundary=boundary,
            binding=admit_binding(settings,owner_root=None,boundary=boundary))
        host=RemoteInferenceTrace(settings,boundary)
        async def authenticate(callback):return await callback(auth='synthetic-credential')
        async def physical(request,*,auth):
            capture_inference('input',{'provider_payload':{'messages':[{'role':'user','content':[{'text':'visible prompt'}]}],
                'inferenceConfig':{'maxTokens':512}}},'native',auth=auth)
            capture_inference('output',{'provider_response':{'id':'response-one',
                'stopReason':'end_turn','output':{'message':{'role':'assistant','content':[{'text':'visible answer'}]}}}},'native',auth=auth)
            return LLMResult(Message('assistant',(TextContent('visible answer'),)),Usage(12,7,4,2,1),actual_model='served-v2')
        class SourceRegistry(Registry):
            async def complete(self,binding,request):
                bind_boundary(boundary)
                bind_inference_trace(host)
                settle_trace_ownership()
                return await source.complete(request)
            def quote_cost(self,*args):return None
        async def run():
            nonlocal source
            source=SharedSetupOwner('source','source',lambda _:SimpleNamespace(call=authenticate,aclose=AsyncMock()),
                complete=physical,captures_effective_payload=True,telemetry_provider_name='independent.provider')
            await worker.astart();await host.astart()
            server=SetupServer(SourceRegistry(),CAPABILITY,timeout=5)
            client=SetupClient(await server.start(),timeout=5)
            node=worker.begin('node worker',{})
            try:
                await asyncio.to_thread(client.complete,request(),observation_call_id=str(uuid4()),
                    observation_parent=node.export_context(),observation_pipeline=worker.pipeline_id)
                assert client.trace_ownership=='granted'
            finally:
                await asyncio.to_thread(client.close)
                await server.aclose();await source.aclose()
                node.end();await host.aclose();await worker.aclose()
        source=None
        asyncio.run(run())
    generations=[span for span in spans_from(received) if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(generations)==1
    fields=attrs(generations[0])
    for name,value in [('input_tokens',12),('output_tokens',7),('cache_read.input_tokens',4),
                       ('cache_write.input_tokens',1),('reasoning.output_tokens',2)]:
        assert fields['gen_ai.usage.'+name]=={'intValue':str(value)}
    assert fields['gen_ai.provider.name']=={'stringValue':'independent.provider'}
    assert fields['gen_ai.response.model']=={'stringValue':'served-v2'}
    assert fields['gen_ai.request.max_tokens']=={'intValue':'512'}
    assert fields['gen_ai.response.id']=={'stringValue':'response-one'}
    assert fields['gen_ai.response.finish_reasons']=={'arrayValue':{'values':[{'stringValue':'end_turn'}]}}
    for direction in ('input','output'):
        assert ('gen_ai.'+direction+'.messages' in fields) is trace
        assert ('langfuse.observation.'+direction in fields) is trace
    assert 'synthetic-credential' not in repr(received)


@pytest.mark.parametrize('remote',[False,True])
@pytest.mark.parametrize('known_failed',[False,True])
@pytest.mark.parametrize('trace,log',[(False,False),(False,True),(True,False),(True,True)])
def test_serialized_standard_usage_counts_attempts_once(remote,known_failed,trace,log,monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import LLMResult,Message,TextContent,Usage
    from api_agent.providers.generation_attempts import TransportAttemptLedger,bind_transport_attempt_ledger
    from api_agent.providers import inference_retry
    from api_agent.providers.setup_transport import SetupClient,SetupServer
    from api_agent.telemetry.remote_inference import RemoteInferenceTrace
    from api_agent.telemetry.trace_ownership import settle_trace_ownership
    from api_agent.workflows.inference_capture import bind_boundary,bind_inference_trace
    from api_agent.workflows.attribution import bind_workflow_visit
    from test_trace_ownership_transport import request
    from test_setup_transport import Registry,CAPABILITY
    monkeypatch.setattr(inference_retry,'retry_delay',lambda *_:0)
    with collector() as (target,received):
        settings=config(log,trace,TRACE_EXPORT_ENDPOINT=target)
        boundary=EmissionBoundary(ContentPolicy(log))
        runtime=TraceRuntime(settings,boundary=boundary,model_identities=('synthetic-model',),
            binding=admit_binding(settings,owner_root=None,boundary=boundary))
        host=RemoteInferenceTrace(settings,boundary) if remote else None
        calls=0
        async def authenticate(callback):return await callback(auth='synthetic-auth')
        async def physical(value,*,auth):
            nonlocal calls
            calls+=1
            if calls==1:
                failure=ProviderAccessError('network','synthetic retry',
                    usage=Usage(5,0) if known_failed else None)
                failure.execution='possibly_executed'
                raise failure
            return LLMResult(Message('assistant',(TextContent('visible answer'),)),Usage(7,0))
        source=SharedSetupOwner('source','source',lambda _:SimpleNamespace(call=authenticate,aclose=AsyncMock()),
            complete=physical,max_attempts=2)
        class SourceRegistry(Registry):
            async def complete(self,binding,value):
                bind_boundary(boundary);bind_inference_trace(host);settle_trace_ownership()
                return await source.complete(value)
            def quote_cost(self,*args):return None
        async def run():
            await runtime.astart()
            adapter=WorkflowObservationFactory(settings,boundary=boundary,trace=runtime).create(definition())
            adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
            if remote:
                await host.astart()
                server=SetupServer(SourceRegistry(),CAPABILITY,timeout=5)
                client=SetupClient(await server.start(),timeout=5)
                call_id=str(uuid4());offer=adapter.remote_offer(call_id,'visit')
                try:
                    result=await asyncio.to_thread(client.complete,request(),observation_call_id=call_id,**offer.kwargs)
                    offer.receipt(client.trace_ownership)
                    _trace_publish(offer,lambda:adapter.record_call(call_id=call_id,visit_id='visit',
                        selection='synthetic-model',usage={'input_tokens':7,'output_tokens':0},outcome='completed',
                        transport_attempts=client.transport_attempts))
                finally:
                    await asyncio.to_thread(client.close);await server.aclose();await host.aclose()
            else:
                ledger=TransportAttemptLedger(2)
                with bind_workflow_visit(adapter,'visit'),bind_transport_attempt_ledger(ledger):
                    result=await source.complete(request())
                adapter.record_call(call_id=str(uuid4()),visit_id='visit',selection='synthetic-model',
                    usage={'input_tokens':7,'output_tokens':0},outcome='completed',transport_attempts=ledger.snapshot())
            assert result.usage.input_tokens==7
            adapter.observe(dict(event='scope_end',scope_id='visit',outcome='passed'))
            adapter.close();await source.aclose();await runtime.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    generations=[span for span in spans if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(generations)==1
    physical=[span for span in spans if span.name=='transport attempt']
    assert len(physical)==2 and all(not key.startswith('gen_ai.usage.') for span in physical for key in attrs(span))
    fields=attrs(generations[0])
    assert fields['api_agent.usage.complete']=={'boolValue':known_failed}
    if known_failed:
        assert fields['gen_ai.usage.input_tokens']=={'intValue':'12'}
        assert sum(int(attrs(span).get('gen_ai.usage.input_tokens',{}).get('intValue',0)) for span in spans)==12
    else:
        assert 'gen_ai.usage.input_tokens' not in fields
    assert sorted(int(attrs(span)['api_agent.transport.usage.input_tokens']['intValue'])
        for span in physical if 'api_agent.transport.usage.input_tokens' in attrs(span))==([5,7] if known_failed else [7])

@pytest.mark.parametrize('remote',[False,True])
@pytest.mark.parametrize('settled',[False,True])
def test_serialized_cancellation_and_unknown_terminal_are_not_sdk_errors(remote,settled):
    from api_agent.providers.generation_attempts import TransportAttemptLedger
    from api_agent.telemetry.remote_inference import RemoteTraceCall
    ledger=TransportAttemptLedger(1)
    token=ledger.begin(1)
    if settled:
        ledger.finish(token,error=asyncio.CancelledError())
    else:
        ledger.mark_incomplete()
    evidence=ledger.snapshot()
    if settled:
        assert evidence['attempts'][0]['error_kind']=='cancelled'
    with collector() as (target,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=target)
        runtime=TraceRuntime(settings,boundary=EmissionBoundary(ContentPolicy(False)))
        async def run():
            await runtime.astart()
            if remote:
                call=RemoteTraceCall(runtime,str(uuid4()))
                call.publish([],evidence)
                call.end(cancelled=True)
            else:
                adapter=WorkflowObservationFactory(settings,boundary=runtime.boundary,trace=runtime).create(definition())
                adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
                adapter.record_call(call_id=str(uuid4()),visit_id='visit',selection='synthetic-model',
                    outcome='cancelled',transport_attempts=evidence)
                adapter.observe(dict(event='scope_end',scope_id='visit',outcome='cancelled'))
                adapter.close()
            await runtime.aclose()
        asyncio.run(run())
    physical=[span for span in spans_from(received) if span.name=='transport attempt']
    assert len(physical)==1
    assert physical[0].status.code==0
    assert 'error.type' not in attrs(physical[0])
    assert attrs(physical[0])['api_agent.transport.terminal_known']=={'boolValue':settled}
    if settled:
        assert physical[0].end_time_unix_nano==evidence['attempts'][0]['stopped_ms']*1_000_000
    else:
        assert attrs(physical[0])['api_agent.trace.complete']=={'boolValue':False}
