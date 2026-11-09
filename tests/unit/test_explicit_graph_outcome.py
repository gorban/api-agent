"""Explicit semantic outcomes agree across the journal, graph and request."""
import asyncio

from langgraph.graph.message import add_messages
from opentelemetry.trace import StatusCode
import pytest

from api_agent.telemetry.operation import operation
from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeCause, OutcomeState
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.workflows.observations import WorkflowObservationFactory
from api_agent.workflows.execution import (Workflow)
from api_agent.workflows.native import (load_native_bindings)
from api_agent.workflows.definition import (load_definition)
from test_trace_failure_classification import trace_runtime
from test_workflow_candidate_repair import workflow, replace_node, draft
from test_workflow_stateful import fixture_graph as fixture_graph


@pytest.mark.parametrize('engine', ['immutable', 'stateful'])
@pytest.mark.parametrize('state,label', [
    (OutcomeState.SUCCESS, 'passed'),
    (OutcomeState.FAILURE, 'failed'),
    (OutcomeState.CANCELLED, 'cancelled'),
    (OutcomeState.PARTIAL, 'partial'),
    (OutcomeState.UNKNOWN, 'unknown'),
])
def test_explicit_outcome_preserves_successful_nodes_and_truthful_graph(
        fixture_graph, engine, state, label):
    outcome = ExecutionOutcome(state, OutcomeCause.VALIDATION
        if state in (OutcomeState.FAILURE, OutcomeState.PARTIAL) else None)
    events = []
    services = {'execution_outcome': lambda: outcome, 'observe': events.append}
    if engine == 'stateful':
        root, entry = fixture_graph
        definition = load_definition(entry, base_root=root, pack_root=root)
        flow = Workflow(definition, catalog=load_native_bindings(definition),
            reducers={'messages': add_messages}, services=services)
        async def invoke():
            result = await flow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='synthetic')
            assert result['visits'] == 1
    else:
        flow = workflow()
        async def producer(inputs, context):
            return draft()
        replace_node(flow, 'first_draft', producer)
        flow.services.update(services)
        async def invoke():
            result = await flow.arun({'user_input': 'user'})
            assert result['first_draft'] == draft()

    with trace_runtime() as (runtime, resolved, exporter):
        factory = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime)
        if engine == 'stateful':
            flow = Workflow(flow.definition, catalog=flow.catalog, reducers=flow.reducers,
                services={**services, 'observation_factory': factory})
        else:
            flow.services['observation_factory'] = factory
        with operation(runtime) as owner:
            asyncio.run(invoke())
        assert owner.final == outcome
        spans = exporter.get_finished_spans()
        graph = next(span for span in spans if span.name == 'graph GRAPH')
        request = next(span for span in spans if span.name == 'request')
        assert graph.attributes['api_agent.outcome.state'] == state.value
        assert request.attributes['api_agent.outcome.state'] == state.value
        expected_status = StatusCode.ERROR if state in (OutcomeState.FAILURE, OutcomeState.PARTIAL) else StatusCode.UNSET
        assert graph.status.status_code == request.status.status_code == expected_status
        if outcome.cause is not None:
            assert graph.attributes['error.type'] == request.attributes['error.type'] == outcome.cause.value
    graph_end = next(row for row in events if row['event'] == 'scope_end' and row['owner'] == 'GRAPH')
    assert graph_end['outcome'] == label
    assert all(row['outcome'] == 'passed' for row in events
        if row['event'] == 'scope_end' and row['kind'] == 'node')
