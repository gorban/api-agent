"""Pure bounded codecs for Mantle's OpenAI-compatible JSON contracts.

The maintained SDK owns HTTP and SSE parsing in the isolated worker. Tools are
buffered until EOF; the factory validates their schemas before publication.
"""
from copy import deepcopy
import json

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import LLMRequest, LLMResult, Message, StreamEvent, TextContent, ToolCall, Usage
from api_agent.providers.rich_inference import object_json, request_to_wire, schema
from .catalog import selection

_LIMIT = 524288
_REASONS = {'stop': 'completed', 'tool_calls': 'tool_calls', 'length': 'length', 'content_filter': 'refused'}


def _require(condition):
    if not condition:
        raise ProviderAccessError('unknown', 'Invalid Bedrock inference response.')


def _text(value, *, empty=True):
    _require(type(value) is str and (empty or bool(value)))
    try:
        _require(len(value.encode('utf-8')) <= _LIMIT)
    except UnicodeError:
        raise ProviderAccessError('unknown', 'Invalid Bedrock response encoding.') from None
    return value


def _object(value):
    try:
        return object_json(_text(value))
    except ContractError:
        raise ProviderAccessError('unknown', 'Invalid Bedrock tool arguments.') from None


def _bounded(value):
    try:
        _require(len(json.dumps(value, allow_nan=False, ensure_ascii=True).encode('utf-8')) <= _LIMIT)
    except (ValueError, TypeError, RecursionError):
        raise ProviderAccessError('unknown', 'Invalid Bedrock inference response.') from None


def _fields(row, allowed):
    """Unknown null SDK fields are absence; nonnull unsupported semantics fail."""
    _require(type(row) is dict and all(key in allowed or value is None for key, value in row.items()))


def _reasoning(item, *, incomplete=False):
    _fields(item, {'type', 'id', 'status', 'summary', 'content'})
    status = item.get('status')
    status = 'completed' if status is None else status
    _require(item.get('type') == 'reasoning'
             and status in (('completed', 'incomplete') if incomplete else ('completed',)))
    _text(item.get('id'), empty=False)
    for key, kind in (('summary', 'summary_text'), ('content', 'reasoning_text')):
        rows = item.get(key)
        rows = [] if key == 'content' and rows is None else rows
        _require(type(rows) is list and len(rows) <= 64)
        for row in rows:
            _require(type(row) is dict and set(row) == {'type', 'text'} and row.get('type') == kind)
            _text(row.get('text'))


def validate_request(request):
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock request.')
    payload = request.payload
    if (payload.structured is not None and
            (request.selected_llm != 'zai.glm-5' or not payload.structured.strict)
            or payload.generation.cache_policy is not None
            or payload.generation.reasoning_effort not in (None, '') and
               (request.selected_llm not in ('openai.gpt-oss-20b', 'openai.gpt-oss-120b')
                or payload.generation.reasoning_effort not in ('low', 'medium', 'high'))
            or any(m.cache_boundary is not None or m.continuation is not None for m in payload.messages)):
        raise ProviderAccessError('unsupported', 'Bedrock request feature is unsupported.')
    maximum_temperature = 1 if request.selected_llm.startswith('zai.') else 2
    if payload.generation.temperature is not None and payload.generation.temperature > maximum_temperature:
        raise ProviderAccessError('invalid_request', 'Bedrock model temperature is out of range.')
    try:
        request_to_wire(request)
    except ContractError:
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock request.') from None


