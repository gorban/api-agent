"""Graph callers consume admitted progress without accepting draft answers."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import StreamEvent, TextContent, ToolCall
from api_agent.providers.presentation import (complete_with_progress, present_summary,
    summary_delivery, reasoning_progress)
from test_reasoning_presentation import result
from test_bedrock_mantle import request


class Provider:
    capabilities = SimpleNamespace(operations=('complete', 'stream'))

    def __init__(self, events):
        self.events = events
        self.closed = False
        self.buffered = False

    async def acomplete(self, value, *, auth):
        self.buffered = True
        return result()

    async def astream(self, value, *, auth):
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


def test_progress_consumer_can_translate_and_completed_call_is_not_duplicated():
    async def run():
        observed = []
        async def translate(text):
            observed.append('Friendly: ' + text)
        provider = Provider([StreamEvent(TextContent('Checked '), kind='reasoning_summary'),
            StreamEvent(TextContent('format.'), kind='reasoning_summary'), StreamEvent(TextContent('Ok')),
            StreamEvent(result('Checked format.'), kind='completed')])
        with summary_delivery(translate):
            value = replace(request(), request_id='stable-call')
            final = await complete_with_progress(provider, value, auth=object())
            await present_summary(replace(final), request_id=value.request_id)
            await present_summary(replace(final), request_id=value.request_id)
        assert observed == ['Friendly: Checked ', 'Friendly: format.']
        assert final.payload.content[0].text == 'Ok'
        assert provider.closed and not provider.buffered
    asyncio.run(run())


@pytest.mark.parametrize('case', ['no_sink', 'no_stream', 'rich'])
def test_nonstreaming_admission_retains_original_completion(case):
    from api_agent.providers.llm import StructuredOutput
    async def run():
        provider = Provider([])
        value = request()
        if case == 'no_stream':
            provider.capabilities = SimpleNamespace(operations=('complete',))
        if case == 'rich':
            value = replace(value, payload=replace(value.payload,
                structured=StructuredOutput('shape', '{"type":"object"}')))
        async def sink(text):
            raise AssertionError('No progress expected')
        with summary_delivery(None if case == 'no_sink' else sink):
            assert await complete_with_progress(provider, value) == result()
        assert provider.buffered and not provider.closed
    asyncio.run(run())


@pytest.mark.parametrize('events', [
    [], [StreamEvent(TextContent('wrong')), StreamEvent(result(None), kind='completed')],
    [StreamEvent(TextContent('Ok')), StreamEvent(result(), kind='completed')],
    [StreamEvent(result(None), kind='completed'), StreamEvent(TextContent('late'))],
    [StreamEvent(ToolCall('c', 'lookup', '{}'), kind='tool_call')],
    [StreamEvent(TextContent('x' * 65537), kind='reasoning_summary')],
    [StreamEvent(TextContent('x' * 524289))],
])
def test_invalid_progress_stream_is_closed_without_returning_a_graph_result(events):
    async def run():
        async def sink(text):
            pass
        provider = Provider(events)
        with summary_delivery(sink), pytest.raises(ContractError):
            await complete_with_progress(provider, request())
        assert provider.closed
    asyncio.run(run())


def test_cancellation_closes_provider_stream():
    async def run():
        async def cancel(text):
            raise asyncio.CancelledError()
        provider = Provider([StreamEvent(TextContent('progress'), kind='reasoning_summary')])
        with summary_delivery(cancel), pytest.raises(asyncio.CancelledError):
            await complete_with_progress(provider, request())
        assert provider.closed
    asyncio.run(run())


def test_translator_receives_call_identity_deltas_and_a_completed_section_without_studio():
    async def run():
        updates = []
        async def sink(update):
            updates.append(update)
        value = replace(request(), request_id='call-identity')
        provider = Provider([StreamEvent(TextContent('Checked.'), kind='reasoning_summary'),
            StreamEvent(TextContent('Ok')), StreamEvent(result('Checked.'), kind='completed')])
        with reasoning_progress(sink):
            final = await complete_with_progress(provider, value)
            await present_summary(replace(final), request_id=value.request_id)
        await present_summary(final)
        assert [(item.request_id, item.text, item.complete) for item in updates] == [
            ('call-identity', 'Checked.', False), ('call-identity', 'Checked.', True)]
        assert 'Checked.' not in repr(updates[0])
    asyncio.run(run())


def test_buffered_call_can_feed_a_completed_section_to_a_translator():
    async def run():
        updates = []
        async def sink(update):
            updates.append(update)
        with reasoning_progress(sink):
            completed = result()
            await present_summary(completed, request_id='buffered-call')
            await present_summary(replace(completed), request_id='buffered-call')
        assert len(updates) == 1
        assert updates[0].complete and updates[0].request_id == 'buffered-call'
    asyncio.run(run())


def test_distinct_results_with_empty_request_ids_are_independent_sections():
    async def run():
        updates = []
        async def sink(update):
            updates.append(update)
        first, second = result(), result()
        with reasoning_progress(sink):
            await present_summary(first)
            await present_summary(second)
            await present_summary(first)
        assert len(updates) == 2
        assert all(update.request_id for update in updates)
        assert updates[0].request_id != updates[1].request_id
    asyncio.run(run())


def test_plain_async_iterator_without_optional_close_is_supported():
    async def run():
        class Iterator:
            def __init__(self):
                self.events = iter([StreamEvent(TextContent('Ok')), StreamEvent(result(None), kind='completed')])
            def __aiter__(self):
                return self
            async def __anext__(self):
                event = next(self.events, None)
                if event is None:
                    raise StopAsyncIteration
                return event
        provider = Provider([])
        provider.astream = lambda *args, **kwargs: Iterator()
        async def sink(text):
            raise AssertionError('No reasoning expected')
        with summary_delivery(sink):
            assert await complete_with_progress(provider, request()) == result(None)
    asyncio.run(run())


def test_recycled_object_identity_does_not_suppress_a_distinct_anonymous_call(monkeypatch):
    import gc
    import weakref
    from api_agent.providers import presentation
    monkeypatch.setattr(presentation, 'id', lambda value: 42, raising=False)

    async def run():
        updates = []
        async def sink(update):
            updates.append(update)
        with reasoning_progress(sink):
            first = result()
            reference = weakref.ref(first)
            await present_summary(first)
            del first
            gc.collect()
            assert reference() is None
            await present_summary(result())
        assert len(updates) == 2
        assert all(update.request_id for update in updates)
        assert updates[0].request_id != updates[1].request_id
    asyncio.run(run())


@pytest.mark.parametrize('failure', [False, True])
def test_concurrent_completion_consumers_share_one_delivery_and_its_failure(failure):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []
        error = RuntimeError('Synthetic translator failure')
        async def sink(update):
            observed.append(update)
            entered.set()
            await release.wait()
            if failure:
                raise error
        with reasoning_progress(sink):
            completed = result()
            first = asyncio.create_task(present_summary(completed, request_id='same-call'))
            await entered.wait()
            second = asyncio.create_task(present_summary(replace(completed), request_id='same-call'))
            await asyncio.sleep(0)
            assert not second.done() and len(observed) == 1
            release.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert outcomes == ([error, error] if failure else [None, None])
        assert len(observed) == 1
    asyncio.run(run())


def test_provider_without_reasoning_still_returns_a_streamed_answer():
    async def run():
        observed = []
        async def sink(text):
            observed.append(text)
        provider = Provider([StreamEvent(TextContent('Ok')), StreamEvent(result(None), kind='completed')])
        with summary_delivery(sink):
            final = await complete_with_progress(provider, request())
            await present_summary(final)
        assert final.payload.content[0].text == 'Ok' and observed == []
    asyncio.run(run())
