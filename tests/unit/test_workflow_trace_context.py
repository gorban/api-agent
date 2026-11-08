"""Retained trace correlation identifies the explicit graph after span closure."""
from unittest.mock import Mock
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_observations import config, definition


CONTEXT = {'version': 1, 'trace_id': 'a' * 32, 'span_id': 'b' * 16, 'trace_flags': 1}


def observations(context):
    span = Mock()
    span.export_context.return_value = context
    runtime = Mock(active=True, is_model_identity=lambda _: False)
    runtime.boundary = EmissionBoundary()
    runtime.begin.return_value = span
    adapter = WorkflowObservationFactory(config(), boundary=EmissionBoundary(), trace=runtime).create(definition())
    return adapter, span


def test_graph_context_is_retained_after_graph_closes_and_returns_a_copy():
    adapter, span = observations(CONTEXT)
    assert adapter.export_trace_context() is None
    scope = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=scope, owner='GRAPH', kind='graph'))
    adapter.observe(dict(event='scope_end', scope_id=scope, owner='GRAPH', kind='graph', outcome='passed'))
    span.export_context.return_value = None
    retained = adapter.export_trace_context()
    assert retained == {**CONTEXT, 'root_kind': 'graph'}
    retained['trace_id'] = 'changed'
    assert adapter.export_trace_context()['trace_id'] == CONTEXT['trace_id']


@pytest.mark.parametrize('context', [None, {}, {'version': 1, 'trace_id': 'private'}])
def test_invalid_graph_context_is_not_retained(context):
    adapter, _ = observations(context)
    adapter.observe(dict(event='scope_start', scope_id=str(uuid4()), owner='GRAPH', kind='graph'))
    assert adapter.export_trace_context() is None


def test_node_context_cannot_replace_graph_identity():
    adapter, _ = observations(CONTEXT)
    adapter.observe(dict(event='scope_start', scope_id=str(uuid4()), owner='Make', kind='node'))
    assert adapter.export_trace_context() is None


def test_nested_graph_does_not_replace_root_context():
    adapter, span = observations(CONTEXT)
    root = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=root, owner='GRAPH', kind='graph'))
    span.export_context.return_value = {**CONTEXT, 'span_id': 'c' * 16}
    adapter.observe(dict(event='scope_start', scope_id=str(uuid4()), parent_id=root,
                         owner='nested', kind='graph'))
    assert adapter.export_trace_context()['span_id'] == CONTEXT['span_id']
