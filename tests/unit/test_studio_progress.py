"""Studio streams application-projected call progress and tool lifecycle frames in order."""
import asyncio
import json

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.providers.presentation import CallProgress, present_summary, publish_call_progress, publish_tool_progress
from test_studio_bridge import payload, request, setup_bridge


def progress(node, text, attempt='a1', summary=''):
    return CallProgress(node, attempt, 'call', text, summary)


async def frames(backend, capability='chat_stream'):
    response = await backend.handle(request('POST', payload()), capability)
    if capability != 'chat_stream':
        return json.loads(response.content)
    chunks = [part.decode() async for part in response.streaming_content]
    return [(part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:])) for part in chunks]


def workflow(*steps):
    """A fake application whose model calls and tools publish through the engine hooks."""
    async def complete(value, *, auth, context):
        for step in steps:
            if type(step) is CallProgress:
                await publish_call_progress(step.node, step.attempt, step.request_id, LLMResult(
                    Message('assistant', (TextContent(step.response_text),)),
                    reasoning_summary=step.reasoning_summary or None))
            elif step == 'fail':
                raise ValueError('workflow failed')
            else:
                await publish_tool_progress(*step)
        return LLMResult(Message('assistant', (TextContent('hello'),)))
    return complete


def run(scenario):
    asyncio.run(scenario())


def test_projected_text_and_tool_frames_precede_the_answer_in_workflow_order():
    async def projector(update):
        return f'{update.node}: {update.response_text}'
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(
            progress('plan', 'one'), ('start', 'lookup'), ('end', 'lookup'), progress('write', 'two', 'b1')))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        names = [name for name, _ in events]
        assert names == ['session', 'thinking', 'reasoning_token', 'tool_start', 'tool_end',
                         'reasoning_token', 'token', 'response_complete', 'result', 'done']
        by_name = [value for name, value in events if name in ('reasoning_token', 'tool_start', 'tool_end')]
        assert by_name == [{'content': 'plan: one', 'section': 'plan:a1'}, {'tool': 'lookup'},
                           {'tool': 'lookup'}, {'content': 'write: two', 'section': 'write:b1'}]
    run(scenario)


def test_section_is_stable_for_a_node_attempt_and_changes_with_the_attempt():
    async def projector(update):
        return update.response_text
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(
            progress('plan', 'a', 'a1'), progress('plan', 'b', 'a1'), progress('plan', 'c', 'a2'),
            progress('plan', 'd', None)))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert [value['section'] for name, value in events if name == 'reasoning_token'] == [
            'plan:a1', 'plan:a1', 'plan:a2', 'plan']
    run(scenario)


@pytest.mark.parametrize('result', [None, '', 7, b'bytes'])
def test_empty_or_invalid_projection_adds_no_frame(result):
    async def projector(update):
        return result
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(progress('plan', 'one')))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert [name for name, _ in events] == ['session', 'thinking', 'token', 'response_complete', 'result', 'done']
    run(scenario)


def test_projector_failure_skips_the_frame_and_records_only_a_fixed_classification():
    recorded = []
    async def projector(update):
        if update.response_text == 'bad':
            raise ValueError('private model text')
        return update.response_text
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, on_error=recorded.append, complete=workflow(
            progress('plan', 'bad'), progress('plan', 'good')))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert [value['content'] for name, value in events if name == 'reasoning_token'] == ['good']
        assert events[-1][0] == 'done'
        result = next(value for name, value in events if name == 'result')
        assert result['response_text'] == 'hello'
    run(scenario)
    assert [str(error) for error in recorded] == ['Application progress projection failed.']


def test_projection_runs_off_the_workflow_path_but_finishes_before_the_answer():
    order = []
    async def scenario():
        gate = asyncio.Event()
        async def projector(update):
            await gate.wait()
            await asyncio.sleep(.05)
            order.append('projected')
            return 'late'
        async def complete(value, *, auth, context):
            await publish_call_progress('plan', 'a1', 'call', LLMResult(Message('assistant', (TextContent('x'),))))
            order.append('workflow continued')
            gate.set()
            return LLMResult(Message('assistant', (TextContent('hello'),)))
        backend, _ = setup_bridge(progress_projector=projector, complete=complete)
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert order == ['workflow continued', 'projected']
        assert [name for name, _ in events][2:4] == ['reasoning_token', 'token']
    run(scenario)


