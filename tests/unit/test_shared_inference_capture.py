"""Shared capture traverses a parsed demo and real serialized loopback exporters."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.attribution import bind_workflow_visit, current_workflow_visit
from api_agent.workflows.inference_capture import capture_inference
from api_agent.workflows.observations import WorkflowObservationFactory
from test_demo_application import Owner, application, verdict
from test_workflow_observations import collector, config, definition


def authenticated_child():
    async def call(callback):
        return await callback(auth='synthetic-auth')
    return SimpleNamespace(call=call, aclose=AsyncMock())


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
def test_parsed_useful_demo_captures_nodes_and_actual_llm_messages_without_custom_wiring(log, trace, mode):
    with collector() as (endpoint, received):
        resolved = config(log, trace, mode, TRACE_TARGET=endpoint,
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(True))
        boundary.register_secret('registered-sensitive-value')
        runtime = TraceRuntime(resolved, boundary=boundary, model_identities=('chosen-model',))
        rows, journals, requests = [], [], []
        class Factory(WorkflowObservationFactory):
            def create(self, *args, **kwargs):
                journal = super().create(*args, **kwargs)
                journals.append(journal)
                return journal
        app = application(Owner())
        app.observations = Factory(resolved, boundary=boundary, sink=rows.append, trace=runtime)
        outputs = iter([json.dumps({'use_tool': True,
            'arguments': {'query': '{ item { name } }'}, 'answer': ''}), verdict(), 'shared-visible-response'])
        async def provider(request, *, auth):
            requests.append(request)
            result = LLMResult(Message('assistant', (TextContent(next(outputs)),)), Usage(2, 3))
            # Stand in for the existing authoritative accounting publisher.
            journal, visit, attempt = current_workflow_visit()
            journal.record_call(visit_id=visit, attempt_id=attempt, call_id=str(uuid4()),
                usage={'input_tokens': 2, 'output_tokens': 3}, selection='chosen-model', elapsed=.001)
            return result
        async def scenario():
            await runtime.astart()
            shared = SharedSetupOwner('operation', 'selected', lambda credentials: authenticated_child(), complete=provider)
            async def complete(request, *, context):
                return await shared.complete(request)
            request = LLMRequest('chosen-model', CompletionRequest((Message('user',
                (TextContent('shared-visible-request registered-sensitive-value'),)),)))
            try:
                result = await app.complete(request, llm=SimpleNamespace(complete=complete),
                    context=OperationContext('turn'))
                assert result.payload.content[0].text == 'shared-visible-response'
            finally:
                await shared.aclose()
        asyncio.run(scenario())
        retained = journals[0].finalize()
        assert runtime.shutdown()
        spans = [span for _, body in received
                 for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        wire_text = str(spans)
        log_text, artifact_text = repr(rows), json.dumps(retained)
        for text in ('shared-visible-request', 'shared-visible-response'):
            assert (text in wire_text) is trace
            assert (text in log_text) is log
            assert (text in artifact_text) is log
        assert all('registered-sensitive-value' not in text for text in (wire_text, log_text, artifact_text))
        assert len(requests) == 3
        assert 'registered-sensitive-value' in requests[0].payload.messages[-1].content[0].text
        captures = [row for row in rows if row['event'] == 'capture']
        assert len(captures) == 14  # Four node pairs plus three actual provider pairs.
        pairs = [row for row in captures if 'cause_id' in row]
        assert len(pairs) == 6 and len({row['cause_id'] for row in pairs}) == 3
        assert all([row['direction'] for row in pairs if row['cause_id'] == cause] == ['input', 'output']
                   for cause in {row['cause_id'] for row in pairs})
        calls = [row for row in retained['events'] if row['event'] == 'call' and row.get('usage')]
        assert len(calls) == 3
        assert sum(row['usage']['input_tokens'] for row in calls) == 6
        assert len([span for span in spans if span.name.startswith('chat ')]) == 3


@pytest.mark.parametrize('failure', [ValueError('provider failed'), asyncio.CancelledError()])
def test_failed_provider_capture_has_request_without_fabricated_response(failure):
    async def scenario():
        rows = []
        resolved = config(True, False)
        adapter = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), sink=rows.append).create(definition())
        visit = str(uuid4())
        adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
        async def complete(request, *, auth):
            raise failure
        shared = SharedSetupOwner('operation', 'selected', lambda credentials: authenticated_child(), complete=complete)
        request = LLMRequest('chosen-model', CompletionRequest((Message('user', (TextContent('visible-request synthetic-auth'),)),)))
        try:
            with bind_workflow_visit(adapter, visit):
                with pytest.raises(type(failure)):
                    await shared.complete(request)
        finally:
            await shared.aclose()
            adapter.close()
        captures = [row for row in rows if row['event'] == 'capture']
        assert len(captures) == 1 and captures[0]['direction'] == 'input'
        assert 'visible-request' in repr(captures)
        assert 'synthetic-auth' not in repr(captures)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [ValueError('diagnostic failure'), asyncio.CancelledError()])
def test_capture_without_observation_authority_is_inert_and_callback_failures_preserve_inference(failure):
    capture_inference('input', {}, str(uuid4()))
    with bind_workflow_visit(object(), 'visit'):
        capture_inference('input', {}, str(uuid4()))
    with bind_workflow_visit(SimpleNamespace(capture_json=AsyncMock()), 'visit'):
        capture_inference('input', {}, str(uuid4()), auth='not-registered')
    broken = SimpleNamespace(capture_json=lambda **kwargs: (_ for _ in ()).throw(failure))
    with bind_workflow_visit(broken, 'visit'):
        capture_inference('input', {}, str(uuid4()))