def encode(request, api_mode):
    validate_request(request)
    selected = selection(request.selected_llm, api_mode)
    selected.validate(request)
    payload = request.payload
    response = api_mode == 'responses'
    items = []
    for message in payload.messages:
        text = ''.join(part.text for part in message.content)
        if response:
            if message.content:
                items.append({'role': message.role, 'content': text})
            items.extend({'type': 'function_call', 'call_id': call.call_id, 'name': call.name,
                          'arguments': call.arguments} for call in message.calls)
            items.extend({'type': 'function_call_output', 'call_id': result.call_id,
                          'output': ''.join(part.text for part in result.content)} for result in message.results)
        else:
            if message.role != 'tool':
                row = {'role': message.role, 'content': text}
                if message.calls:
                    row['tool_calls'] = [{'id': call.call_id, 'type': 'function', 'function':
                        {'name': call.name, 'arguments': call.arguments}} for call in message.calls]
                items.append(row)
            items.extend({'role': 'tool', 'tool_call_id': result.call_id,
                          'content': ''.join(part.text for part in result.content)} for result in message.results)
    body = {'model': request.selected_llm, 'input' if response else 'messages': items}
    if response:
        body['store'] = False
    if payload.tools:
        tools = []
        for tool in payload.tools:
            spec = {'name': tool.name, 'description': tool.description,
                    'parameters': schema(tool.input_schema, tool.strict), 'strict': tool.strict}
            tools.append({'type': 'function', **spec} if response else {'type': 'function', 'function': spec})
        body['tools'] = tools
    for name, value in (('temperature', payload.generation.temperature),
                        ('max_output_tokens' if response else 'max_tokens', payload.generation.max_output_tokens if payload.generation.max_output_tokens is not None else 1024)):
        if value is not None:
            body[name] = value
    if payload.structured is not None:
        definition = payload.structured
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': definition.name, 'schema': schema(definition.schema, True), 'strict': True}}
    effort = payload.generation.reasoning_effort
    if effort:
        body['reasoning' if response else 'reasoning_effort'] = {'effort': effort} if response else effort
    return body


def _usage(raw, response):
    _require(type(raw) is dict)
    incoming, outgoing, total = (raw.get(key) for key in
        (('input_tokens', 'output_tokens', 'total_tokens') if response else ('prompt_tokens', 'completion_tokens', 'total_tokens')))
    _require(all(type(value) is int and 0 <= value <= 2147483647 for value in (incoming, outgoing, total)))
    _require(total == incoming + outgoing)
    counts = []
    for key, subset, maximum in (
        ('input_tokens_details' if response else 'prompt_tokens_details', 'cached_tokens', incoming),
        ('output_tokens_details' if response else 'completion_tokens_details', 'reasoning_tokens', outgoing)):
        details = raw.get(key)
        _require(details is None or type(details) is dict)
        value = None if details is None else details.get(subset)
        _require(value is None or type(value) is int and 0 <= value <= maximum)
        counts.append(value)
    return Usage(incoming, outgoing, *counts)


def _call_metadata(row):
    _fields(row, {'type', 'id', 'status', 'call_id', 'name', 'arguments', 'item_id', 'output_index'})
    for key in ('id', 'item_id'):
        if row.get(key) is not None:
            _text(row[key], empty=False)
    position = row.get('output_index')
    if position is not None:
        _require(type(position) is int and 0 <= position < 64)


def _calls(rows, response):
    rows = [] if rows is None and not response else rows
    _require(type(rows) is list and len(rows) <= 64)
    calls, seen = [], set()
    for row in rows:
        if response:
            _call_metadata(row)
        else:
            _fields(row, {'type', 'id', 'function'})
        _require(row.get('type') == ('function_call' if response else 'function'))
        call_id = _text(row.get('call_id' if response else 'id'), empty=False)
        _require(call_id not in seen)
        seen.add(call_id)
        spec = row if response else row.get('function')
        if not response:
            _fields(spec, {'name', 'arguments'})
        name, arguments = _text(spec.get('name'), empty=False), _text(spec.get('arguments'))
        _object(arguments)
        calls.append(ToolCall(call_id, name, arguments))
    return tuple(calls)


def _metadata(raw):
    values = {key: raw.get(key) for key in ('model', 'service_tier', 'serving_revision')}
    _require(all(value is None or type(value) is str and 0 < len(value) <= 200 and value.isprintable()
                 for value in values.values()))
    return values


def _reported_error(error, raw, response):
    error.execution = 'possibly_executed'
    if type(raw) is dict and raw.get('usage') is not None:
        try:
            error.usage = _usage(raw['usage'], response)
        except ProviderAccessError:
            pass
    return error


def decode(raw, api_mode, *, selected_llm):
    try:
        return _decode(raw, api_mode, selected_llm=selected_llm)
    except ProviderAccessError as error:
        raise _reported_error(error, raw, api_mode == 'responses')


