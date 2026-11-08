"""SDK call panels retain upstream loss facts without inventing payload content."""
import asyncio
import json
import time
from uuid import uuid4

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import collector, config, definition


def attributes(rows):
    return {row.key: next(iter(row.value.ListFields()))[1] for row in rows}


def export_panels(*, log=False, trace=True, values=None, reason=None, source='fallback', physical=True):
    with collector() as (endpoint, received):
        resolved = config(log=log, trace=trace, TRACE_EXPORT_ENDPOINT=endpoint)
        runtime = TraceRuntime(resolved, boundary=EmissionBoundary())
        call, attempt = str(uuid4()), str(uuid4())
        async def run():
            await runtime.astart()
            try:
                adapter = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(),
                    trace=runtime).create(definition())
                graph, node = str(uuid4()), str(uuid4())
                adapter.observe(dict(event='scope_start', scope_id=graph, owner='GRAPH', kind='graph'))
                adapter.observe(dict(event='scope_start', scope_id=node, parent_id=graph, owner='Make', kind='node'))
                adapter.capture_json(visit_id=node, direction='input', cause_id=call,
                    transport_attempt_id=attempt if physical else None, capture_source=source,
                    capture_reason=reason, values=values, schema={'type': 'object'})
                adapter.capture_json(visit_id=node, direction='output', cause_id=call,
                    transport_attempt_id=attempt, capture_source='provider',
                    values={'llm_response': {'text': 'ordinary result'}}, schema={'type': 'object'})
                now = int(time.time() * 1000)
                ledger = {'version': 1, 'complete': True, 'attempts': [{
                    'id': attempt, 'index': 1, 'execution': 'completed',
                    'usage': {'input_tokens': 11, 'output_tokens': 2,
                              'cached_input_tokens': None, 'reasoning_tokens': None},
                    'billing': None, 'quote': None, 'retry_delay_seconds': None,
                    'started_ms': now - 1, 'stopped_ms': now, 'outcome': 'completed',
                    'status': None, 'error_kind': None, 'response': None}]}
                adapter.record_call(call_id=call, visit_id=node,
                    usage={'input_tokens': 11, 'output_tokens': 2}, transport_attempts=ledger)
                adapter.observe(dict(event='scope_end', scope_id=node, owner='Make', kind='node', outcome='passed'))
                adapter.observe(dict(event='scope_end', scope_id=graph, owner='GRAPH', kind='graph', outcome='passed'))
                adapter.close()
                adapter.finalize()
            finally:
                await runtime.aclose()
        asyncio.run(run())
        spans = [span for _, body in received for resource in
            ExportTraceServiceRequest.FromString(body).resource_spans for scope in resource.scope_spans
            for span in scope.spans]
        logical = next(span for span in spans if attributes(span.attributes).get('api_agent.call_id') == call)
        transport = next(span for span in spans if attributes(span.attributes).get('api_agent.transport_id') == attempt)
        assert transport.parent_span_id == logical.span_id
        return logical, transport


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_request_overflow_marker_reaches_matching_call_panels_after_successful_output(log, trace):
    panels = export_panels(log=log, trace=trace,
        values={'llm_request': {'text': 'must-never-export-' + 'x' * 65536}})
    for panel in panels:
        tags = attributes(panel.attributes)
        assert tags['api_agent.capture.reason'] == ('truncated' if trace else 'withheld')
        assert tags['api_agent.capture.phase'] == ('request' if trace else 'response')
        markers = [attributes(event.attributes) for event in panel.events]
        assert any(row.get('api_agent.capture.reason') == ('truncated' if trace else 'withheld')
                   and row.get('api_agent.capture.phase') == 'request'
                   and row.get('api_agent.capture.source') == 'fallback' for row in markers)
        assert 'gen_ai.input.messages' not in tags
        assert 'must-never-export' not in json.dumps(tags)
        assert tags.get('gen_ai.usage.input_tokens', 11) == 11


@pytest.mark.parametrize('reason', ['partial', 'unavailable', 'withheld'])
def test_empty_upstream_loss_is_structural_on_both_call_panels(reason):
    for panel in export_panels(values={}, reason=reason):
        tags = attributes(panel.attributes)
        assert tags['api_agent.capture.reason'] == reason
        assert tags['api_agent.capture.phase'] == 'request'
        assert tags['api_agent.capture.source'] == 'fallback'
        assert 'gen_ai.input.messages' not in tags


def test_capture_without_transport_identity_does_not_label_an_unmatched_physical_panel():
    logical, physical = export_panels(values={}, reason='partial', source=None, physical=False)
    tags = attributes(logical.attributes)
    assert tags['api_agent.capture.reason'] == 'partial'
    assert 'api_agent.capture.source' not in tags
    assert 'api_agent.capture.reason' not in attributes(physical.attributes)


def test_call_capture_retention_stays_bounded_and_existing_phases_can_finish():
    from unittest.mock import Mock
    from api_agent.telemetry.privacy import ContentPolicy
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    adapter = WorkflowObservationFactory(config(trace=True), boundary=EmissionBoundary(),
        trace=runtime).create(definition())
    visit = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    pairs = [(str(uuid4()), str(uuid4())) for _ in range(257)]
    for cause, attempt in pairs:
        adapter.capture_json(visit_id=visit, direction='input', cause_id=cause,
            transport_attempt_id=attempt, capture_source='fallback', capture_reason='truncated',
            values={}, schema={'type': 'object'})
    assert len(adapter._call_payloads) == 256 and pairs[-1] not in adapter._call_payloads
    cause, attempt = pairs[0]
    adapter.capture_json(visit_id=visit, direction='output', cause_id=cause,
        transport_attempt_id=attempt, capture_source='provider', values={}, schema={'type': 'object'})
    assert set(adapter._call_payloads[pairs[0]]) == {'input', 'output'}
