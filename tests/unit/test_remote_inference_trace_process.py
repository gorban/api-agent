"""Remote tracing: real pytest/setup/SDK/projection IPC, loopback OTLP only."""
import json

import pytest

from test_bedrock_authority_process import synthetic_sdk  # noqa: F401
from test_bedrock_authority_process import test_bedrock_worker_through_recorded_allure as _recorded_case
from test_workflow_observations import collector


@pytest.fixture
def recorded_trace(synthetic_sdk, tmp_path, monkeypatch):
    from api_agent.config.resolve import ResolvedValue
    from api_agent.testing import recorded
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    original = recorded.run_recorded_integration

    def run(log, trace, *, mode='OTEL', failure=False, provenance='cli'):
        results, events = [], []
        with collector() as (endpoint, received):
            def recording(context, tests, config, **kwargs):
                config = dict(config, TRACE_MODE=mode, TRACE_TARGET=endpoint,
                              LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-export-secret')
                fields = {} if provenance is None else {
                    name: ResolvedValue(config[name], provenance, provenance != 'default')
                    for name in ('LOG_CONTENT', 'TRACELOOP_TRACE_CONTENT')}
                result = original(context, tests, config, config_fields=fields,
                                  on_event=events.append, **kwargs)
                results.append(result)
                return result
            monkeypatch.setattr(recorded, 'run_recorded_integration', recording)
            _recorded_case(synthetic_sdk, tmp_path, monkeypatch, log, trace, failure)
        spans = [span for _, body in received
                 for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        artifacts = ''.join(path.read_text(errors='replace') for path in results[0].directory.rglob('*') if path.is_file())
        return spans, artifacts, events
    return run


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('failure', [False, True])
def test_remote_destination_policy_and_attempt_identity(recorded_trace, monkeypatch, log, trace, mode, failure):
    monkeypatch.setenv('SYNTHETIC_REASONING', 'remote-visible-reasoning')
    spans, artifacts, events = recorded_trace(log, trace, mode=mode, failure=failure)
    root, = [span for span in spans if span.name == 'chat']
    child, = [span for span in spans if span.name == 'transport attempt']
    assert child.parent_span_id == root.span_id
    assert child.events
    assert all(child.start_time_unix_nano <= event.time_unix_nano <= child.end_time_unix_nano for event in child.events)
    attributes = {item.key: item.value.string_value for item in root.attributes}
    child_attributes = {item.key: item.value.string_value for item in child.attributes}
    assert attributes['api_agent.call_id'] == child_attributes['api_agent.call_id']
    assert attributes['api_agent.call_id'] in artifacts
    assert child_attributes['api_agent.transport_id'] in artifacts
    wire = str(spans)
    marker = 'observed failure' if failure else 'remote-visible-reasoning'
    assert (marker in wire) is trace
    if not failure:
        assert (marker in artifacts) is log
        assert (marker in json.dumps(events, default=str)) is log
    assert ('Say Ok' in wire) is trace
    if not trace:
        assert 'TRACELOOP_TRACE_CONTENT=True' in wire
        assert 'withheld' in wire
    for secret in ('synthetic-access', 'synthetic-secret', 'synthetic-token', 'synthetic-export-secret'):
        assert secret not in wire
    if failure:
        assert child.status.code == 2


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_rejected_contract_body_uses_independent_destination_permissions(recorded_trace, monkeypatch, log, trace):
    monkeypatch.setenv('SYNTHETIC_CONTRACT_REJECTION', '1')
    monkeypatch.setenv('SYNTHETIC_REASONING', 'rejected-reasoning-canary')

    spans, artifacts, events = recorded_trace(log, trace)

    child, = [span for span in spans if span.name == 'transport attempt']
    assert child.status.code == 2
    wire = str(spans)
    for marker in ('contract-response-canary', 'rejected-reasoning-canary'):
        assert (marker in wire) is trace
        assert (marker in artifacts) is log
        assert (marker in json.dumps(events, default=str)) is log
    assert 'response_validation' in artifacts and 'rejected' in artifacts
    assert 'allowed_fields' in artifacts and '$.choices[].message' in artifacts
    assert '"_rejected"' not in artifacts and '"_rejected"' not in json.dumps(events, default=str)
    for marker in ('excluded-encrypted-canary', 'excluded-state-canary',
                   'synthetic-access', 'synthetic-secret', 'synthetic-token'):
        assert marker not in wire and marker not in artifacts and marker not in json.dumps(events, default=str)


@pytest.mark.parametrize('split_field', [None, 'thinking_content', 'chain_of_thought', 'unexpected', 'synthetic-'])
def test_rejected_contract_body_with_a_credential_remains_withheld(recorded_trace, monkeypatch, split_field):
    monkeypatch.setenv('SYNTHETIC_CONTRACT_REJECTION', '1')
    monkeypatch.setenv('SYNTHETIC_CROSS_SECRET', 'synthetic-caller-only')
    if split_field is not None:
        monkeypatch.setenv('SYNTHETIC_SPLIT_FIELD', split_field)

    spans, artifacts, events = recorded_trace(True, True)

    child, = [span for span in spans if span.name == 'transport attempt']
    assert child.status.code == 2
    for marker in ('synthetic-caller-only', 'contract-response-canary'):
        assert marker not in str(spans) and marker not in artifacts and marker not in json.dumps(events, default=str)
    assert 'unavailable' in artifacts


@pytest.mark.parametrize('provenance', [None, 'default', 'provider'])
def test_remote_trace_mapping_cannot_invent_content_permission(recorded_trace, monkeypatch, provenance):
    monkeypatch.setenv('SYNTHETIC_REASONING', 'remote-visible-reasoning')
    spans, artifacts, _ = recorded_trace(False, True, provenance=provenance)
    assert spans
    assert 'remote-visible-reasoning' not in str(spans)
    assert 'remote-visible-reasoning' not in artifacts


def test_remote_trace_retry_projects_complete_late_credential_union(recorded_trace, tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_RETRY_COUNTER', str(tmp_path / 'retry-count'))
    spans, artifacts, _ = recorded_trace(False, True)
    root, = [span for span in spans if span.name == 'chat']
    children = [span for span in spans if span.name == 'transport attempt']
    assert len(children) == 2
    assert all(child.parent_span_id == root.span_id for child in children)
    assert all(child.start_time_unix_nano <= event.time_unix_nano <= child.end_time_unix_nano
               for child in children for event in child.events)
    assert all(next(item.value.string_value for item in child.attributes if item.key == 'api_agent.transport_id') in artifacts for child in children)
    assert 'retry [REDACTED]' in str(spans)
    assert 'synthetic-refreshed-token' not in str(spans)


def test_remote_trace_rejects_caller_only_secret(recorded_trace, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_CROSS_SECRET', 'synthetic-caller-only')
    spans, artifacts, _ = recorded_trace(False, True)
    assert spans
    assert 'Say Ok' in str(spans)
    assert 'synthetic-caller-only' not in str(spans)
    assert 'synthetic-caller-only' not in artifacts


def test_remote_trace_keeps_worker_withholding_for_secret_bearing_retry_error(recorded_trace, tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_CROSS_SECRET', 'synthetic-caller-only')
    monkeypatch.setenv('SYNTHETIC_RETRY_COUNTER', str(tmp_path / 'retry-count'))
    monkeypatch.setenv('SYNTHETIC_RETRY_ERROR', 'retry synthetic-caller-only synthetic-refreshed-token')
    spans, artifacts, _ = recorded_trace(False, True)
    assert len([span for span in spans if span.name == 'transport attempt']) == 2
    # The SDK worker rejects an error body containing an already joined secret;
    # later projections must not recover the original body from that rejection.
    assert 'retry ' not in str(spans)
    assert 'Say Ok' in str(spans)
    assert 'synthetic-caller-only' not in str(spans)
    assert 'synthetic-refreshed-token' not in str(spans)
    assert 'synthetic-caller-only' not in artifacts


@pytest.mark.parametrize('disconnect', [False, True])
def test_remote_interruption_ends_spans_without_projecting_active_payloads(disconnect):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer, _encode, _decode
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.generation_attempts import begin_transport_attempt, finish_transport_attempt
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.telemetry.remote_inference import RemoteInferenceTrace
    from api_agent.workflows import inference_capture as capture
    from test_setup_call_state import CAP, REQUEST
    from test_workflow_observations import config
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    call_id = str(uuid4())
    with collector() as (endpoint, received):
        async def run():
            boundary = EmissionBoundary(ContentPolicy(False))
            trace = RemoteInferenceTrace(config(False, True, TRACE_TARGET=endpoint), boundary)
            entered = asyncio.Event()
            async def complete(request):
                capture.bind_boundary(boundary)
                await trace.astart()
                capture.bind_inference_trace(trace)
                token = begin_transport_attempt(1)
                try:
                    capture.capture_inference('input', {'prompt':'active-payload-must-not-export'}, request.request_id)
                    entered.set()
                    await asyncio.sleep(60)
                finally:
                    finish_transport_attempt(token, error=asyncio.CancelledError())
            owner = SimpleNamespace(complete=complete, ensure_ready=AsyncMock(), aclose=trace.aclose)
            registry = SetupRegistry('synthetic-operation', 'synthetic-selection', lambda: owner)
            server = SetupServer(registry, CAP, timeout=3)
            descriptor = await server.start()
            reader, writer = await asyncio.open_connection(descriptor['host'], descriptor['port'])
            try:
                writer.write(_encode(dict(version=1, capability=CAP, action='register', timeout=3, worker_id='worker', attempt=1)))
                binding = _decode(await reader.readline())['binding']
                writer.write(_encode(dict(version=1, capability=CAP, action='complete', timeout=3, binding=binding,
                                          request=request_to_wire(REQUEST), observation_call_id=call_id)))
                await asyncio.wait_for(entered.wait(), 2)
                if disconnect:
                    writer.close()
                    await writer.wait_closed()
                    # Let the server observe EOF before requesting shutdown.
                    await asyncio.sleep(.05)
                await server.aclose()
            finally:
                writer.close()
                await writer.wait_closed()
                await server.aclose()
                await registry.aclose()
        asyncio.run(run())
    spans = [span for _, body in received
             for resource in ExportTraceServiceRequest.FromString(body).resource_spans
             for scope in resource.scope_spans for span in scope.spans]
    root, = [span for span in spans if span.name == 'chat']
    child, = [span for span in spans if span.name == 'transport attempt']
    assert root.end_time_unix_nano and child.end_time_unix_nano
    assert child.parent_span_id == root.span_id
    assert call_id in str(spans) and 'unavailable' in str(spans)
    assert 'active-payload-must-not-export' not in str(spans)
