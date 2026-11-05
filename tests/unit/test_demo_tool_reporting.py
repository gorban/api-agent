"""A selected tool result has one lifecycle, independent of inference accounting."""
import asyncio

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.providers.control import OperationContext
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.observations import WorkflowObservationFactory
from test_demo_application import Owner, LLM, application, request, verdict


@pytest.mark.parametrize('content', [False, True])
def test_demo_tool_has_one_lifecycle_with_policy_owned_partial_payload(content):
    events = []
    values = {'LOG_CONTENT': content, 'TRACELOOP_TRACE_CONTENT': False}
    config = ConfigResult(values, {key: ResolvedValue(value, 'cli', True)
        for key, value in values.items()}, (), (), ())
    graph = application(Owner())
    graph.config = values
    graph.observations = WorkflowObservationFactory(config,
        boundary=EmissionBoundary(ContentPolicy(content)), sink=events.append)
    llm = LLM(['{"use_tool":true,"arguments":{"query":"{ item { name } }"},"answer":""}',
               verdict(), 'Ada'])

    asyncio.run(graph.complete(request(), llm=llm, context=OperationContext('tool-report')))

    assert not any(event['event'] == 'call' for event in events)
    tool = [event for event in events if event.get('tool_invocation_id')]
    assert [event['event'] for event in tool] == ['tool_start', 'capture', 'capture', 'tool_end']
    assert tool[-1]['outcome'] == 'partial'
    captures = [event for event in tool if event['event'] == 'capture']
    assert all(('values' in event) is content for event in captures)
    if content:
        assert captures[1]['values']['data'] == {'item': {'name': 'Ada'}}
        assert tuple(dict(item) for item in captures[1]['values']['errors']) == ({'message': 'optional field failed'},)
    else:
        assert {event['reason'] for event in captures} == {'withheld'}


def test_demo_final_tool_cause_matches_the_actual_lifecycle_identity():
    from types import SimpleNamespace
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.tool_gate import ToolGate
    from test_tool_observations import observed_journal, VISIT

    async def scenario():
        journal, boundary = observed_journal()
        graph = application(Owner())
        gate = ToolGate(Owner(), invocation_id='demo-operation')
        prepared = await gate.prepare({'query': '{ item { name } }'})
        grant = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        store = {}
        context = SimpleNamespace(services={'verified_call': grant, 'result_store': store},
                                  tools={'primary': gate})
        with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
            await graph.registry['execute_tool'].invoke({}, context)
        ended = next(event for event in journal.finalize()['events'] if event['event'] == 'tool_end')
        assert store['tool_outcome'].cause_id == ended['tool_invocation_id'] == grant.gate_id
        assert store['tool_outcome'].state.value == ended['outcome'] == 'partial'

    asyncio.run(scenario())


def test_legacy_call_after_typed_tool_start_does_not_create_a_second_trace_span():
    from types import SimpleNamespace
    from test_tool_observations import TraceRecorder
    from api_agent.workflows.attribution import current_workflow_visit

    class Tracing(TraceRecorder):
        def __init__(self):
            super().__init__(False)
            self.started = []

        def begin(self, name, *args, **kwargs):
            self.started.append(name)
            return SimpleNamespace(event=self.events.append, attributes=lambda _: None,
                                   outcome=lambda _: None, end=lambda **kwargs: None)

    class LegacyOwner(Owner):
        async def invoke(self, prepared):
            result = await super().invoke(prepared)
            journal, visit, attempt = current_workflow_visit()
            journal.record_call(visit_id=visit, attempt_id=attempt, elapsed=0,
                                outcome='partial')
            return result

    tracing, events = Tracing(), []
    values = {'LOG_CONTENT': False, 'TRACELOOP_TRACE_CONTENT': False}
    config = ConfigResult(values, {key: ResolvedValue(value, 'cli', True)
        for key, value in values.items()}, (), (), ())
    graph = application(LegacyOwner())
    graph.observations = WorkflowObservationFactory(config,
        boundary=EmissionBoundary(ContentPolicy(False)), trace=tracing, sink=events.append)
    llm = LLM(['{"use_tool":true,"arguments":{"query":"{ item { name } }"},"answer":""}',
               verdict(), 'Ada'])

    asyncio.run(graph.complete(request(), llm=llm, context=OperationContext('legacy-tool-report')))

    assert tracing.started.count('execute_tool') == 1
    assert not any(name.startswith('chat ') for name in tracing.started)
    assert len([event for event in events if event['event'] == 'call']) == 1
    assert len([event for event in events if event['event'] == 'tool_start']) == 1
