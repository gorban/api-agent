"""Rejected selected-provider output remains visible without failing repaired runs."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from api_agent.api.application import Application
from api_agent.providers.control import OperationContext
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.observations import WorkflowObservationFactory
from test_demo_application import Owner, application, request, verdict
from test_operation_traces import attrs, shared_model, spans_from
from test_workflow_observations import collector, config


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('adapter_validates', [False, True])
def test_rejected_generation_details_preserve_transport_and_final_graph_outcome(tmp_path, mode, log, trace, terminal, adapter_validates):
    secret = 'distinct-credential'
    rejected = json.dumps({'use_tool': False, 'arguments': {},
                           'answer': secret + 'x' * (8666 - len(secret))})
    outputs = [rejected, rejected] if terminal else [rejected,
        json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Repaired'}), verdict(), 'Repaired']
    with collector() as (endpoint, received):
        settings = config(log, trace, mode, TRACE_EXPORT_ENDPOINT=endpoint,
                          LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(log))
        runtime = TraceRuntime(settings, boundary=boundary, model_identities=('chosen-model',))
        app = application(Owner())
        app.observations = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
        model = shared_model(tmp_path, boundary, runtime, outputs)
        if adapter_validates:
            from api_agent.providers.inference import result_to_wire
            from api_agent.providers.rich_inference import async_validate_result
            from api_agent.workflows.inference_capture import capture_inference
            provider = model.loader.return_value.implementation
            complete = provider.acomplete

            async def validate_in_adapter(value, *, auth):
                result = await complete(value, auth=auth)
                capture_inference('output', result_to_wire(result), value.request_id,
                                  auth=auth, fallback=True)
                return await async_validate_result(value, result)

            provider.acomplete = validate_in_adapter

        async def handler(scope, receive, send):
            await app.complete(request(), llm=model, context=OperationContext('request'))
            await send({'type': 'http.response.body', 'body': b'complete'})

        asgi = Application(SimpleNamespace(tracing=runtime, services={}), handler)

        async def run():
            await runtime.astart()
            try:
                if terminal:
                    with pytest.raises(WorkflowError, match='exhausted'):
                        await asgi({'type': 'http'}, lambda: None, lambda _: asyncio.sleep(0))
                else:
                    await asgi({'type': 'http'}, lambda: None, lambda _: asyncio.sleep(0))
            finally:
                await model.aclose()
                await runtime.aclose()

        asyncio.run(run())

    spans = spans_from(received)
    root = next(span for span in spans if span.name == 'request')
    graph = next(span for span in spans if span.name == 'graph GRAPH')
    generations = [span for span in spans if attrs(span).get('gen_ai.operation.name') == {'stringValue': 'chat'}]
    rejected_call = next(span for span in generations if span.status.code == 2)
    physical = [span for span in spans if span.parent_span_id == rejected_call.span_id]
    assert len(generations) == (2 if terminal else 4)
    assert len(physical) == 1 and physical[0].status.code == (2 if adapter_validates else 0)
    assert attrs(physical[0])['api_agent.transport_outcome'] == {
        'stringValue': 'failed' if adapter_validates else 'completed'}
    assert root.status.code == graph.status.code == (2 if terminal else 0)
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue': 'failure' if terminal else 'success'}
    rejected_calls = [span for span in generations if span.status.code == 2]
    rejected_ids = {attrs(span)['api_agent.call_id']['stringValue'] for span in rejected_calls}
    rejected_nodes = [span for span in spans if span.name == 'node plan' and span.status.code == 2]
    assert {attrs(span)['api_agent.outcome.cause_id']['stringValue'] for span in rejected_nodes} == rejected_ids
    if terminal:
        final_call = max(rejected_calls, key=lambda span: span.end_time_unix_nano)
        assert attrs(root)['api_agent.outcome.cause_id'] == attrs(final_call)['api_agent.call_id']
        assert attrs(graph)['api_agent.outcome.cause_id'] == attrs(final_call)['api_agent.call_id']
    output_key = 'langfuse.observation.output'
    assert (output_key in attrs(rejected_call)) is trace
    if trace:
        output = json.loads(attrs(rejected_call)[output_key]['stringValue'])
        failure = output['llm_response']['application_failure']
        assert failure['cause'] == 'validation'
        assert failure['code'] == 'schema_validation'
        assert failure['candidate']['feedback']['errors'] == [
            {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 8192, 'actual': 8666}]
        messages = json.loads(attrs(rejected_call)['gen_ai.output.messages']['stringValue'])
        assert len(messages) == 1 and messages[0]['role'] == 'assistant'
        assert 'application_failure' not in repr(messages)
    assert secret not in repr(received)