def _decode(raw, api_mode, *, selected_llm):
    selection(selected_llm, api_mode)
    _bounded(raw)
    _require(type(raw) is dict and raw.get('error') is None and raw.get('model', selected_llm) == selected_llm)
    response = api_mode == 'responses'
    if response:
        _require(raw.get('status') in ('completed', 'incomplete') and type(raw.get('output')) is list and len(raw['output']) <= 64)
        text, tools = [], []
        for item in raw['output']:
            _require(type(item) is dict)
            if item.get('type') == 'reasoning':
                _reasoning(item, incomplete=raw['status'] == 'incomplete')
            elif item.get('type') == 'function_call':
                _call_metadata(item)
                _require(item.get('status', 'completed') == 'completed')
                tools.append(item)
            else:
                _require(item.get('type') == 'message' and item.get('role') == 'assistant'
                         and item.get('status', 'completed') in (('completed', 'incomplete') if raw['status'] == 'incomplete' else ('completed',))
                         and type(item.get('content')) is list and len(item['content']) <= 64)
                for part in item['content']:
                    _require(type(part) is dict and part.get('type') == 'output_text')
                    text.append(_text(part.get('text')))
        calls = _calls(tools, True)
        reason = 'tool_calls' if calls else 'completed'
        if raw['status'] == 'incomplete':
            _require(raw.get('incomplete_details') == {'reason': 'max_output_tokens'} and not calls)
            reason = 'length'
        else:
            _require(raw.get('incomplete_details') is None)
    else:
        choices = raw.get('choices')
        _require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict and choices[0].get('index') == 0)
        choice = choices[0]
        message = choice.get('message')
        _fields(message, {'role', 'content', 'tool_calls', 'reasoning_content', 'reasoning', 'annotations'})
        annotations = message.get('annotations')
        _require(annotations is None or type(annotations) is list and not annotations)
        _require(message.get('role') == 'assistant')
        for field in ('reasoning_content', 'reasoning'):
            if message.get(field) is not None:
                _text(message[field])
        content = message.get('content')
        text = [] if content is None else [_text(content)]
        calls = _calls(message.get('tool_calls', []), False)
        finish = choice.get('finish_reason')
        _require(type(finish) is str and finish in _REASONS and (finish == 'tool_calls') == bool(calls))
        reason = _REASONS[finish]
    content = _text(''.join(text))
    metadata = _metadata(raw)
    return LLMResult(Message('assistant', (TextContent(content),), calls), _usage(raw.get('usage'), response),
                     reason, metadata['model'], metadata['service_tier'], metadata['serving_revision'])


