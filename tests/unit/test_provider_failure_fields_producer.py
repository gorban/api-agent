"""Embedding applications attach classified provider failure facts to the active node span."""
import asyncio
import logging

import pytest

from api_agent.telemetry.failure_vocabulary import provider_failure_vocabulary_scope
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.inference_capture import record_provider_failure_fields
from api_agent.workflows.observations import WorkflowObservationFactory
from test_trace_failure_classification import trace_runtime
from test_workflow_candidate_repair import draft, replace_node, workflow
from test_workflow_observations import definition

SENTINEL = 'synthetic-provider-narrative'
FAILURE = 'api_agent.provider.failure.'


def _fields(**overrides):
    return {
        FAILURE + 'class': 'policy_block',
        FAILURE + 'http_status': 400,
        FAILURE + 'categories': ['example_category'],
        FAILURE + 'category_source': 'inferred',
        'api_agent.provider.retry.attempt': 1,
        'api_agent.provider.retry.total': 2,
        'api_agent.provider.operation': 'example_operation',
        **overrides,
    }


class _Span:
    def __init__(self, fail=False):
        self.applied, self.fail = [], fail

    def structural(self, payload):
        if self.fail:
            raise RuntimeError('span rejected the payload')
        self.applied.append(payload)


@pytest.fixture
def observations():
    with trace_runtime() as (runtime, resolved, exporter):
        created = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime).create(definition())
        yield created
        created.close()


def test_registered_failure_facts_land_on_the_running_node_span_and_nothing_else():
    flow = workflow()
    async def node(inputs, context):
        record_provider_failure_fields(_fields(**{
            'gen_ai.request.model': 'example-model',
            'api_agent.failure.code': 'workflow',
            FAILURE + 'message': SENTINEL,
            FAILURE + 'type': SENTINEL,
        }))
        return draft()
    replace_node(flow, 'first_draft', node)
    with provider_failure_vocabulary_scope(categories=['example_category']):
        with trace_runtime() as (runtime, resolved, exporter):
            flow.services['observation_factory'] = WorkflowObservationFactory(
                resolved, boundary=EmissionBoundary(), trace=runtime)
            asyncio.run(flow.arun({'user_input': 'user'}))
            spans = exporter.get_finished_spans()
    span = next(span for span in spans if span.name == 'node first_draft')
    assert {key: span.attributes[key] for key in span.attributes if key.startswith('api_agent.provider.')} == {
        FAILURE + 'class': 'policy_block', FAILURE + 'http_status': 400,
        FAILURE + 'categories': ('example_category',), FAILURE + 'category_source': 'inferred',
        'api_agent.provider.retry.attempt': 1, 'api_agent.provider.retry.total': 2,
        'api_agent.provider.operation': 'example_operation'}
    assert 'gen_ai.request.model' not in span.attributes
    assert 'api_agent.failure.code' not in span.attributes
    assert SENTINEL not in repr([dict(span.attributes) for span in spans])
    other = [span for span in spans if span.name != 'node first_draft']
    assert all(not key.startswith('api_agent.provider.') for span in other for key in span.attributes)


def test_unregistered_category_is_dropped_by_the_span_sanitizer_not_by_the_producer():
    flow = workflow()
    async def node(inputs, context):
        record_provider_failure_fields(_fields())
        return draft()
    replace_node(flow, 'first_draft', node)
    with trace_runtime() as (runtime, resolved, exporter):
        flow.services['observation_factory'] = WorkflowObservationFactory(
            resolved, boundary=EmissionBoundary(), trace=runtime)
        asyncio.run(flow.arun({'user_input': 'user'}))
        span = next(span for span in exporter.get_finished_spans() if span.name == 'node first_draft')

    assert FAILURE + 'categories' not in span.attributes
    assert span.attributes[FAILURE + 'class'] == 'policy_block'


def test_nothing_happens_without_an_active_workflow_visit():
    assert record_provider_failure_fields(_fields()) is None


def test_visit_whose_owner_has_no_failure_producer_is_ignored():
    with bind_workflow_visit(object(), 'visit'):
        assert record_provider_failure_fields(_fields()) is None


def test_only_provider_prefixed_keys_reach_the_span(observations):
    span = _Span()
    observations._spans['visit'] = span

    with bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields({**_fields(), 'gen_ai.request.model': 'm', 7: 'x', 'other': 1,
                                        'api_agent.providers.lookalike': 1})

    assert span.applied == [_fields()]


def test_nothing_is_applied_when_no_key_is_provider_prefixed(observations):
    span = _Span()
    observations._spans['visit'] = span

    with bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields({'gen_ai.request.model': 'm', 3: 4})

    assert span.applied == []


@pytest.mark.parametrize('fields', [None, [], 'text', ('api_agent.provider.operation', 'x')])
def test_non_mapping_fields_are_ignored(observations, fields):
    span = _Span()
    observations._spans['visit'] = span

    with bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields(fields)

    assert span.applied == []


def test_a_visit_without_a_node_span_is_ignored(observations):
    span = _Span()
    observations._spans['other'] = span

    with bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields(_fields())

    assert span.applied == []


def test_a_visit_owned_by_another_observer_is_ignored(observations):
    span = _Span()
    observations._spans['visit'] = span

    with bind_workflow_visit(object(), 'visit'):
        observations.provider_failure_fields(_fields())

    assert span.applied == []


def test_failure_facts_are_ignored_outside_any_visit(observations):
    span = _Span()
    observations._spans['visit'] = span

    observations.provider_failure_fields(_fields())

    assert span.applied == []


def test_closed_observations_ignore_late_failure_facts(observations):
    span = _Span()
    observations._spans['visit'] = span
    observations.close()
    observations._spans['visit'] = span

    with bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields(_fields())

    assert span.applied == []


def test_a_failing_span_never_fails_the_caller_and_is_reported_without_content(observations, caplog):
    observations._spans['visit'] = _Span(fail=True)

    with caplog.at_level(logging.ERROR), bind_workflow_visit(observations, 'visit'):
        record_provider_failure_fields(_fields(**{FAILURE + 'message': SENTINEL}))

    assert 'Tracing observation failed' in caplog.text
    assert SENTINEL not in caplog.text
