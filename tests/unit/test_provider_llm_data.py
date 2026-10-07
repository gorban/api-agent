import asyncio
from dataclasses import FrozenInstanceError
import pytest
from api_agent.providers.llm import AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, StreamEvent, StructuredOutput, TextContent, Tool, ToolCall, ToolResult, Usage
from api_agent.providers.control import OperationalError


def test_normalized_immutable_data():
    text = TextContent('private text')
    call = ToolCall('a', 'lookup', '{}')
    result = ToolResult('a', (text,))
    assistant = Message('assistant', (text,), (call,))
    assert Message('tool', results=(result,)).results == (result,)
    request = LLMRequest('neutral', CompletionRequest((Message('user', (text,)), assistant), (Tool('lookup', 'help', '{}'),), GenerationOptions(3, .5), StructuredOutput('answer', '{}')))
    AvailableSelection('neutral', features=('tools', 'structured', 'temperature', 'max_output_tokens')).validate(request)
    assert StreamEvent(text).final is False
    assert StreamEvent(call, 'tool_call').payload == call
    terminal = StreamEvent(LLMResult(assistant, Usage(1, 2), 'tool_calls'), 'completed')
    assert terminal.final and terminal.payload.usage.input_tokens == 1
    assert 'private text' not in repr(request)
    with pytest.raises(FrozenInstanceError):
        text.text = 'change'


@pytest.mark.parametrize('factory', [
    lambda: Tool("x", "", "[]"), lambda: StructuredOutput("x", "invalid"), lambda: TextContent(3), lambda: ToolCall('', 'x', '{}'), lambda: ToolResult('x', []),
    lambda: Message('bad'), lambda: Message('user', calls=(ToolCall('a', 'x', '{}'),)),
    lambda: Message('assistant', results=(ToolResult('a', ()),)), lambda: Tool('', '', '{}'),
    lambda: GenerationOptions(0), lambda: GenerationOptions(True), lambda: GenerationOptions(temperature=-1),
    lambda: GenerationOptions(temperature=float('nan')), lambda: StructuredOutput('', '{}'),
    lambda: CompletionRequest(()), lambda: CompletionRequest((Message('user'),), tools=(Tool('a', '', '{}'), Tool('a', '', '{}'))),
    lambda: CompletionRequest((Message('user'),), structured=object()), lambda: AvailableSelection('', ''),
    lambda: AvailableSelection('x', features=('bad',)), lambda: LLMRequest('x', object()),
    lambda: Usage(-1), lambda: Usage(True), lambda: LLMResult(Message('user')),
    lambda: LLMResult(Message('assistant'), outcome='bad'), lambda: StreamEvent(TextContent('x'), 'bad'),
    lambda: StreamEvent(TextContent('x'), 'completed'),
])
def test_invalid_data(factory):
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize('payload', [
    CompletionRequest((Message('user'),), tools=(Tool('t', '', '{}'),)),
    CompletionRequest((Message('assistant', calls=(ToolCall('a', 't', '{}'),)),)),
    CompletionRequest((Message('tool', results=(ToolResult('a', ()),)),)),
    CompletionRequest((Message('user'),), structured=StructuredOutput('x', '{}')),
    CompletionRequest((Message('user'),), generation=GenerationOptions(1)),
    CompletionRequest((Message('user'),), generation=GenerationOptions(temperature=0)),
])
def test_optional_features_rejected(payload):
    with pytest.raises(OperationalError) as caught:
        AvailableSelection('x').validate(LLMRequest('x', payload))
    assert caught.value.kind == 'unsupported' and caught.value.execution == 'not_started'


