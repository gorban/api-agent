"""A request may name the structured field whose text streams as the visible answer."""
import json

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import (AnswerStream, CompletionRequest, GenerationOptions, LLMRequest,
    Message, StructuredOutput, TextContent)
from api_agent.providers.rich_inference import request_from_wire, request_to_wire

SCHEMA = json.dumps({'type': 'object', 'properties': {'response_text': {'type': 'string'}},
                     'required': ['response_text'], 'additionalProperties': False})


def answer_request(stream=AnswerStream('response_text', 'Text')):
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Answer.'),)),),
        generation=GenerationOptions(answer_stream=stream),
        structured=StructuredOutput('answer', SCHEMA)), 'answer')


@pytest.mark.parametrize('field,presentation', [
    ('', 'Text'), ('response text', 'Text'), ('1st', 'Text'), ('x' * 65, 'Text'),
    ('response_text', ''), ('response_text', 'x' * 33), ('response_text', 'Te\nxt'),
    (3, 'Text'), ('response_text', None)])
def test_answer_stream_names_one_field_and_a_short_presentation(field, presentation):
    with pytest.raises(ValueError):
        AnswerStream(field, presentation)


def test_generation_accepts_only_an_answer_stream_or_none():
    with pytest.raises(ValueError):
        GenerationOptions(answer_stream=('response_text', 'Text'))
    assert GenerationOptions().answer_stream is None


def test_answer_stream_survives_the_rich_wire_round_trip():
    row = request_to_wire(answer_request())
    assert row['answer_stream'] == {'field': 'response_text', 'presentation': 'Text'}
    assert request_from_wire(row).payload.generation.answer_stream == AnswerStream('response_text', 'Text')
    assert 'answer_stream' not in request_to_wire(answer_request(None))


@pytest.mark.parametrize('value', [
    {'field': 'response_text'}, {'field': 'response_text', 'presentation': 'Text', 'extra': 1},
    {'field': 'bad field', 'presentation': 'Text'}, 'response_text', None])
def test_malformed_wire_answer_stream_is_rejected(value):
    row = request_to_wire(answer_request())
    row['answer_stream'] = value
    with pytest.raises(ContractError):
        request_from_wire(row)


def chunks(text, size):
    return [text[index:index + size] for index in range(0, len(text), size)]


ANSWER = 'Hello "world"\n\u00e9 \U0001F600 done'
DOCUMENT = json.dumps({'response_structure': {'response_text': 'nested', 'note': '"response_text": "fake"'},
                       'response_text': ANSWER, 'after': 'ignored'})


@pytest.mark.parametrize('size', [1, 2, 3, 7, len(DOCUMENT)])
def test_decoder_emits_the_top_level_field_text_across_any_chunking(size):
    from api_agent.providers.answer_stream import AnswerFieldDecoder

    decoder = AnswerFieldDecoder('response_text')
    emitted = [decoder.feed(part) for part in chunks(DOCUMENT, size)]
    assert ''.join(emitted) == ANSWER
    assert decoder.feed('') == ''


def test_decoder_without_the_field_emits_nothing_and_survives_malformed_escapes():
    from api_agent.providers.answer_stream import AnswerFieldDecoder

    decoder = AnswerFieldDecoder('response_text')
    assert decoder.feed('{"other": "x", "list": [1, {"response_text": "no"}]}') == ''
    broken = AnswerFieldDecoder('response_text')
    assert broken.feed('{"response_text": "a\\q') == 'a'
    assert broken.feed('b"}') == ''
    surrogate = AnswerFieldDecoder('response_text')
    assert surrogate.feed('{"response_text": "x\\ud83d') == 'x'
    assert surrogate.feed('y"}') == '\ufffdy'
    stray = AnswerFieldDecoder('response_text')
    assert stray.feed('}{"a\\"b": "x", "response_text": "y\\u+041z"}') == 'y'
    assert AnswerFieldDecoder('response_text').feed('{"response_text": "\\u0041\\uZZZZ"}') == 'A'


def stream_result(text, calls=()):
    from api_agent.providers.llm import LLMResult, Message as Reply
    return LLMResult(Reply('assistant', (TextContent(text),), calls=calls))


