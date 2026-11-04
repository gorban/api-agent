"""Real parsed applications serialize complete native observations through OTLP."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from api_agent.api.application import Application
from api_agent.providers.control import OperationContext
from api_agent.providers.tools import ToolResult
from api_agent.telemetry.outcomes import (ExecutionOutcome, OutcomeState, OutcomeCause,
    report_outcome, current_operation)
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import collector, config
from test_demo_application import application, request, Owner, LLM, verdict


def attrs(span):
    from google.protobuf.json_format import MessageToDict
    return {a.key: MessageToDict(a.value) for a in span.attributes}


def spans_from(received):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    return [span for _, raw in received for resource in
        ExportTraceServiceRequest.FromString(raw).resource_spans
        for scope in resource.scope_spans for span in scope.spans]


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
@pytest.mark.parametrize('status', ['success', 'partial', 'failure'])
def test_parsed_request_named_tool_and_typed_returned_outcome(mode, trace, log, status):
    with collector() as (endpoint, received):
        settings = config(log, trace, mode, TRACE_EXPORT_ENDPOINT=endpoint + ('/api/public/otel/v1/traces' if mode == 'LangFuse' else ''),
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(log), secrets=['distinct-credential'])
        runtime = TraceRuntime(settings, boundary=boundary, model_identities=('chosen-model',))
        class ToolOwner(Owner):
            async def invoke(self, prepared):
                return ToolResult(status, {'answer': 'visible distinct-credential'},
                    ({'message': 'synthetic rejection'},) if status != 'success' else ())
        app = application(ToolOwner())
        app.observations = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
        model = LLM([json.dumps({'use_tool': True,
            'arguments': {'query': '{ item { name } }'}, 'answer': ''}), verdict(), 'visible final'])
        async def handler(scope, receive, send):
            result = await app.complete(request(), llm=model, context=OperationContext('request'))
            assert result.payload.content[0].text == 'visible final'
            await send({'type':'http.response.start', 'status':200, 'headers':[]})
            await send({'type':'http.response.body', 'body':b'complete'})
        asgi = Application(SimpleNamespace(tracing=runtime, services={}), handler)
        async def run():
            await runtime.astart()
            await asgi({'type':'http'}, lambda: None, lambda _: asyncio.sleep(0))
            await runtime.aclose()
        asyncio.run(run())
    spans = spans_from(received)
    root = next(s for s in spans if s.name == 'request')
    graph = next(s for s in spans if s.name == 'graph GRAPH')
    tool = [s for s in spans if attrs(s).get('gen_ai.operation.name') == {'stringValue':'execute_tool'}]
    assert len(tool) == 1
    tool = tool[0]
    assert graph.parent_span_id == root.span_id
    node = next(s for s in spans if s.span_id == tool.parent_span_id)
    assert node.name == 'node execute_tool'
    assert tool.trace_id == root.trace_id
    assert root.start_time_unix_nano <= graph.start_time_unix_nano <= tool.start_time_unix_nano
    assert tool.end_time_unix_nano <= graph.end_time_unix_nano <= root.end_time_unix_nano
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue':status}
    assert root.status.code == (0 if status == 'success' else 2)
    assert graph.status.code == root.status.code
    assert tool.status.code == root.status.code
    assert attrs(tool)['langfuse.observation.type'] == {'stringValue':'tool'}
    assert 'gen_ai.tool.name' in attrs(tool)
    for phase in ('input','output'):
        assert ('langfuse.observation.'+phase in attrs(tool)) is trace
    wire = repr(received)
    assert 'distinct-credential' not in wire
    assert ('visible final' in wire) is trace
    assert current_operation() is None


def shared_model(tmp_path, boundary, runtime, outputs, *, retry=False):
    from unittest.mock import AsyncMock
    from api_agent.runtime.local_llm import LocalLLM
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.control import OperationalError
    model = LLM(outputs)
    class Auth:
        async def __call__(self):
            await implementation.authority.aregister_secret('distinct-credential')
            return 'distinct-credential'
        async def aclose(self):
            pass
    class Provider:
        credential_protection_version = 1
        capabilities = ProviderCapabilities(kind='llm')
        retried = False
        def bind_credential_authority(self, authority):
            self.authority = authority
        async def asetup(self, **options):
            return Auth()
        async def adescribe(self, *, auth):
            return (AvailableSelection('chosen-model'),)
        async def acomplete(self, value, *, auth):
            if retry and not self.retried:
                self.retried = True
                raise OperationalError('network')
            result = await model.complete(value, context=None)
            if value.payload.response is not None:
                from dataclasses import replace
                from api_agent.llm_providers.response_policy import resolve_response
                _, evidence = resolve_response(value, lambda _: None)
                result = replace(result, response=evidence)
            return result
    implementation = Provider()
    loaded = SimpleNamespace(implementation=implementation, astart=AsyncMock(), aclose=AsyncMock())
    owner = LocalLLM({'LLM_PROVIDER':'synthetic', 'LLM_ALLOWED_MODELS':'chosen-model'},
        tmp_path, loader=AsyncMock(return_value=loaded), emissions=boundary, trace=runtime)
    return owner


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
def test_real_selected_provider_retry_has_native_llm_panels_and_one_usage(tmp_path, monkeypatch, mode, trace, log):
    from api_agent.providers import inference_retry
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda *_: 0)
    with collector() as (endpoint, received):
        settings = config(log, trace, mode, TRACE_EXPORT_ENDPOINT=endpoint + ('/api/public/otel/v1/traces' if mode == 'LangFuse' else ''),
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(log))
        runtime = TraceRuntime(settings, boundary=boundary, model_identities=('chosen-model',))
        app = application(Owner())
        app.observations = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
        model = shared_model(tmp_path, boundary, runtime, [
            json.dumps({'use_tool':False, 'arguments':{}, 'answer':'visible answer'}),
            verdict(), 'visible final'], retry=True)
        async def handler(scope, receive, send):
            await app.complete(request(), llm=model, context=OperationContext('request'))
            await send({'type':'http.response.body', 'body':b'complete'})
        asgi = Application(SimpleNamespace(tracing=runtime, services={}), handler)
        async def run():
            await runtime.astart()
            await asgi({'type':'http'}, lambda: None, lambda _: asyncio.sleep(0))
            await model.aclose()
            await runtime.aclose()
        asyncio.run(run())
    spans = spans_from(received)
    root = next(s for s in spans if s.name == 'request')
    generations = [s for s in spans if attrs(s).get('gen_ai.operation.name') == {'stringValue':'chat'}]
    attempts = [s for s in spans if s.name == 'transport attempt']
    assert len(generations) == 3
    assert len(model.usage) == 3
    assert len(attempts) == 4
    assert len([s for s in attempts if s.status.code == 2]) == 1
    assert root.status.code == 0
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue':'success'}
    assert all(s.status.code == 0 and s.trace_id == root.trace_id for s in generations)
    for generation in generations:
        fields = attrs(generation)
        assert fields['langfuse.observation.type'] == {'stringValue':'generation'}
        physical = [child for child in attempts if child.parent_span_id == generation.span_id]
        # This fixture explicitly reports the failed pre-dispatch attempt as
        # not_started; it cannot add unknown executed usage to the final total.
        assert fields['gen_ai.usage.input_tokens'] == {'intValue':'2'}
        assert fields['gen_ai.usage.output_tokens'] == {'intValue':'3'}
        assert all(not key.startswith('gen_ai.usage.') for child in physical for key in attrs(child))
        for phase in ('input', 'output'):
            assert ('langfuse.observation.'+phase in fields) is trace
            assert ('gen_ai.'+phase+'.messages' in fields) is trace
    assert 'distinct-credential' not in repr(received)


from test_workflow_stateful import fixture_graph


@pytest.mark.parametrize('terminal', [OutcomeState.SUCCESS, OutcomeState.FAILURE])
def test_stateful_real_parsed_graph_joins_request_and_explicit_outcome(fixture_graph, terminal):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_definition, load_stateful_catalog
    from langgraph.graph.message import add_messages
    root_path, entry = fixture_graph
    definition = load_stateful_definition(entry, base_root=root_path, pack_root=root_path)
    outcome = ExecutionOutcome(terminal, OutcomeCause.TOOL if terminal == OutcomeState.FAILURE else None)
    with collector() as (endpoint, received):
        settings = config(False, True, TRACE_EXPORT_ENDPOINT=endpoint)
        boundary = EmissionBoundary()
        runtime = TraceRuntime(settings, boundary=boundary)
        factory = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
        workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition),
            reducers={'messages':add_messages}, services={'observation_factory':factory,
                'execution_outcome':lambda:outcome})
        async def handler(scope, receive, send):
            await workflow.arun({'messages':[], 'pending':False, 'visits':0}, thread_id='synthetic')
        app = Application(SimpleNamespace(tracing=runtime, services={}), handler)
        async def run():
            await runtime.astart()
            await app({'type':'http'}, lambda:None, lambda _:asyncio.sleep(0))
            await runtime.aclose()
        asyncio.run(run())
    spans = spans_from(received)
    request_span = next(s for s in spans if s.name == 'request')
    graph = next(s for s in spans if s.name == 'graph GRAPH')
    nodes = [s for s in spans if s.name.startswith('node ')]
    assert len(nodes) == 2
    assert graph.parent_span_id == request_span.span_id
    assert all(s.parent_span_id == graph.span_id for s in nodes)
    assert request_span.status.code == graph.status.code == (2 if terminal == OutcomeState.FAILURE else 0)
    assert nodes[0].end_time_unix_nano <= nodes[1].start_time_unix_nano
    assert all(s.start_time_unix_nano <= s.end_time_unix_nano for s in spans)


@pytest.mark.parametrize('ending', ['success','failure','cancelled','disconnect'])
def test_request_scope_lasts_through_actual_stream_body_and_cleanup(ending):
    from api_agent.telemetry.outcomes import report_outcome
    observed = []
    with collector() as (endpoint, received):
        runtime = TraceRuntime(config(False,False,TRACE_EXPORT_ENDPOINT=endpoint))
        async def receive():
            return {'type':'http.disconnect'}
        async def handler(scope, receive, send):
            await send({'type':'http.response.start','status':200,'headers':[]})
            async def cleanup():
                observed.append('cleanup')
                assert current_operation(runtime) is not None
            scope['api_agent_http_cleanup'].append(cleanup)
            async def bodies():
                yield b'first'
                await asyncio.sleep(0)
                if ending == 'failure':
                    raise ValueError('private exception distinct-credential')
                if ending == 'cancelled':
                    raise asyncio.CancelledError()
                if ending == 'disconnect':
                    await receive()
                else:
                    report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=runtime)
                yield b'last'
            async for body in bodies():
                assert current_operation(runtime) is not None
                observed.append(body)
                await send({'type':'http.response.body','body':body,'more_body':True})
        app = Application(SimpleNamespace(tracing=runtime, services={}),handler)
        async def run():
            await runtime.astart()
            if ending in ('failure','cancelled'):
                with pytest.raises(ValueError if ending == 'failure' else asyncio.CancelledError):
                    await app({'type':'http'},receive,lambda _:asyncio.sleep(0))
            else:
                await app({'type':'http'},receive,lambda _:asyncio.sleep(0))
            await runtime.aclose()
        asyncio.run(run())
    spans = spans_from(received)
    assert len(spans) == 1
    root = spans[0]
    expected = 'cancelled' if ending in ('cancelled','disconnect') else ending
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue':expected}
    assert root.status.code == (2 if ending == 'failure' else 0)
    assert observed[-1] == 'cleanup'
    assert 'private exception' not in repr(received)


@pytest.mark.parametrize('ending', ['before_done', 'after_done', 'late_failure',
                                   'during_done_cancel', 'during_done_failure'])
def test_studio_terminal_frame_preserves_success_after_normal_client_close(ending):
    from test_studio_bridge import setup_bridge, request as http_request, payload
    with collector() as (endpoint, received):
        runtime = TraceRuntime(config(False, False, TRACE_EXPORT_ENDPOINT=endpoint))
        backend, _ = setup_bridge()
        backend.owner.trace = runtime
        frames = []
        sent = []
        async def receive():
            return {'type': 'http.disconnect'}
        async def send(message):
            body = message['body']
            if body.startswith(b'event: done'):
                if ending == 'during_done_cancel':
                    raise asyncio.CancelledError()
                if ending == 'during_done_failure':
                    raise OSError('synthetic send failure')
            sent.append(body)
        async def handler(scope, receive, send):
            response = await backend.handle(http_request('POST', payload()), 'chat_stream')
            try:
                async for part in response.streaming_content:
                    frames.append(part)
                    await send({'type': 'http.response.body', 'body': part, 'more_body': True})
                    if ending == 'before_done' and part.startswith(b'event: token'):
                        await receive()
                        break
                if ending != 'before_done':
                    await receive()
                    if ending == 'late_failure':
                        raise ValueError('synthetic late failure')
            finally:
                await backend.aclose()
        app = Application(SimpleNamespace(tracing=runtime, services={}), handler)
        async def run():
            await runtime.astart()
            try:
                if ending == 'late_failure':
                    with pytest.raises(ValueError, match='synthetic late failure'):
                        await app({'type': 'http'}, receive, send)
                elif ending == 'during_done_cancel':
                    with pytest.raises(asyncio.CancelledError):
                        await app({'type': 'http'}, receive, send)
                elif ending == 'during_done_failure':
                    with pytest.raises(OSError, match='synthetic send failure'):
                        await app({'type': 'http'}, receive, send)
                else:
                    await app({'type': 'http'}, receive, send)
            finally:
                await runtime.aclose()
        asyncio.run(run())
    root = next(s for s in spans_from(received) if s.name == 'request')
    expected = {'before_done': 'cancelled', 'after_done': 'success', 'late_failure': 'failure',
                'during_done_cancel': 'cancelled', 'during_done_failure': 'failure'}[ending]
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue': expected}
    assert root.status.code == (2 if ending in ('late_failure', 'during_done_failure') else 0)
    assert any(frame.startswith(b'event: done') for frame in frames) == (ending != 'before_done')
    assert any(frame.startswith(b'event: done') for frame in sent) == (ending in ('after_done', 'late_failure'))


@pytest.mark.parametrize('ending', ['success', 'cancel', 'failure'])
def test_django_terminal_send_cannot_hide_request_cancellation(ending):
    from api_agent.api import Runtime, create_application
    from test_studio_bridge import setup_bridge, payload, ORIGIN, NONCE
    with collector() as (endpoint, received):
        trace = TraceRuntime(config(False, False, TRACE_EXPORT_ENDPOINT=endpoint))
        backend, _ = setup_bridge()
        backend.owner.trace = trace
        app = create_application(Runtime('demo', services={'studio_backend': backend}, tracing=trace))
        delivered = []
        async def run():
            first = True
            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {'type': 'http.request', 'body': payload(), 'more_body': False}
                await asyncio.Event().wait()
            async def send(message):
                if message.get('body', b'').startswith(b'event: done'):
                    if ending == 'cancel':
                        raise asyncio.CancelledError()
                    if ending == 'failure':
                        raise OSError('synthetic transport failure')
                delivered.append(message)
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': 'POST', 'path': '/app/agent/chat/stream', 'raw_path': b'/app/agent/chat/stream',
                'query_string': b'', 'root_path': '', 'server': ('testserver', 80), 'client': ('127.0.0.1', 1),
                'headers': [(b'host', b'testserver'), (b'origin', ORIGIN.encode()),
                    (b'x-api-agent-session', NONCE.encode()), (b'content-type', b'application/json')]}
            await trace.astart()
            try:
                if ending == 'failure':
                    with pytest.raises(OSError, match='synthetic transport failure'):
                        await app(scope, receive, send)
                else:
                    # Django consumes the send's cancellation and returns normally.
                    await app(scope, receive, send)
            finally:
                await backend.aclose()
                await trace.aclose()
        asyncio.run(run())
    root = next(s for s in spans_from(received) if s.name == 'request')
    expected = {'success': 'success', 'cancel': 'cancelled', 'failure': 'failure'}[ending]
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue': expected}
    assert root.status.code == (2 if ending == 'failure' else 0)
    assert any(m.get('body', b'').startswith(b'event: done') for m in delivered) == (ending == 'success')


@pytest.mark.parametrize('kind',['demo','chat','stateful'])
def test_real_parsed_foreign_runtime_cannot_report_to_request_owner(fixture_graph, kind):
    from test_application_capture_bundle import selected_application
    from api_agent.workflows.stateful import StatefulWorkflow,load_stateful_definition,load_stateful_catalog
    from langgraph.graph.message import add_messages
    with collector() as (endpoint, received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=endpoint)
        first=TraceRuntime(settings)
        second=TraceRuntime(settings,model_identities=('chosen-model',))
        factory=WorkflowObservationFactory(settings,boundary=EmissionBoundary(),trace=second)
        if kind == 'stateful':
            root_path,entry=fixture_graph
            definition=load_stateful_definition(entry,base_root=root_path,pack_root=root_path)
            workflow=StatefulWorkflow(definition,catalog=load_stateful_catalog(definition),
                reducers={'messages':add_messages},services={'observation_factory':factory,
                    'execution_outcome':lambda:ExecutionOutcome(OutcomeState.FAILURE,OutcomeCause.TOOL)})
            async def invoke():
                await workflow.arun({'messages':[],'pending':False,'visits':0},thread_id='foreign')
        elif kind == 'chat':
            chat,model,value=selected_application('chat','visible')
            chat.observations=factory
            async def invoke():
                await chat.complete(value,llm=model,context=OperationContext('foreign'))
        else:
            demo=application(Owner());demo.observations=factory
            model=LLM([json.dumps({'use_tool':True,'arguments':{'query':'{ item { name } }'},'answer':''}),
                verdict(),'visible final'])
            async def invoke():
                await demo.complete(request(),llm=model,context=OperationContext('foreign'))
        async def handler(scope,receive,send):
            await invoke()
            assert current_operation(first).final.state == OutcomeState.UNKNOWN
        app=Application(SimpleNamespace(tracing=first,services={}),handler)
        async def run():
            await first.astart();await second.astart()
            await app({'type':'http'},lambda:None,lambda _:asyncio.sleep(0))
            await second.aclose();await first.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    inbound=next(s for s in spans if s.name=='request')
    graph=next(s for s in spans if s.name=='graph GRAPH')
    assert attrs(inbound)['api_agent.outcome.state']=={'stringValue':'unknown'}
    assert inbound.status.code==0
    assert not graph.parent_span_id and graph.trace_id != inbound.trace_id
    assert graph.status.code==(0 if kind=='chat' else 2)


def test_real_parsed_late_task_cannot_mutate_ended_same_runtime_request():
    from api_agent.telemetry.operation import operation
    with collector() as (endpoint,received):
        settings=config(False,False,TRACE_EXPORT_ENDPOINT=endpoint)
        runtime=TraceRuntime(settings,model_identities=('chosen-model',))
        demo=application(Owner());demo.observations=WorkflowObservationFactory(settings,
            boundary=EmissionBoundary(),trace=runtime)
        model=LLM([json.dumps({'use_tool':True,'arguments':{'query':'{ item { name } }'},'answer':''}),
            verdict(),'visible final'])
        async def run():
            await runtime.astart()
            release=asyncio.Event()
            with operation(runtime) as owner:
                async def late():
                    await release.wait()
                    await demo.complete(request(),llm=model,context=OperationContext('late'))
                task=asyncio.create_task(late())
            release.set();await task
            assert owner.closed and owner.final.state==OutcomeState.UNKNOWN
            await runtime.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    inbound=next(s for s in spans if s.name=='request')
    graph=next(s for s in spans if s.name=='graph GRAPH')
    assert inbound.status.code==0
    assert attrs(inbound)['api_agent.outcome.state']=={'stringValue':'unknown'}
    assert not graph.parent_span_id and graph.trace_id!=inbound.trace_id
    assert graph.status.code==2


@pytest.mark.parametrize('kind,trace',[('scope',False),('scope',True),('tool',False),('tool',True),('payload',True)])
@pytest.mark.parametrize('saturate',[False,True])
def test_observation_capacity_reports_loss_and_closes_every_started_span(kind,trace,saturate,caplog):
    import time
    from uuid import uuid4
    from api_agent.telemetry.operation import operation
    from test_workflow_observations import definition
    with collector() as (endpoint,received):
        settings=config(False,trace,TRACE_EXPORT_ENDPOINT=endpoint)
        runtime=TraceRuntime(settings)
        adapter=WorkflowObservationFactory(settings,boundary=EmissionBoundary(),trace=runtime).create(definition())
        async def run():
            await runtime.astart()
            with operation(runtime):
                graph=str(uuid4());node=str(uuid4())
                adapter.observe(dict(event='scope_start',scope_id=graph,owner='GRAPH',kind='graph',started_ms=0))
                if kind!='scope':
                    adapter.observe(dict(event='scope_start',scope_id=node,parent_id=graph,
                        owner='Make',kind='node',started_ms=0))
                count=(257 if saturate else 256) - (1 if kind=='scope' else 0)
                for _ in range(count):
                    if kind=='scope':
                        adapter.observe(dict(event='scope_start',scope_id=str(uuid4()),parent_id=graph,
                            owner='Make',kind='node',started_ms=0))
                    elif kind=='tool':
                        adapter.observe(dict(event='tool_start',tool_invocation_id=str(uuid4()),
                            visit_id=node,tool_name='lookup',tool_instance='named.lookup',started_ms=time.time()*1000))
                    else:
                        adapter.capture_json(visit_id=node,direction='input',cause_id=str(uuid4()),
                            values={'llm_request':{'messages':[{'role':'user','text':'distinct-payload'}]}},
                            schema={'type':'object'},capture_source='provider')
                assert adapter.trace_losses.count==(1 if saturate else 0)
                assert runtime.losses.count==adapter.trace_losses.count
                adapter.close()
                assert not adapter._spans and not adapter._tool_spans and not adapter._call_payloads
            await runtime.aclose()
        asyncio.run(run())
    spans=spans_from(received)
    inbound=next(s for s in spans if s.name=='request')
    assert len(spans)=={'scope':257,'tool':259,'payload':3}[kind]
    assert len({s.span_id for s in spans})==len(spans)
    by_id={s.span_id:s for s in spans}
    assert all(s.end_time_unix_nano <= by_id[s.parent_span_id].end_time_unix_nano
        for s in spans if s.parent_span_id in by_id)
    assert all(s.end_time_unix_nano>=s.start_time_unix_nano for s in spans)
    assert ('api_agent.trace.loss_count' in attrs(inbound)) is saturate
    if saturate:
        assert attrs(inbound)['api_agent.trace.loss_count']=={'intValue':'1'}
        assert attrs(inbound)['api_agent.trace.complete']=={'boolValue':False}
    warnings=[r for r in caplog.records if 'Tracing observation was lost' in r.message]
    assert len(warnings)==int(saturate)
    assert 'distinct-payload' not in caplog.text


def test_sdk_export_budget_counts_overflow_without_sdk_queue_drops(monkeypatch,caplog):
    from threading import Event
    import requests
    entered,release=Event(),Event()
    original=requests.Session.post
    def blocked(session,*args,**kwargs):
        entered.set()
        assert release.wait(10)
        return original(session,*args,**kwargs)
    monkeypatch.setattr(requests.Session,'post',blocked)
    with collector() as (endpoint,received):
        runtime=TraceRuntime(config(False,False,TRACE_EXPORT_ENDPOINT=endpoint))
        asyncio.run(runtime.astart())
        try:
            for _ in range(32):runtime.begin('request',{}).end()
            assert entered.wait(5)
            for _ in range(994):runtime.begin('request',{}).end()
            assert runtime.losses.count==2
        finally:
            release.set()
            assert runtime.shutdown()
    assert len(spans_from(received))==1024
    assert len([r for r in caplog.records if 'Tracing observation was lost' in r.message])==1
    assert not any('Queue full' in r.message for r in caplog.records)


@pytest.mark.parametrize('kind',['scope','tool','payload'])
@pytest.mark.parametrize('content',[False,True])
@pytest.mark.parametrize('mode',['','OTEL'])
def test_inactive_runtime_caps_are_inert_even_with_broken_logger(monkeypatch,kind,content,mode):
    import builtins
    import time
    from unittest.mock import Mock
    from uuid import uuid4
    from test_workflow_observations import definition
    original_import=builtins.__import__
    def guarded_import(name,*args,**kwargs):
        if name.startswith('opentelemetry'):
            pytest.fail('Inactive tracing imported an optional SDK')
        return original_import(name,*args,**kwargs)
    def broken_log(*args,**kwargs):
        raise OSError('Disabled tracing must not log')
    monkeypatch.setattr(builtins,'__import__',guarded_import)
    monkeypatch.setattr('logging.Logger.error',broken_log)
    factory=Mock(side_effect=AssertionError('Inactive exporter allocation'))
    settings=config(False,content,mode=mode)
    runtime=TraceRuntime(settings,factory=factory)
    adapter=WorkflowObservationFactory(settings,boundary=EmissionBoundary(),trace=runtime).create(definition())
    graph,node=str(uuid4()),str(uuid4())
    adapter.observe(dict(event='scope_start',scope_id=graph,owner='GRAPH',kind='graph',started_ms=0))
    adapter.observe(dict(event='scope_start',scope_id=node,parent_id=graph,
        owner='Make',kind='node',started_ms=0))
    for _ in range(258):
        if kind=='scope':
            adapter.observe(dict(event='scope_start',scope_id=str(uuid4()),parent_id=graph,
                owner='Make',kind='node',started_ms=0))
        elif kind=='tool':
            adapter.observe(dict(event='tool_start',tool_invocation_id=str(uuid4()),visit_id=node,
                tool_name='lookup',tool_instance='named.lookup',started_ms=time.time()*1000))
        else:
            adapter.capture_json(visit_id=node,direction='input',cause_id=str(uuid4()),
                values={'llm_request':{'messages':[{'role':'user','text':'unretained'}]}},
                schema={'type':'object'},capture_source='provider')
    assert runtime.enabled is (mode!='')
    assert not runtime.active
    assert not adapter._spans and not adapter._tool_spans and not adapter._call_payloads
    assert adapter.trace_losses.count==runtime.losses.count==0
    adapter.close()
    assert runtime.shutdown()
    factory.assert_not_called()
