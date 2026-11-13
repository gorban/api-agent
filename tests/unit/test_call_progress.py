"""Request-local call and tool progress reaches an application sink without changing results."""
import asyncio
import json

import pytest

from api_agent.providers.llm import (CompletionRequest, LLMRequest, LLMResult, Message,
    ResponsePolicy, StructuredOutput, TextContent, ToolCall)
from api_agent.providers.presentation import (CallProgress, call_progress, publish_call_progress,
    publish_tool_progress, tool_progress)
from api_agent.workflows.node_summary import ProgressServices
from test_tool_gate import Owner, ToolGate, consume
from test_workflow_candidate_repair import draft, replace_node, workflow
import test_workflow_typed_functions as typed

SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'boolean'}},
          'required': ['answer'], 'additionalProperties': False}


def plain_request(request_id='plain'):
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Say.'),)),)), request_id)


def structured_request(request_id='structured'):
    response = ResponsePolicy('structured', 'allow', StructuredOutput('check', json.dumps(SCHEMA)))
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Check.'),)),),
        response=response), request_id)


async def provider(request):
    if request.payload.response is not None:
        return LLMResult(Message('assistant', (TextContent('{"answer":true}'),)), reasoning_summary='Checked.')
    return LLMResult(Message('assistant', (TextContent('Hello.'),)))


def recorder():
    events = []
    async def sink(*value):
        events.append(value[0] if len(value) == 1 else value)
    return events, sink


def test_legacy_node_reports_structured_and_plain_calls_with_its_step_id():
    flow = workflow()
    flow.services['llm'] = provider
    async def producer(inputs, context):
        await context.services['llm'](plain_request())
        await context.services['llm'](structured_request())
        return draft()
    replace_node(flow, 'first_draft', producer)
    events, sink = recorder()
    with call_progress(sink):
        asyncio.run(flow.arun({'user_input': 'user'}))
    assert [(event.node, event.request_id, event.response_text, event.reasoning_summary)
            for event in events] == [
        ('first_draft', 'plain', 'Hello.', ''), ('first_draft', 'structured', '{"answer":true}', 'Checked.')]
    assert all(isinstance(event.attempt, str) and event.attempt for event in events)


def test_typed_node_reports_its_step_id_without_an_attempt():
    native = typed.NATIVE.replace('def decide(state, context):', 'async def decide(state, context):').replace(
        "    SEEN.append(context.response_request((Message('user', (TextContent('Decide.'),)),), strict=True))\n"
        "    SEEN.append(type(context.services).__name__)\n",
        "    from api_agent.providers.llm import CompletionRequest, LLMRequest\n"
        "    await context.services['llm'](LLMRequest('selected', CompletionRequest("
        "(Message('user', (TextContent('Decide.'),)),)), 'typed'))\n")
    async def llm(request):
        return LLMResult(Message('assistant', (TextContent('Decided.'),)))
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        entry = typed.build(root)
        typed.write(root, 'native.py', native)
        events, sink = recorder()
        with call_progress(sink):
            typed.run(typed.admit(entry), {'topic': 'good'}, services={'llm': llm})
    assert [(event.node, event.attempt, event.request_id, event.response_text) for event in events] == [
        ('decide', None, 'typed', 'Decided.')]


def test_unbound_sink_leaves_node_services_untouched():
    services = {'llm': provider}
    assert ProgressServices.bind(services, 'node', None) is services


@pytest.mark.asyncio
async def test_generated_display_summary_is_reported_with_the_projected_result():
    from api_agent.workflows.inference_history import InferenceHistory
    from api_agent.workflows.node_summary import SummaryServices
    async def envelope(request):
        return LLMResult(Message('assistant', (TextContent(json.dumps(
            {'progress_summary': 'Checked it.', 'result': {'answer': True}})),)))
    events, sink = recorder()
    services = SummaryServices(InferenceHistory().services({'llm': envelope}, 'check'), 'explanation')
    with call_progress(sink):
        wrapped = ProgressServices.bind(services, 'check', 'attempt-1')
        await wrapped['llm'](structured_request())
    assert events == [CallProgress('check', 'attempt-1', 'structured', '{"answer": true}', 'Explanation: Checked it.')]


@pytest.mark.asyncio
async def test_tool_calls_are_reported_without_leaking_into_response_text():
    async def calling(request):
        return LLMResult(Message('assistant', calls=(ToolCall('c1', 'lookup', '{"reason":"why"}'),)), outcome='tool_calls')
    events, sink = recorder()
    with call_progress(sink):
        await ProgressServices.bind({'complete': calling}, 'plan', None)['complete'](plain_request())
    assert events[0].response_text == '' and events[0].tool_calls == (('lookup', '{"reason":"why"}'),)


@pytest.mark.asyncio
async def test_services_view_is_a_borrowed_mutable_mapping():
    original = {'plain': object(), 'llm': provider, 'complete': None}
    _, sink = recorder()
    with call_progress(sink):
        services = ProgressServices.bind(original, 'node', None)
    assert services['plain'] is original['plain'] and services['complete'] is None
    services['next'] = 1
    assert dict(services).keys() == original.keys() and len(services) == 4
    del services['next']
    assert 'next' not in original


@pytest.mark.asyncio
async def test_publishing_without_a_sink_does_nothing():
    await publish_call_progress('node', None, 'plain', await provider(plain_request()))
    await publish_tool_progress('start', 'lookup')


@pytest.mark.asyncio
async def test_sinks_are_scoped_to_their_context():
    events, sink = recorder()
    with call_progress(sink), tool_progress(sink):
        await publish_tool_progress('start', 'lookup')
    await publish_tool_progress('end', 'lookup')
    assert events == [('start', 'lookup')]


def test_tool_start_and_end_reach_the_sink_around_dispatch():
    owner = Owner('graphql')
    events, sink = recorder()
    original = owner.invoke
    async def invoke(prepared):
        events.append('invoke')
        return await original(prepared)
    owner.invoke = invoke
    async def run():
        gate = ToolGate(owner, invocation_id='turn-1', tool_alias='lookup')
        prepared = await gate.prepare({'query': 'read'})
        checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        with tool_progress(sink):
            await gate.execute(checked)
    asyncio.run(run())
    assert events == [('start', 'lookup'), 'invoke', ('end', 'lookup')]


def test_failed_tool_still_reports_its_end_before_the_error_propagates():
    owner = Owner('graphql')
    async def invoke(prepared):
        raise ValueError('boom')
    owner.invoke = invoke
    events, sink = recorder()
    async def run():
        gate = ToolGate(owner, invocation_id='turn-1')
        prepared = await gate.prepare({'query': 'read'})
        checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        with tool_progress(sink), pytest.raises(ValueError, match='boom'):
            await gate.execute(checked)
    asyncio.run(run())
    assert events == [('start', 'lookup'), ('end', 'lookup')]


def test_cancelled_tool_reports_no_end():
    owner = Owner('graphql')
    events, sink = recorder()
    async def run():
        gate_started = asyncio.Event()
        async def invoke(prepared):
            gate_started.set()
            await asyncio.sleep(60)
        owner.invoke = invoke
        gate = ToolGate(owner, invocation_id='turn-1')
        prepared = await gate.prepare({'query': 'read'})
        checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        with tool_progress(sink):
            task = asyncio.create_task(gate.execute(checked))
            await gate_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert events == [('start', 'lookup')]


def test_tool_result_is_unchanged_by_progress():
    owner = Owner('graphql')
    _, sink = recorder()
    with tool_progress(sink):
        result, _, _ = asyncio.run(consume(owner))
    assert result.data == {'source': 'graphql'}