class StreamDecoder:
    """Ordered SDK dictionaries; terminal snapshots are withheld until EOF."""
    def __init__(self, api_mode, *, selected_llm):
        selection(selected_llm, api_mode)
        self.mode, self.model = api_mode, selected_llm
        self.finished = False
        self.terminal = None
        self.identity = None
        self.text = ''
        self.tools = {}
        self.reason = None
        self.started = False
        self.items = []
        self.closed = set()
        self.parts = {}
        self.sequence = -1
        self.arguments_done = set()
        self.size = 0
        self.last_text_index = -1
        self.metadata = {}

    def feed(self, event):
        try:
            return self._feed(event)
        except ProviderAccessError as error:
            raw = event.get('response', event) if type(event) is dict else None
            raise _reported_error(error, raw, self.mode == 'responses')

    def _feed(self, event):
        _require(not self.finished and self.terminal is None and type(event) is dict and event.get('error') is None)
        _bounded(event)
        self.size += len(json.dumps(event, ensure_ascii=True))
        _require(self.size <= _LIMIT)
        if self.mode == 'responses':
            return self._responses(event)
        return self._completions(event)

    def _emit(self, fragment):
        self.text = _text(self.text + _text(fragment))
        return (StreamEvent(TextContent(fragment)),)

    def _remember_metadata(self, raw):
        for key, value in _metadata(raw).items():
            if value is not None:
                _require(key not in self.metadata or self.metadata[key] == value)
                self.metadata[key] = value

    def _completions(self, event):
        _require(event.get('object') == 'chat.completion.chunk' and event.get('model') == self.model)
        self._remember_metadata(event)
        identity = _text(event.get('id'), empty=False)
        _require(self.identity is None or self.identity == identity)
        self.identity = identity
        choices = event.get('choices')
        _require(type(choices) is list)
        if not choices:
            _require(self.reason is not None)
            self.terminal = {**self.metadata, 'usage': event.get('usage'), 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': self.text, 'tool_calls': list(self.tools.values())},
                'finish_reason': self.reason}]}
            decode(self.terminal, self.mode, selected_llm=self.model)
            return ()
        _require(self.reason is None and len(choices) == 1 and type(choices[0]) is dict and choices[0].get('index') == 0
                 and event.get('usage') is None)
        choice = choices[0]
        delta = choice.get('delta')
        _fields(delta, {'role', 'content', 'tool_calls', 'reasoning_content'})
        if delta.get('role') is not None:
            _require(not self.started and delta['role'] == 'assistant')
            self.started = True
        _require(self.started)
        events = ()
        if delta.get('reasoning_content') is not None:
            _text(delta['reasoning_content'])
        if delta.get('content') is not None:
            events = self._emit(delta['content'])
        rows = delta.get('tool_calls')
        rows = [] if rows is None else rows
        _require(type(rows) is list)
        for row in rows:
            _fields(row, {'index', 'id', 'type', 'function'})
            index = row.get('index')
            _require(type(index) is int and 0 <= index < 64)
            spec = row.get('function')
            spec = {} if spec is None and index in self.tools else spec
            _fields(spec, {'name', 'arguments'})
            if index not in self.tools:
                _require(index == len(self.tools) and row.get('type') == 'function')
                self.tools[index] = {'id': _text(row.get('id'), empty=False), 'type': 'function',
                                     'function': {'name': _text(spec.get('name'), empty=False), 'arguments': ''}}
            else:
                _require(row.get('id') is None and row.get('type') is None and spec.get('name') is None)
            tool = self.tools[index]['function']
            arguments = spec.get('arguments')
            tool['arguments'] = _text(tool['arguments'] + ('' if arguments is None else _text(arguments)))
        finish = choice.get('finish_reason')
        if finish is not None:
            _require(type(finish) is str and finish in _REASONS)
            self.reason = finish
        return events

    def _responses(self, event):
        sequence = event.get('sequence_number')
        _require(type(sequence) is int and sequence == self.sequence + 1)
        self.sequence = sequence
        kind = event.get('type')
        if kind == 'response.created':
            raw = event.get('response')
            _require(not self.started and type(raw) is dict and raw.get('status') == 'in_progress'
                     and raw.get('error') is None and raw.get('model', self.model) == self.model)
            self._remember_metadata(raw)
            self.identity = _text(raw.get('id'), empty=False)
            self.started = True
            return ()
        _require(self.started)
        if kind == 'response.in_progress':
            raw = event.get('response')
            _require(not self.items and type(raw) is dict and raw.get('id') == self.identity and raw.get('status') == 'in_progress'
                     and raw.get('error') is None and raw.get('model', self.model) == self.model)
            self._remember_metadata(raw)
            return ()
        if kind in ('response.completed', 'response.incomplete'):
            raw = event.get('response')
            _require(type(raw) is dict and raw.get('id') == self.identity and len(self.closed) == len(self.items)
                     and raw.get('output') == self.items
                     and raw.get('status') == ('completed' if kind == 'response.completed' else 'incomplete'))
            self._remember_metadata(raw)
            raw = {**raw, **self.metadata}
            decoded = decode(raw, self.mode, selected_llm=self.model)
            _require(decoded.payload.content == (TextContent(self.text),))
            self.terminal = deepcopy(raw)
            return ()
        index = event.get('output_index')
        _require(type(index) is int and 0 <= index < 64 and index not in self.closed)
        if kind == 'response.output_item.added':
            item = event.get('item')
            _require(index == len(self.items) and type(item) is dict
                     and item.get('type') in ('message', 'function_call', 'reasoning') and item.get('status') == 'in_progress')
            _text(item.get('id'), empty=False)
            _require(all(existing['id'] != item['id'] for existing in self.items))
            if item['type'] == 'reasoning':
                _require(item.get('summary') == [] and item.get('content') in (None, []))
                _fields(item, {'type', 'id', 'status', 'summary', 'content'})
            elif item['type'] == 'message':
                _require(item.get('role') == 'assistant' and item.get('content') == [])
            else:
                _call_metadata(item)
                _text(item.get('call_id'), empty=False)
                _text(item.get('name'), empty=False)
                _require(item.get('arguments') == '')
            self.items.append(deepcopy(item))
            return ()
        _require(index < len(self.items))
        item = self.items[index]
        if kind == 'response.output_item.done':
            done = event.get('item')
            _require(type(done) is dict and done.get('status') in ('completed', 'incomplete'))
            expected = dict(item, status=done['status'])
            _require(event.get('item') == expected and not any(key[0] == index for key in self.parts)
                     and (item['type'] != 'function_call' or index in self.arguments_done))
            self.items[index] = expected
            self.closed.add(index)
            return ()
        _require(event.get('item_id') == item['id'])
        if item['type'] == 'reasoning':
            return self._reasoning_event(event, item, index, kind)
        if kind in ('response.function_call_arguments.delta', 'response.function_call_arguments.done'):
            _require(item['type'] == 'function_call')
            if kind.endswith('.delta'):
                _require(index not in self.arguments_done)
                item['arguments'] = _text(item['arguments'] + _text(event.get('delta')))
            else:
                _require(index not in self.arguments_done and event.get('arguments') == item['arguments'])
                _object(item['arguments'])
                self.arguments_done.add(index)
            return ()
        part_index = event.get('content_index')
        _require(item['type'] == 'message' and type(part_index) is int and 0 <= part_index < 64)
        key = (index, part_index)
        if kind == 'response.content_part.added':
            part = event.get('part')
            _require(part_index == len(item['content']) and key not in self.parts
                     and type(part) is dict and part.get('type') == 'output_text' and part.get('text') == '')
            item['content'].append(deepcopy(part))
            self.parts[key] = False
            return ()
        _require(key in self.parts)
        part = item['content'][part_index]
        if kind == 'response.output_text.delta':
            _require(not self.parts[key])
            _require(index >= self.last_text_index)
            self.last_text_index = index
            fragment = _text(event.get('delta'))
            part['text'] = _text(part['text'] + fragment)
            return self._emit(fragment)
        if kind == 'response.output_text.done':
            _require(not self.parts[key] and event.get('text') == part['text'])
            self.parts[key] = True
            return ()
        _require(kind == 'response.content_part.done' and self.parts[key] and event.get('part') == part)
        del self.parts[key]
        return ()

    def _reasoning_event(self, event, item, index, kind):
        summary = kind in ('response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
                           'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done')
        allowed = ('response.reasoning_text.delta', 'response.reasoning_text.done',
                   'response.content_part.added', 'response.content_part.done')
        _require(summary or kind in allowed)
        field = 'summary' if summary else 'content'
        part_index = event.get('summary_index' if summary else 'content_index')
        _require(type(part_index) is int and 0 <= part_index < 64)
        key = (index, field, part_index)
        rows = item.get(field)
        rows = [] if rows is None else rows
        item[field] = rows
        part_type = 'summary_text' if summary else 'reasoning_text'
        if kind.endswith('.added'):
            part = event.get('part')
            _require(part_index == len(rows) and key not in self.parts
                     and part == {'type': part_type, 'text': ''})
            rows.append(deepcopy(part))
            self.parts[key] = False
            return ()
        _require(key in self.parts)
        part = rows[part_index]
        if kind.endswith('.delta'):
            _require(not self.parts[key])
            part['text'] = _text(part['text'] + _text(event.get('delta')))
        elif kind in ('response.reasoning_summary_text.done', 'response.reasoning_text.done'):
            _require(not self.parts[key] and event.get('text') == part['text'])
            self.parts[key] = True
        else:
            _require(self.parts[key] and event.get('part') == part)
            del self.parts[key]
        return ()

    def finish(self):
        _require(not self.finished and self.terminal is not None)
        result = decode(self.terminal, self.mode, selected_llm=self.model)
        self.finished = True
        return result
