"""Actual Studio SSE completion retains trace failure details and recovery state."""
import asyncio
import json
import secrets
from uuid import uuid4

import httpx
import pytest

from api_agent.providers.llm import AvailableSelection
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.testing.studio_http import agent_endpoint
from api_agent.testing.studio_tracing import StudioTraceOwner
from api_agent.testing.workflow_capture import capture_sources
from api_agent.workflows.observations import WorkflowObservationFactory
from test_demo_application import Owner, application, verdict
from test_operation_traces import attrs, shared_model, spans_from
from test_workflow_observations import collector, config


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('terminal', [False, True])
def test_actual_studio_stream_keeps_failed_call_details_and_final_outcome(tmp_path, mode, terminal):
    with collector() as (endpoint, received):
        settings = config(True, True, mode, TRACE_EXPORT_ENDPOINT=endpoint,
                          LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        boundary = EmissionBoundary(ContentPolicy(True))
        runtime = TraceRuntime(settings, boundary=boundary, model_identities=('chosen-model',))
        rejected = json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'x' * 8666})
        outputs = [rejected, rejected] if terminal else [rejected,
            json.dumps({'use_tool': False, 'arguments': {}, 'answer': 'Repaired'}), verdict(), 'Repaired']

        async def run():
            loop = asyncio.get_running_loop()
            model = shared_model(tmp_path, boundary, runtime, outputs)

            class View:
                emissions = boundary

                def describe(self):
                    return (AvailableSelection('chosen-model'),)

                def complete(self, request):
                    return asyncio.run_coroutine_threadsafe(model.complete(request), loop).result()

            provider = View()
            tracing = StudioTraceOwner(settings.values, capture_sources(settings.values, settings.fields),
                provider, capture_services={'emissions': boundary},
                runtime_factory=lambda *args, **kwargs: runtime)
            app = application(Owner())
            app.observations = WorkflowObservationFactory(settings, boundary=boundary, trace=runtime)
            origin, nonce = 'http://127.0.0.1:9443', secrets.token_urlsafe(32)
            try:
                async with agent_endpoint(provider, 'chosen-model', origin, nonce, application=app, tracing=tracing) as url:
                    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
                        response = await client.post(url + '/app/agent/chat/stream',
                            headers={'Origin': origin, 'X-API-Agent-Session': nonce},
                            json={'session_id': str(uuid4()), 'messages': [{'role': 'user', 'content': 'Answer briefly'}]})
                        assert response.status_code == 200
                        return response.text
            finally:
                await model.aclose()

        body = asyncio.run(run())

    spans = spans_from(received)
    root = next(span for span in spans if span.name == 'POST /app/agent/chat/stream')
    assert attrs(root)['api_agent.outcome.state'] == {'stringValue': 'failure' if terminal else 'success'}
    assert root.status.code == (2 if terminal else 0)
    failed = [span for span in spans if span.name == 'chat chosen-model' and span.status.code == 2]
    assert len(failed) == (2 if terminal else 1)
    for span in failed:
        output = json.loads(attrs(span)['langfuse.observation.output']['stringValue'])
        assert output['llm_response']['application_failure']['candidate']['feedback']['errors'][0]['actual'] == 8666
    if terminal:
        assert 'event: error' in body and 'event: result' not in body
        assert attrs(root)['error.type'] == {'stringValue': 'validation'}
        assert attrs(root)['api_agent.outcome.cause_id'] in [attrs(span)['api_agent.call_id'] for span in failed]
    else:
        assert 'event: result' in body and 'event: error' not in body
        assert 'Repaired' in body