class StreamingProvider:
    def __init__(self, parts, final=None, extra=()):
        from types import SimpleNamespace
        self.capabilities = SimpleNamespace(operations=('stream',))
        self.parts, self.final, self.extra = parts, final, extra
        self.completed = 0

    async def acomplete(self, request, *, auth=None):
        self.completed += 1
        return stream_result(''.join(self.parts))

    async def astream(self, request, *, auth=None):
        from api_agent.providers.llm import StreamEvent
        for event in self.extra:
            yield event
        for part in self.parts:
            yield StreamEvent(TextContent(part))
        yield StreamEvent(self.final or stream_result(''.join(self.parts)), kind='completed')


def run_answer(provider, request, *, owner=('render', 'a1'), bound=True):
    import asyncio
    from api_agent.providers.presentation import answer_progress, complete_with_progress, progress_call_owner
    shown = []

    async def sink(update):
        shown.append(update)

    async def scenario():
        from contextlib import nullcontext
        with (answer_progress(sink) if bound else nullcontext()), \
                (progress_call_owner(*owner) if owner else nullcontext()):
            return await complete_with_progress(provider, request)
    return asyncio.run(scenario()), shown


def test_bound_answer_stream_publishes_decoded_text_and_returns_the_completed_result():
    from api_agent.providers.llm import StreamEvent, ToolCall
    call = ToolCall('c1', 'lookup', '{}')
    final = stream_result(DOCUMENT, calls=(call,))
    provider = StreamingProvider(chunks(DOCUMENT, 5), final=final,
                                 extra=(StreamEvent(call, kind='tool_call'),))
    result, shown = run_answer(provider, answer_request())
    assert result is final and provider.completed == 0
    assert ''.join(update.text for update in shown) == ANSWER
    assert {(update.node, update.attempt, update.presentation) for update in shown} == {('render', 'a1', 'Text')}


@pytest.mark.parametrize('owner,bound,stream', [
    (None, True, AnswerStream('response_text', 'Text')),
    (('render', 'a1'), False, AnswerStream('response_text', 'Text')),
    (('render', 'a1'), True, None)])
def test_without_owner_sink_or_hint_the_call_is_not_streamed(owner, bound, stream):
    provider = StreamingProvider([DOCUMENT])
    result, shown = run_answer(provider, answer_request(stream), owner=owner, bound=bound)
    assert provider.completed == 1 and not shown
    assert result.payload.content[0].text == DOCUMENT


def test_stream_that_differs_from_its_result_is_rejected():
    provider = StreamingProvider(chunks(DOCUMENT, 9), final=stream_result('{"response_text": "other"}'))
    with pytest.raises(ContractError, match='differs'):
        run_answer(provider, answer_request())


def test_streamed_reasoning_summary_still_reaches_the_reasoning_display():
    import asyncio
    from api_agent.providers.llm import LLMResult, Message as Reply, StreamEvent
    from api_agent.providers.presentation import (answer_progress, complete_with_progress,
        progress_call_owner, reasoning_progress)
    final = LLMResult(Reply('assistant', (TextContent(DOCUMENT),)), reasoning_summary='Thinking.')
    provider = StreamingProvider([DOCUMENT], final=final,
                                 extra=(StreamEvent(TextContent('Thinking.'), kind='reasoning_summary'),))
    reasoning, answers = [], []

    async def scenario():
        async def on_reasoning(update):
            reasoning.append(update)

        async def on_answer(update):
            answers.append(update)
        with reasoning_progress(on_reasoning), answer_progress(on_answer), progress_call_owner('render', 'a1'):
            return await complete_with_progress(provider, answer_request())
    assert asyncio.run(scenario()) is final
    assert [update.text for update in reasoning if not update.complete] == ['Thinking.']
    assert ''.join(update.text for update in answers) == ANSWER