def test_one_renderer_two_distinct_transport_adapters():
    class First:
        def __init__(self):
            self.sent = None
        async def acomplete(self, request):
            AvailableSelection('x', features=('tools', 'structured')).validate(request)
            self.sent = {'history': [(message.role, ''.join(part.text for part in message.content)) for message in request.payload.messages],
                         'functions': [(tool.name, tool.input_schema) for tool in request.payload.tools],
                         'format': request.payload.structured.schema}
            raw = {'reply': {'text': '{"answer":3}', 'functions': [{'id': 'a', 'function': 'lookup', 'args': '{"key":3}'}]},
                   'counts': {'in': 10, 'out': 6, 'cached': 4, 'thinking': 2}}
            reply, counts = raw['reply'], raw['counts']
            return LLMResult(Message('assistant', (TextContent(reply['text']),), tuple(ToolCall(item['id'], item['function'], item['args']) for item in reply['functions'])),
                             Usage(counts['in'], counts['out'], counts['cached'], counts['thinking']), 'tool_calls')
    class Second:
        def __init__(self):
            self.sent = None
        async def acomplete(self, request):
            AvailableSelection('x', features=('tools', 'structured')).validate(request)
            self.sent = {'turns': [{'speaker': message.role, 'segments': [part.text for part in message.content]} for message in request.payload.messages],
                         'catalog': {tool.name: tool.input_schema for tool in request.payload.tools},
                         'json_shape': (request.payload.structured.name, request.payload.structured.schema)}
            raw = [('text', '{"answer":'), ('text', '3}'), ('invoke', ('a', 'lookup', '{"key":3}'))]
            # This transport reports uncached input and visible output separately.
            counts = {'fresh': 6, 'reused': 4, 'visible': 4, 'reasoning': 2}
            return LLMResult(Message('assistant', tuple(TextContent(data) for kind, data in raw if kind == 'text'),
                                     tuple(ToolCall(*data) for kind, data in raw if kind == 'invoke')),
                             Usage(counts['fresh'] + counts['reused'], counts['visible'] + counts['reasoning'], counts['reused'], counts['reasoning']), 'tool_calls')
    async def render(adapter, request):
        result = await adapter.acomplete(request)
        return (''.join(part.text for part in result.payload.content),
                tuple((call.call_id, call.name, call.arguments) for call in result.payload.calls), result.outcome, result.usage)
    async def scenario():
        request = LLMRequest('x', CompletionRequest((Message('user', (TextContent('lookup three'),)),),
                            tools=(Tool('lookup', 'lookup a key', '{"type":"object"}'),),
                            structured=StructuredOutput('answer', '{"type":"object"}')))
        first, second = First(), Second()
        assert await render(first, request) == await render(second, request) == ('{"answer":3}', (('a', 'lookup', '{"key":3}'),), 'tool_calls', Usage(10, 6, 4, 2))
        assert first.sent == {'history': [('user', 'lookup three')], 'functions': [('lookup', '{"type":"object"}')], 'format': '{"type":"object"}'}
        assert second.sent == {'turns': [{'speaker': 'user', 'segments': ['lookup three']}], 'catalog': {'lookup': '{"type":"object"}'}, 'json_shape': ('answer', '{"type":"object"}')}
        with pytest.raises(OperationalError):
            AvailableSelection('other').validate(request)
    asyncio.run(scenario())


def test_canonical_enums_and_usage_subsets():
    class Pretend:
        def __hash__(self):
            return hash('assistant')
        def __eq__(self, other):
            return True
    class StringSubclass(str):
        pass
    for value in ([], Pretend(), StringSubclass('assistant')):
        with pytest.raises(ValueError):
            Message(value)
        with pytest.raises(ValueError):
            LLMResult(Message('assistant'), outcome=value)
        with pytest.raises(ValueError):
            StreamEvent(TextContent('x'), kind=value)
        with pytest.raises(ValueError):
            AvailableSelection('x', features=(value,))
    with pytest.raises(ValueError):
        AvailableSelection('x', features=('tools', 'tools'))
    for usage in (lambda: Usage(1, 3, 2), lambda: Usage(3, 1, reasoning_tokens=2)):
        with pytest.raises(ValueError):
            usage()
    assert Usage(3, 4, 3, 4).cached_input_tokens == 3
    assert Usage(cached_input_tokens=4, reasoning_tokens=5).reasoning_tokens == 5


@pytest.mark.parametrize('schema', ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"nested":[{"x":1e999}]}'])
def test_schemas_reject_nonfinite_recursive_values(schema):
    with pytest.raises(ValueError):
        Tool('x', '', schema)
    with pytest.raises(ValueError):
        StructuredOutput('x', schema)


def test_schema_nested_finite_values():
    assert Tool('x', '', '{"nested":[{"number":1.5},null,true,"text"]}').name == 'x'
