"""Displayable summaries are separate from answers across the normal bridge."""
import asyncio
import json
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import result_from_wire, result_to_wire
from api_agent.providers.llm import LLMResult, Message, TextContent, StreamEvent, ToolCall
from api_agent.providers.presentation import present_summary, summary_delivery
from test_studio_bridge import setup_bridge, request, payload


def result(summary='Checked the requested format.'):
    return LLMResult(Message('assistant', (TextContent('Ok'),)), reasoning_summary=summary)


def test_summary_wire_roundtrip_does_not_change_answer_or_repr():
    value = result()
    wire = result_to_wire(value)
    assert wire['text'] == 'Ok'
    assert wire['reasoning_summary'] == value.reasoning_summary
    assert result_from_wire(wire) == value
    assert value.reasoning_summary not in repr(value)
    assert 'reasoning_summary' not in result_to_wire(result(None))
    assert result_from_wire(result_to_wire(result(''))).reasoning_summary == ''


def test_rich_tool_result_preserves_summary_without_modifying_call():
    value = LLMResult(Message('assistant', (), (ToolCall('c', 'lookup', '{}'),)),
                      outcome='tool_calls', reasoning_summary='Checking a source.')
    assert result_from_wire(result_to_wire(value)) == value


@pytest.mark.parametrize('summary', [4, [], 'é' * 32769])
def test_summary_contract_rejects_nontext_and_oversized_bytes(summary):
    with pytest.raises(ValueError):
        result(summary)
    with pytest.raises(ContractError):
        result_from_wire(dict(text='Ok', outcome='completed', usage=result_to_wire(result(None))['usage'],
                              reasoning_summary=summary))


def test_summary_delivery_is_request_local_and_resets_on_error():
    async def run():
        observations = [[], []]
        async def task(index):
            async def sink(text):
                observations[index].append(text)
            with pytest.raises(RuntimeError), summary_delivery(sink):
                await asyncio.sleep(0)
                await present_summary(result(str(index)))
                await present_summary(result(None))
                raise RuntimeError('synthetic')
            await present_summary(result('not delivered'))
        await asyncio.gather(task(0), task(1))
        assert observations == [['0'], ['1']]
    asyncio.run(run())


def test_buffered_summary_event_precedes_final_answer():
    async def complete(value, **options):
        return result()
    async def run():
        backend, _ = setup_bridge(complete=complete)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            frames = [part.decode() async for part in response.streaming_content]
            assert [frame.splitlines()[0] for frame in frames] == [
                'event: session', 'event: thinking', 'event: reasoning_token', 'event: token',
                'event: response_complete', 'event: result', 'event: done']
            final = json.loads(frames[-2].split('data: ')[1])
            assert final['response_text'] == 'Ok'
            assert final['reasoning_summary'] == result().reasoning_summary
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_corrected_terminal_summary_replaces_streamed_summary():
    async def stream(value, **options):
        yield StreamEvent(TextContent('Interim summary.'), kind='reasoning_summary')
        yield StreamEvent(TextContent('Ok'))
        yield StreamEvent(result('Corrected summary.'), kind='completed')
    async def run():
        backend, _ = setup_bridge(stream=stream)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            frames = [part.decode() async for part in response.streaming_content]
            final = json.loads(next(frame for frame in frames if frame.startswith('event: result')).split('data: ')[1])
            assert final['response_text'] == 'Ok'
            assert final['reasoning_summary'] == 'Corrected summary.'
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_physical_retry_keeps_discarded_progress_out_of_authoritative_summary():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.control import OperationalError
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.presentation import complete_with_progress

    async def run():
        attempts = []

        class Provider:
            capabilities = SimpleNamespace(operations=('stream',))

            async def astream(self, value, *, auth):
                attempts.append(value.request_id)
                if len(attempts) == 1:
                    yield StreamEvent(TextContent('Discarded attempt.'), kind='reasoning_summary')
                    raise OperationalError('network', execution='possibly_executed')
                yield StreamEvent(TextContent('Successful attempt.'), kind='reasoning_summary')
                yield StreamEvent(TextContent('Ok'))
                yield StreamEvent(result('Successful attempt.'), kind='completed')

        async def call(callback):
            return await callback(auth=object())

        provider = Provider()
        async def invoke(value, *, auth):
            return await complete_with_progress(provider, value, auth=auth)
        shared = SharedSetupOwner('progress-operation', 'progress-provider',
            lambda _: SimpleNamespace(call=call, aclose=AsyncMock()), complete=invoke, max_attempts=2)
        async def complete(value, **options):
            final = await shared.complete(value)
            await present_summary(final, request_id=value.request_id)
            return final
        backend, _ = setup_bridge(complete=complete)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            frames = [frame.decode() async for frame in response.streaming_content]
            assert any('Discarded attempt.' in frame for frame in frames if frame.startswith('event: reasoning_token'))
            final = json.loads(next(frame for frame in frames if frame.startswith('event: result')).split('data: ')[1])
            assert final['reasoning_summary'] == 'Successful attempt.'
            assert final['response_text'] == 'Ok'
            assert len(attempts) == 2
        finally:
            await backend.aclose()
            await shared.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('tail', ['duplicate', 'late_summary'])
def test_late_or_duplicate_stream_completion_is_failure(tail):
    async def stream(value, **options):
        yield StreamEvent(TextContent('Ok'))
        yield StreamEvent(result(), kind='completed')
        yield (StreamEvent(result(), kind='completed') if tail == 'duplicate'
               else StreamEvent(TextContent('late'), kind='reasoning_summary'))
    async def run():
        backend, _ = setup_bridge(stream=stream)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            frames = [part.decode() async for part in response.streaming_content]
            assert frames[-1].startswith('event: error')
            assert not any(frame.startswith('event: result') for frame in frames)
        finally:
            await backend.aclose()
    asyncio.run(run())


def test_workflow_summaries_arrive_before_completion_without_reexecuting():
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(value, **options):
        await present_summary(result('First node summary.'))
        entered.set()
        await release.wait()
        return result(None)
    async def run():
        backend, _ = setup_bridge(complete=complete)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            stream = response.streaming_content.__aiter__()
            assert b'event: session' in await anext(stream)
            assert b'event: thinking' in await anext(stream)
            await entered.wait()
            assert b'event: reasoning_token' in await anext(stream)
            release.set()
            remaining = [part.decode() async for part in stream]
            assert remaining[-1].startswith('event: done')
            assert 'First node summary.' in remaining[-2]
        finally:
            release.set()
            await backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('delivery', ['direct', 'workflow', 'event_count'])
def test_summary_budget_overflow_fails_stream_without_final_answer(delivery):
    async def stream(value, **options):
        if delivery == 'event_count':
            for _ in range(4097):
                yield StreamEvent(TextContent(''), kind='reasoning_summary')
        else:
            yield StreamEvent(TextContent('x' * 65537), kind='reasoning_summary')
    async def complete(value, **options):
        await present_summary(result('é' * 32768))
        await present_summary(result('one more'))
        return result(None)
    async def run():
        backend, _ = setup_bridge(**({'stream': stream} if delivery != 'workflow' else {'complete': complete}))
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            frames = [part.decode() async for part in response.streaming_content]
            assert frames[-1].startswith('event: error')
            assert not any(frame.startswith('event: result') for frame in frames)
        finally:
            await backend.aclose()
    asyncio.run(run())
