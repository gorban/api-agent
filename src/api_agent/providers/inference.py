"""Strict bounded text-completion values for the integration consumer boundary."""
import json

from .contracts import ContractError
from .llm import CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage


def _fail():
    raise ContractError('Invalid text inference value')


def _text(value, limit, *, identifier=False):
    if type(value) is not str or len(value) > limit:
        _fail()
    if identifier and (not value or not value.isprintable()):
        _fail()
    return value


def _size(value):
    if len(json.dumps(value, separators=(',', ':')).encode()) > 131072:
        raise ContractError('Text inference exceeds its byte limit')


def request_from_wire(row):
    if type(row) is not dict or set(row) != {'selected_llm', 'messages', 'max_output_tokens', 'cache_policy', 'request_id'}:
        _fail()
    _text(row['selected_llm'], 200, identifier=True)
    _text(row['request_id'], 200)
    if row['request_id'] and not row['request_id'].isprintable():
        _fail()
    if row['max_output_tokens'] is not None and (type(row['max_output_tokens']) is not int or not 1 <= row['max_output_tokens'] <= 4096):
        _fail()
    if row['cache_policy'] is not None and (type(row['cache_policy']) is not str or row['cache_policy'] not in ('off', 'medium', 'high')):
        _fail()
    if type(row['messages']) is not list or not 1 <= len(row['messages']) <= 64:
        _fail()
    messages = []
    for item in row['messages']:
        if (type(item) is not dict or set(item) != {'role', 'text'}
                or type(item['role']) is not str or item['role'] not in ('system', 'user', 'assistant')):
            _fail()
        messages.append(Message(item['role'], (TextContent(_text(item['text'], 32768)),)))
    _size(row)
    return LLMRequest(row['selected_llm'], CompletionRequest(tuple(messages),
        generation=GenerationOptions(max_output_tokens=row['max_output_tokens'], cache_policy=row['cache_policy'])), row['request_id'])


def request_to_wire(request):
    if (not isinstance(request, LLMRequest) or request.payload.tools or request.payload.structured is not None
            or request.payload.generation.temperature is not None):
        _fail()
    rows = []
    for message in request.payload.messages:
        if message.calls or message.results or len(message.content) != 1:
            _fail()
        rows.append({'role': message.role, 'text': message.content[0].text})
    row = dict(selected_llm=request.selected_llm, messages=rows,
               max_output_tokens=request.payload.generation.max_output_tokens,
               cache_policy=request.payload.generation.cache_policy, request_id=request.request_id)
    request_from_wire(row)
    return row


def result_from_wire(row):
    if type(row) is not dict or not {'text', 'outcome', 'usage'} <= set(row) or set(row) - {'text', 'outcome', 'usage', 'actual_model', 'service_tier', 'serving_revision', 'billing'}:
        _fail()
    text = _text(row['text'], 65536)
    if type(row['outcome']) is not str or row['outcome'] not in ('completed', 'length', 'refused'):
        _fail()
    usage = row['usage']
    if type(usage) is not dict or set(usage) not in (
            {'input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens'},
            {'input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'cache_write_tokens'}):
        _fail()
    if any(value is not None and (type(value) is not int or not 0 <= value <= 2147483647) for value in usage.values()):
        _fail()
    try:
        result = LLMResult(Message('assistant', (TextContent(text),)), Usage(**usage), row['outcome'], row.get('actual_model'), row.get('service_tier'), row.get('serving_revision'), row.get('billing'))
    except ValueError:
        _fail()
    _size(row)
    return result


def usage_to_wire(usage):
    """Keep older four-field observations readable; write counts are optional."""
    row = {name: getattr(usage, name) for name in
           ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens')}
    if usage.cache_write_tokens is not None:
        row['cache_write_tokens'] = usage.cache_write_tokens
    return row


def result_to_wire(result):
    if (not isinstance(result, LLMResult) or result.payload.calls or result.payload.results
            or len(result.payload.content) != 1):
        _fail()
    row = dict(text=result.payload.content[0].text, outcome=result.outcome,
        usage=usage_to_wire(result.usage))
    for name in ('actual_model', 'service_tier', 'serving_revision', 'billing'):
        if getattr(result, name) is not None:
            row[name] = getattr(result, name)
    result_from_wire(row)
    return row
