"""Loopback setup handoff and serialized host/worker trace ancestry."""
import asyncio
import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock, ANY
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_transport import SetupClient, SetupServer, _encode, _request_auth
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.remote_inference import RemoteInferenceTrace
from api_agent.telemetry.trace_ownership import settle_trace_ownership
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.inference_capture import bind_boundary, bind_inference_trace, capture_inference
from test_setup_transport import CAPABILITY, Registry
from test_trace_ownership import OFFER, PARENT
from test_workflow_observations import collector, config


def request():
    return LLMRequest('synthetic-model', CompletionRequest((Message('user', (TextContent('synthetic prompt'),)),)), 'ownership-request')


class OwnershipRegistry(Registry):
    def __init__(self, trace=None, failure=None):
        super().__init__()
        self.trace = trace
        self.failpoint = failure
        self.executions = 0

    async def complete(self, binding, request):
        if self.failpoint == 'before_bind':
            raise ContractError('Synthetic pre-grant failure')
        boundary = self.trace.runtime.boundary if self.trace is not None else EmissionBoundary()
        bind_boundary(boundary)
        if self.trace is not None:
            bind_inference_trace(self.trace)
        if self.failpoint == 'after_bind':
            raise ContractError('Synthetic post-grant failure')
        settle_trace_ownership()
        self.executions += 1
        from api_agent.providers.generation_attempts import begin_transport_attempt, finish_transport_attempt
        token = begin_transport_attempt(1)
        capture_inference('input', {'prompt': 'synthetic prompt'}, 'synthetic')
        result = LLMResult(Message('assistant', (TextContent('synthetic answer'),)), Usage(7, 2))
        capture_inference('output', {'text': 'synthetic answer'}, 'synthetic')
        finish_transport_attempt(token, result=result)
        return result

    def quote_cost(self, *args):
        return None


def test_request_parent_offer_requires_valid_mac_even_without_authority_channel():
    server = SetupServer(Registry(), CAPABILITY)
    frame = dict(version=1, action='complete', capability=CAPABILITY, timeout=3,
                 binding=Registry().register('worker'), request=request_to_wire(request()), trace_offer=OFFER)
    with pytest.raises(ContractError, match='capability'):
        server._request(frame)
    frame['capability'] = _request_auth(CAPABILITY, frame)
    assert server._request(frame) == 'complete'
    frame['trace_offer'] = dict(OFFER, nonce='5'*64)
    with pytest.raises(ContractError, match='capability'):
        server._request(frame)


@pytest.mark.parametrize('failure,expected', [(None, 'inactive'), ('before_bind', None), ('after_bind', None)])
def test_no_grant_is_not_mistaken_for_authenticated_inactive(failure, expected):
    async def scenario():
        registry = OwnershipRegistry(failure=failure)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        client = SetupClient(await server.start(), timeout=3)
        try:
            if failure is None:
                await asyncio.to_thread(client.complete, request(), observation_parent=PARENT,
                                        observation_pipeline=OFFER['pipeline'])
            else:
                with pytest.raises(ContractError):
                    await asyncio.to_thread(client.complete, request(), observation_parent=PARENT,
                                            observation_pipeline=OFFER['pipeline'])
            assert client.trace_ownership == expected
            assert registry.executions == int(failure is None)
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
    asyncio.run(scenario())


def _trace_worker(endpoint, target, output):
    async def scenario():
        settings = config(trace=True, TRACE_EXPORT_ENDPOINT=target)
        boundary = EmissionBoundary(ContentPolicy(True))
        from api_agent.telemetry.tracing_providers import admit_binding
        runtime = TraceRuntime(settings, boundary=boundary,
            binding=admit_binding(settings, owner_root=None, boundary=boundary))
        await runtime.astart()
        node = runtime.begin('node worker', {})
        client = SetupClient(endpoint, worker_id='sdk-worker', timeout=5)
        try:
            result = await asyncio.to_thread(client.complete, request(), observation_parent=node.export_context(),
                                            observation_pipeline=runtime.pipeline_id, observation_call_id=str(uuid4()))
            output.put((client.trace_ownership, result.usage.input_tokens,
                        client.transport_attempts['attempts'][0]['usage']['input_tokens']))
        finally:
            await asyncio.to_thread(client.close)
            node.end()
            await runtime.aclose()
    asyncio.run(scenario())