def test_progress_services_bind_the_node_attempt_around_each_call():
    import asyncio
    from api_agent.providers.presentation import call_progress, current_progress_call_owner
    from api_agent.workflows.node_summary import ProgressServices
    seen = []

    async def llm(request):
        seen.append(current_progress_call_owner())
        return stream_result('{}')

    async def scenario():
        async def sink(update):
            pass
        with call_progress(sink):
            services = ProgressServices.bind({'llm': llm}, 'render', 'a1')
            await services['llm'](answer_request())
        assert current_progress_call_owner() is None
    asyncio.run(scenario())
    assert seen == [('render', 'a1')]


def test_provider_answer_events_are_published_and_raw_text_is_not_decoded_again():
    from api_agent.providers.llm import StreamEvent

    class DecodingProvider(StreamingProvider):
        async def astream(self, request, *, auth=None):
            yield StreamEvent(TextContent('Hello "wor'), kind='answer')
            yield StreamEvent(TextContent(DOCUMENT))
            yield StreamEvent(stream_result(DOCUMENT), kind='completed')

    result, shown = run_answer(DecodingProvider([]), answer_request())
    assert result.payload.content[0].text == DOCUMENT
    assert [update.text for update in shown] == ['Hello "wor']


def test_released_answer_that_is_not_a_prefix_of_the_reply_is_rejected():
    from api_agent.providers.llm import StreamEvent

    class WrongProvider(StreamingProvider):
        async def astream(self, request, *, auth=None):
            yield StreamEvent(TextContent('Different'), kind='answer')
            yield StreamEvent(TextContent(DOCUMENT))
            yield StreamEvent(stream_result(DOCUMENT), kind='completed')

    with pytest.raises(ContractError, match='differs'):
        run_answer(WrongProvider([]), answer_request())


@pytest.mark.parametrize('events,message', [
    (('completed', ('text', DOCUMENT)), 'after stream completion'),
    ((('reasoning_summary', 'x' * 65537),), 'display reasoning exceeds'),
    ((('text', 'x' * 524289),), 'answer exceeds')])
def test_answer_streams_enforce_order_and_byte_bounds(events, message):
    from api_agent.providers.llm import StreamEvent

    class BoundedProvider(StreamingProvider):
        async def astream(self, request, *, auth=None):
            for event in events:
                if event == 'completed':
                    yield StreamEvent(stream_result(DOCUMENT), kind='completed')
                else:
                    yield StreamEvent(TextContent(event[1]), kind=event[0])

    with pytest.raises(ContractError, match=message):
        run_answer(BoundedProvider([]), answer_request())


def test_answer_stream_reasoning_needs_no_reasoning_display():
    from api_agent.providers.llm import LLMResult, Message as Reply, StreamEvent
    final = LLMResult(Reply('assistant', (TextContent(DOCUMENT),)), reasoning_summary='Thinking.')
    provider = StreamingProvider([DOCUMENT], final=final,
                                 extra=(StreamEvent(TextContent('Thinking.'), kind='reasoning_summary'),))
    result, shown = run_answer(provider, answer_request())
    assert result is final and ''.join(update.text for update in shown) == ANSWER


def test_answer_stream_accepts_a_provider_iterator_without_aclose():
    from api_agent.providers.llm import StreamEvent

    class Events:
        def __init__(self):
            self.items = iter((StreamEvent(TextContent(DOCUMENT)), StreamEvent(stream_result(DOCUMENT), kind='completed')))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration from None

    class IteratorProvider(StreamingProvider):
        def astream(self, request, *, auth=None):
            return Events()

    result, shown = run_answer(IteratorProvider([]), answer_request())
    assert result.payload.content[0].text == DOCUMENT and ''.join(update.text for update in shown) == ANSWER


def test_answer_progress_without_a_sink_is_ignored():
    import asyncio
    from api_agent.providers.presentation import AnswerProgress, publish_answer_progress
    assert asyncio.run(publish_answer_progress(AnswerProgress('render', 'a1', 'x', 'Text'))) is None


def test_answer_stream_events_carry_text():
    from api_agent.providers.llm import StreamEvent
    assert StreamEvent(TextContent('x'), kind='answer').kind == 'answer'
    with pytest.raises(ValueError):
        StreamEvent(stream_result('x'), kind='answer')