def test_projector_model_calls_are_not_reported_back_to_itself():
    seen = []
    async def projector(update):
        seen.append(update.response_text)
        await publish_call_progress('friendly', None, 'call', LLMResult(Message('assistant', (TextContent('nested'),))))
        await publish_tool_progress('start', 'nested')
        return 'shown'
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(progress('plan', 'one')))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert [name for name, _ in events if name.startswith('tool') or name == 'reasoning_token'] == ['reasoning_token']
    run(scenario)
    assert seen == ['one']


def test_provider_summaries_are_replaced_by_the_projection_when_one_is_bound():
    async def complete(value, *, auth, context):
        await present_summary(LLMResult(Message('assistant', (TextContent('x'),)), reasoning_summary='raw reasoning'), request_id='r')
        await publish_call_progress('plan', 'a1', 'call', LLMResult(Message('assistant', (TextContent('x'),)), reasoning_summary='raw reasoning'))
        return LLMResult(Message('assistant', (TextContent('hello'),)))
    async def projector(update):
        return 'friendly'
    async def scenario(**changes):
        backend, _ = setup_bridge(complete=complete, **changes)
        try:
            return [value['content'] for name, value in await frames(backend) if name == 'reasoning_token']
        finally:
            await backend.aclose()
    assert asyncio.run(scenario(progress_projector=projector)) == ['friendly']
    assert asyncio.run(scenario()) == ['raw reasoning']


def test_without_a_projector_hooks_publish_nothing_and_frames_are_unchanged():
    async def scenario():
        backend, _ = setup_bridge(complete=workflow(progress('plan', 'one'), ('start', 'lookup'), ('end', 'lookup')))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert [name for name, _ in events] == ['session', 'thinking', 'token', 'response_complete', 'result', 'done']
    run(scenario)


def test_buffered_chat_never_calls_the_projector():
    calls = []
    async def projector(update):
        calls.append(update)
        return 'text'
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(progress('plan', 'one'), ('start', 'lookup')))
        try:
            result = await frames(backend, 'chat')
        finally:
            await backend.aclose()
        assert result['response_text'] == 'hello'
    run(scenario)
    assert calls == []


def test_projected_text_is_bounded_and_overflow_is_dropped_without_failing_the_turn():
    async def projector(update):
        return 'x' * 40000
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(
            *(progress('plan', 'one') for _ in range(4))))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert sum(len(value['content']) for name, value in events if name == 'reasoning_token') <= 65536
        assert len([name for name, _ in events if name == 'reasoning_token']) == 1
        assert events[-1][0] == 'done'
    run(scenario)


def test_frame_count_is_bounded_and_the_turn_still_completes():
    async def projector(update):
        return 'x'
    async def scenario():
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(
            *(('start', 'lookup') for _ in range(5000))))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert len([name for name, _ in events if name == 'tool_start']) == 4096
        assert events[-1][0] == 'done'
    run(scenario)


def test_failed_turn_cancels_pending_projection():
    started = []
    async def scenario():
        async def projector(update):
            started.append(update)
            await asyncio.sleep(60)
        backend, _ = setup_bridge(progress_projector=projector, complete=workflow(progress('plan', 'one'), 'fail'))
        try:
            events = await frames(backend)
        finally:
            await backend.aclose()
        assert events[-1][0] == 'error'
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks() if 'worker' in repr(task.get_coro())]
    run(scenario)
    assert started


def test_projector_must_be_callable():
    with pytest.raises(ValueError, match='progress projector'):
        setup_bridge(progress_projector='not callable')


@pytest.mark.parametrize('name,value', [('reasoning_token', {'content': 'text', 'section': 'plan:a1'}),
                                        ('tool_start', {'tool': 'lookup'}), ('tool_end', {'tool': 'lookup'})])
def test_progress_frames_are_valid_application_events(name, value):
    from api_agent.api import chat_contract
    body = chat_contract.frame(name, value).decode()
    assert body.startswith(f'event: {name}\ndata: ') and json.loads(body.splitlines()[1][6:]) == value