def test_real_process_uses_one_host_generation_under_worker_sdk_parent():
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    with collector() as (target, received):
        async def scenario():
            trace = RemoteInferenceTrace(config(trace=True, TRACE_EXPORT_ENDPOINT=target), EmissionBoundary(ContentPolicy(True)))
            await trace.astart()
            registry = OwnershipRegistry(trace)
            server = SetupServer(registry, CAPABILITY, timeout=5)
            context = multiprocessing.get_context('spawn')
            output = context.Queue()
            worker = context.Process(target=_trace_worker, args=(await server.start(), target, output))
            try:
                worker.start()
                await asyncio.to_thread(worker.join, 15)
                assert worker.exitcode == 0
                assert output.get(timeout=1) == ('granted', 7, 7)
                assert registry.executions == 1
            finally:
                if worker.is_alive():
                    worker.terminate()
                    await asyncio.to_thread(worker.join, 5)
                output.close()
                output.join_thread()
                await server.aclose()
                await trace.aclose()
        asyncio.run(scenario())
    spans = [span for _, raw in received for resource in ExportTraceServiceRequest.FromString(raw).resource_spans
             for scope in resource.scope_spans for span in scope.spans]
    node, = [span for span in spans if span.name == 'node worker']
    generation, = [span for span in spans if span.name == 'chat']
    attempt, = [span for span in spans if span.name == 'transport attempt']
    assert generation.trace_id == node.trace_id == attempt.trace_id
    assert generation.parent_span_id == node.span_id
    assert attempt.parent_span_id == generation.span_id
    assert len(spans) == 3
    assert 'synthetic prompt' in str(spans) and 'synthetic answer' in str(spans)
    assert CAPABILITY not in str(spans)
    assert OFFER['pipeline'] not in str(spans)


@pytest.mark.parametrize('mode', ['forged', 'replayed', 'wrong_request', 'wrong_binding', 'wrong_parent', 'lost', 'duplicate'])
def test_wire_receipt_failure_never_authorizes_local_fallback(monkeypatch, mode):
    import api_agent.telemetry.trace_ownership as ownership
    import api_agent.providers.setup_transport as transport
    real_encode = transport._encode
    def altered_encode(value):
        if value.get('status') != 'trace_ownership':
            return real_encode(value)
        row = dict(value)
        field = {'replayed': 'call_sequence', 'wrong_request': 'request_sha256',
                 'wrong_binding': 'binding_sha256', 'wrong_parent': 'offer_sha256'}.get(mode)
        if field is not None:
            import hmac
            from hashlib import sha256
            row[field] = 99 if field == 'call_sequence' else 'f'*64
            row.pop('proof')
            row['proof'] = hmac.new(CAPABILITY.encode(),
                ('trace-ownership:' + ownership.digest(row)).encode(), sha256).hexdigest()
        elif mode == 'forged':
            row['proof'] = '0'*64
        encoded = real_encode(row)
        if mode == 'lost':
            return b''
        return encoded + encoded if mode == 'duplicate' else encoded
    monkeypatch.setattr(transport, '_encode', altered_encode)
    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        call = Mock(boundary=boundary)
        call.span.export_context.return_value = PARENT
        owner = SimpleNamespace(enabled=True, runtime=SimpleNamespace(boundary=boundary, pipeline_id=OFFER['pipeline']),
                                begin=Mock(return_value=call))
        registry = OwnershipRegistry(owner)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        client = SetupClient(await server.start(), timeout=3)
        try:
            if mode == 'lost':
                await asyncio.to_thread(client.complete, request(), observation_parent=PARENT,
                                        observation_pipeline=OFFER['pipeline'])
            else:
                with pytest.raises(ContractError):
                    await asyncio.to_thread(client.complete, request(), observation_parent=PARENT,
                                            observation_pipeline=OFFER['pipeline'])
            assert client.trace_ownership is None
            assert registry.executions == 1
            owner.begin.assert_called_once()
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['before_bind', 'after_bind'])
def test_failure_on_each_side_of_grant_has_conservative_ownership(failure):
    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        call = Mock(boundary=boundary)
        call.span.export_context.return_value = PARENT
        owner = SimpleNamespace(enabled=True, runtime=SimpleNamespace(boundary=boundary, pipeline_id=OFFER['pipeline']),
                                begin=Mock(return_value=call))
        registry = OwnershipRegistry(owner, failure=failure)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        client = SetupClient(await server.start(), timeout=3)
        try:
            with pytest.raises(ContractError):
                await asyncio.to_thread(client.complete, request(), observation_parent=PARENT,
                                        observation_pipeline=OFFER['pipeline'])
            assert client.trace_ownership == ('granted' if failure == 'after_bind' else None)
            assert registry.executions == 0
            if failure == 'after_bind':
                call.end.assert_called_once_with(failed=True, cancelled=False, end_time=ANY)
                assert type(call.end.call_args.kwargs['end_time']) is int
            else:
                owner.begin.assert_not_called()
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
    asyncio.run(scenario())


