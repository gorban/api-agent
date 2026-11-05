"""Failure status is readable without inventing a generated answer."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from opentelemetry.trace import StatusCode

from api_agent.providers.contracts import ProviderAccessError
from api_agent.telemetry.operation import describe_failure, operation
from api_agent.telemetry.outcomes import (ExecutionOutcome, OutcomeCause, OutcomeState,
                                         report_failure)
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.observations import WorkflowObservationFactory
from test_trace_failure_classification import trace_runtime
from test_workflow_candidate_repair import workflow, replace_node
from test_workflow_observations import collector, config


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('content', [False, True])
def test_failed_request_and_scopes_keep_scrubbed_status_after_close(mode, content):
    secret = 'synthetic-status-secret-73'
    error = ProviderAccessError('unknown', 'Response body limit reached ' + secret,
                                code='response_body_limit')
    flow = workflow()

    async def fail(inputs, context):
        raise error

    replace_node(flow, 'first_draft', fail)
    with trace_runtime(trace=content, mode=mode) as (runtime, resolved, exporter):
        runtime.boundary.register_secret(secret)
        flow.services['observation_factory'] = WorkflowObservationFactory(
            resolved, boundary=runtime.boundary, trace=runtime)
        with pytest.raises(ProviderAccessError):
            with operation(runtime):
                asyncio.run(flow.arun({'user_input': 'user'}))
        spans = exporter.get_finished_spans()
        failed = [span for span in spans if span.status.status_code == StatusCode.ERROR]
        assert {span.name for span in failed} >= {
            'request', 'graph GRAPH', 'component first', 'node first_draft'}
        for span in failed:
            assert span.status.description
            assert ('Response body limit reached' in span.status.description) is content
            events = [event for event in span.events if event.name == 'api_agent.failure']
            assert len(events) == 1
            assert events[0].attributes['api_agent.failure.code'] == 'provider'
            assert ('api_agent.failure.details' in events[0].attributes) is content
        wire = repr([(span.status.description, dict(span.attributes),
            [dict(event.attributes) for event in span.events]) for span in spans])
        assert secret not in wire
        assert not any('gen_ai.output.messages' in span.attributes for span in spans)


def test_unknown_exception_text_never_becomes_failure_status():
    secret = 'unadmitted-exception-text'
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with pytest.raises(ValueError):
            with operation(runtime):
                raise ValueError(secret)
        span = exporter.get_finished_spans()[0]
        assert span.status.description == 'Operation failed (execution).'
        assert secret not in repr((span.status.description, dict(span.attributes),
            [dict(event.attributes) for event in span.events]))


def test_cancellation_does_not_become_error_status():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with pytest.raises(asyncio.CancelledError):
            with operation(runtime):
                raise asyncio.CancelledError('not content')
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code != StatusCode.ERROR
        assert not any(event.name == 'api_agent.failure' for event in span.events)


@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
@pytest.mark.parametrize('content', [False, True])
def test_readable_failure_survives_real_otlp_encoding(mode, content):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    secret = 'wire-status-credential-871'
    boundary = EmissionBoundary()
    with collector() as (endpoint, received):
        resolved = config(trace=content, mode=mode, TRACE_EXPORT_ENDPOINT=endpoint,
                          LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        runtime = TraceRuntime(resolved, boundary=boundary)
        asyncio.run(runtime.astart())
        # Registration after construction must still protect new status/events.
        boundary.register_secret(secret)
        with pytest.raises(ProviderAccessError):
            with operation(runtime):
                raise ProviderAccessError('unknown', 'Body limit reached ' + secret,
                                          code='response_body_limit')
        assert runtime.shutdown()
        wire = ExportTraceServiceRequest()
        wire.ParseFromString(received[0][1])
        span = wire.resource_spans[0].scope_spans[0].spans[0]
        assert span.status.code == 2
        assert ('Body limit reached' in span.status.message) is content
        assert span.events[0].name == 'api_agent.failure'
        assert secret.encode() not in b''.join(body for _, body in received)


def test_handled_child_error_does_not_add_final_request_failure():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with operation(runtime) as owner:
            child = runtime.begin('handled child', {})
            child.failure(ProviderAccessError('unknown', 'A handled rejection'))
            child.end()
            owner.report(ExecutionOutcome(OutcomeState.SUCCESS))
        spans = exporter.get_finished_spans()
        request = next(span for span in spans if span.name == 'request')
        assert request.status.status_code != StatusCode.ERROR
        assert not request.events


def test_terminal_report_only_describes_the_matching_runtime():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with operation(runtime):
            report_failure(ProviderAccessError('unknown', 'not this request'),
                           observations=SimpleNamespace(trace=object()), terminal=True)
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code != StatusCode.ERROR
        assert not span.events


def test_late_failure_and_cancellation_do_not_create_error_events():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        span = runtime.begin('cancelled', {})
        span.failure(asyncio.CancelledError())
        span.end()
        span.failure(ProviderAccessError('unknown', 'late'))
        captured = exporter.get_finished_spans()[0]
        assert captured.status.status_code != StatusCode.ERROR
        assert not captured.events


def test_expired_deadline_preserves_explicit_classification():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        span = runtime.begin('deadline', {})
        span.failure(asyncio.CancelledError(), deadline_expired=True)
        span.outcome(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.TIMEOUT))
        span.attributes({'status': 'error'})
        span.end()
        captured = exporter.get_finished_spans()[0]
        assert captured.status.description == 'Operation failed (deadline).'
        detail = json.loads(captured.events[0].attributes['api_agent.failure.details'])
        assert detail['cause'] == 'timeout' and detail['code'] == 'deadline'


def test_failure_summary_key_is_also_subject_to_credential_scrubbing():
    with trace_runtime() as (runtime, _, exporter):
        runtime.boundary.register_secret('message')
        span = runtime.begin('failed', {})
        span.failure(ValueError('untrusted'))
        span.end()
        captured = exporter.get_finished_spans()[0]
        assert captured.status.description is None
        assert 'message' not in captured.events[0].attributes


def test_failure_without_exception_has_a_fixed_readable_status():
    with trace_runtime() as (runtime, _, exporter):
        span = runtime.begin('semantic failure', {})
        span.outcome(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.EXECUTION))
        span.end()
        captured = exporter.get_finished_spans()[0]
        assert captured.status.description == 'Operation did not complete successfully.'
        assert not captured.events


def test_optional_span_detail_lookup_cannot_replace_execution_error(caplog):
    class BrokenSpan:
        @property
        def failure(self):
            raise ValueError('untrusted adapter text')
    describe_failure(BrokenSpan(), ValueError('original'))
    describe_failure(None, ValueError('original'))
    assert 'Tracing observation failed' in caplog.text
    assert 'untrusted adapter text' not in caplog.text


def test_response_body_limit_has_readable_final_status_without_partial_payload(monkeypatch):
    import httpx
    from api_agent.llm_providers.gpt import inference
    from test_gpt_inference import BASE, TOKEN, request

    monkeypatch.setattr(inference, '_LIMIT', 1024)
    partial = 'private-unparsed-stream-fragment'
    calls = []

    def transport(sent):
        calls.append(sent)
        return httpx.Response(200, content=(partial * 40).encode(),
                              headers={'content-type': 'text/event-stream'})

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await inference.complete_text(client=client, base_url=BASE, access_token=TOKEN,
                subscription=True, api_mode='responses', request=request(maximum=None))

    with trace_runtime(trace=True) as (runtime, _, exporter):
        with pytest.raises(ProviderAccessError) as caught:
            with operation(runtime):
                asyncio.run(invoke())
        span = exporter.get_finished_spans()[0]
        assert '1024-byte body limit before decoding' in span.status.description
        assert 'not a model output-token limit' in span.status.description
        details = json.loads(span.events[0].attributes['api_agent.failure.details'])
        assert details['provider']['code'] == 'response_body_limit'
        assert details['provider']['status'] == 200
        assert caught.value.execution == 'possibly_executed'
        assert caught.value.usage is None
        assert len(calls) == 1
        assert partial not in repr((span.status.description, dict(span.attributes),
            [dict(event.attributes) for event in span.events]))


def test_failure_publication_freezes_registration_through_status_and_event(monkeypatch):
    from api_agent.telemetry.privacy import CredentialRegistrationBlocked
    with trace_runtime(trace=True) as (runtime, _, exporter):
        span = runtime.begin('failed', {})
        checked = []
        original_export = span._boundary.export
        original_status = span._span.set_status
        original_event = span._span.add_event

        def export(payload):
            clean = original_export(payload)
            with pytest.raises(CredentialRegistrationBlocked):
                runtime.boundary.register_secret('new-during-export')
            checked.append('export')
            return clean

        def status(value):
            with pytest.raises(CredentialRegistrationBlocked):
                runtime.boundary.register_secret('new-during-status')
            checked.append('status')
            return original_status(value)

        def event(*args, **kwargs):
            with pytest.raises(CredentialRegistrationBlocked):
                runtime.boundary.register_secret('new-during-event')
            checked.append('event')
            return original_event(*args, **kwargs)

        monkeypatch.setattr(span._boundary, 'export', export)
        monkeypatch.setattr(span._span, 'set_status', status)
        monkeypatch.setattr(span._span, 'add_event', event)
        span.failure(ProviderAccessError('unknown', 'bounded failure'))
        assert checked == ['export', 'status', 'event']
        runtime.boundary.register_secret('new-during-export')
        runtime.boundary.register_secret('new-during-status')
        runtime.boundary.register_secret('new-during-event')
        span.end()
        assert exporter.get_finished_spans()[0].status.description == 'bounded failure'


def test_scope_closure_rescrubs_cached_failure_description():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        span = runtime.begin('failed', {})
        span.failure(ProviderAccessError('unknown', 'Failure with later-registered-value'))
        runtime.boundary.register_secret('later-registered-value')
        span.outcome(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.PROVIDER))
        span.end()
        # Prior publication cannot be recalled; the later status write must use
        # its current credential authority rather than the earlier cached text.
        assert 'later-registered-value' not in exporter.get_finished_spans()[0].status.description


def test_failed_failure_projection_releases_registration_freeze(monkeypatch):
    with trace_runtime(trace=True) as (runtime, _, _):
        span = runtime.begin('failed', {})
        def broken(payload):
            raise ValueError('synthetic projection failure')
        monkeypatch.setattr(span._boundary, 'export', broken)
        with pytest.raises(ValueError, match='projection failure'):
            span.failure(ProviderAccessError('unknown', 'failure'))
        runtime.boundary.register_secret('registration-after-fault')
        span.end()


def test_sdk_status_fault_releases_registration_freeze(monkeypatch):
    with trace_runtime(trace=True) as (runtime, _, _):
        span = runtime.begin('failed', {})
        def broken(value):
            raise ValueError('synthetic SDK status failure')
        monkeypatch.setattr(span._span, 'set_status', broken)
        span.attributes({'status': 'error'})
        runtime.boundary.register_secret('registration-after-SDK-fault')
        span.end()


def test_candidate_text_is_not_a_status_or_failure_event():
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.providers.llm import LLMResult, Message, TextContent
    candidate = 'candidate-private-fragment'
    error = CandidateOutputError(LLMResult(Message('assistant', (TextContent(candidate),))),
                                 {'reason': 'response_schema', 'errors': []})
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with pytest.raises(CandidateOutputError):
            with operation(runtime):
                raise error
        span = exporter.get_finished_spans()[0]
        assert candidate not in repr((span.status.description, dict(span.attributes),
            [dict(event.attributes) for event in span.events]))


def test_request_description_follows_the_retained_terminal_failure():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure
    first = ProviderAccessError('unknown', 'first terminal failure')
    second = ProviderAccessError('unknown', 'second terminal failure')
    first_call, second_call = str(uuid4()), str(uuid4())
    retain_failure(first, provider=True, cause_id=first_call)
    retain_failure(second, provider=True, cause_id=second_call)
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with operation(runtime):
            report_failure(first, observations=SimpleNamespace(trace=runtime), terminal=True)
            report_failure(second, observations=SimpleNamespace(trace=runtime), terminal=True)
        span = exporter.get_finished_spans()[0]
        assert span.attributes['api_agent.outcome.cause_id'] == first_call
        assert span.status.description == 'first terminal failure'
        assert len(span.events) == 1


def test_escaping_secondary_error_cannot_overwrite_an_explicit_terminal_outcome():
    with trace_runtime(trace=True) as (runtime, _, exporter):
        with pytest.raises(ProviderAccessError):
            with operation(runtime) as owner:
                owner.report(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.TOOL))
                raise ProviderAccessError('unknown', 'secondary exception')
        span = exporter.get_finished_spans()[0]
        assert span.attributes['error.type'] == 'tool'
        assert span.status.description == 'Operation did not complete successfully.'
        assert not span.events


def test_same_direct_failure_report_has_only_one_event():
    error = ProviderAccessError('unknown', 'same failure')
    with trace_runtime(trace=True) as (runtime, _, exporter):
        span = runtime.begin('failed', {})
        span.failure(error)
        span.failure(error)
        span.end()
        captured = exporter.get_finished_spans()[0]
        assert captured.status.description == 'same failure'
        assert len(captured.events) == 1