def test_disabled_worker_sends_no_trace_offer_or_trace_ipc(monkeypatch):
    import api_agent.providers.setup_transport as transport
    real_encode = transport._encode
    messages = []
    def record(value):
        messages.append(value)
        return real_encode(value)
    monkeypatch.setattr(transport, '_encode', record)
    async def scenario():
        registry = OwnershipRegistry()
        server = SetupServer(registry, CAPABILITY, timeout=3)
        client = SetupClient(await server.start(), timeout=3)
        try:
            await asyncio.to_thread(client.complete, request())
            assert client.trace_ownership is None
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
    asyncio.run(scenario())
    assert not any('trace_offer' in frame or frame.get('status') == 'trace_ownership' for frame in messages)


def test_host_terminal_timestamp_precedes_late_projection_and_worker_node_end():
    from test_operation_traces import spans_from
    with collector() as (target,received):
        async def scenario():
            settings=config(trace=True,TRACE_EXPORT_ENDPOINT=target)
            boundary=EmissionBoundary(ContentPolicy(True))
            from api_agent.telemetry.tracing_providers import admit_binding
            worker=TraceRuntime(settings,boundary=boundary,
                binding=admit_binding(settings,owner_root=None,boundary=boundary))
            host=RemoteInferenceTrace(settings,boundary)
            await worker.astart();await host.astart()
            server=SetupServer(OwnershipRegistry(host),CAPABILITY,timeout=5)
            client=SetupClient(await server.start(),timeout=5)
            projection=asyncio.Event();release=asyncio.Event();early=asyncio.Event()
            original=server._publish_evidence
            async def delayed(*args,**kwargs):
                if kwargs.get('destination') is not None:
                    projection.set()
                    await release.wait()
                return await original(*args,**kwargs)
            server._publish_evidence=delayed
            node=worker.begin('node worker',{})
            loop=asyncio.get_running_loop()
            def result_ready(result):loop.call_soon_threadsafe(early.set)
            dispatch=asyncio.create_task(asyncio.to_thread(client.complete,request(),
                observation_parent=node.export_context(),observation_pipeline=worker.pipeline_id,
                observation_call_id=str(uuid4()),result_ready=result_ready))
            try:
                await asyncio.wait_for(early.wait(),5)
                await asyncio.wait_for(projection.wait(),5)
                node.end()
                assert not dispatch.done()
                release.set()
                await dispatch
                assert client.trace_ownership=='granted'
            finally:
                release.set()
                await asyncio.gather(dispatch,return_exceptions=True)
                await asyncio.to_thread(client.close);await server.aclose()
                node.end();await host.aclose();await worker.aclose()
        asyncio.run(scenario())
    spans=spans_from(received)
    node=next(span for span in spans if span.name=='node worker')
    generation=next(span for span in spans if span.name=='chat')
    physical=next(span for span in spans if span.name=='transport attempt')
    assert generation.parent_span_id==node.span_id
    assert node.start_time_unix_nano<=generation.start_time_unix_nano
    assert generation.start_time_unix_nano-1_000_000<=physical.start_time_unix_nano
    assert physical.end_time_unix_nano<=generation.end_time_unix_nano<=node.end_time_unix_nano
