"""Version-two bounded structured/tool inference; version-one text stays valid."""
import json
import re
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable
from .contracts import ContractError
from .llm import (CompletionRequest, GenerationOptions, LLMRequest, LLMResult,
                  Message, TextContent, Tool, ToolCall, ToolResult, StructuredOutput, Usage)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite JSON')


def object_json(value):
    """Parse a bounded finite JSON object without ambiguous duplicate keys."""
    try:
        _text(value)
        decoded = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
        if type(decoded) is not dict:
            raise ValueError()
        _bounded(decoded)
        return decoded
    except (ValueError, TypeError, RecursionError):
        raise ContractError('Invalid rich inference JSON object') from None


def _references(value):
    """Inspect only executable positions admitted by the strict schema subset."""
    if type(value) is not dict:
        return
    if any(key in value for key in ('$ref', '$dynamicRef', '$recursiveRef')):
        raise ValueError()
    properties = value.get('properties')
    if type(properties) is dict:
        for child in properties.values():
            _references(child)
    if 'items' in value:
        _references(value['items'])


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


def valid_schema(definition, value):
    """Never permit validation to retrieve remote schema resources."""
    try:
        return Draft202012Validator(definition, registry=Registry(retrieve=_no_remote)).is_valid(value)
    except Unresolvable:
        return False


def _strict(node, depth=0):
    if type(node) is not dict or depth > 16 or '$ref' in node:
        raise ValueError()
    kind = node.get('type')
    kinds = kind if type(kind) is list else [kind]
    if not kinds or any(item not in ('object', 'array', 'string', 'number', 'integer', 'boolean', 'null') for item in kinds):
        raise ValueError()
    if 'object' in kinds:
        properties = node.get('properties')
        required = node.get('required')
        if (type(properties) is not dict or node.get('additionalProperties') is not False
                or type(required) is not list or len(required) != len(set(required))
                or set(required) != set(properties)):
            raise ValueError()
        for child in properties.values():
            _strict(child, depth + 1)
    if 'array' in kinds:
        _strict(node.get('items'), depth + 1)
    # This subset deliberately excludes schema composition and references:
    # every nested object must obey the native strict-output contract.
    if set(node) - {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'title', 'description'}:
        raise ValueError()


def schema(value):
    try:
        decoded = object_json(value)
        _references(decoded)
        Draft202012Validator.check_schema(decoded)
        _strict(decoded)
        if decoded.get('type') != 'object':
            raise ValueError()
        return decoded
    except (ValueError, TypeError, SchemaError, RecursionError):
        raise ContractError('Rich inference requires a strict object schema') from None


def _identifier(value, *, name=False, empty=False):
    _text(value, 64 if name else 200)
    if ((not value and not empty) or (value and (not value.isprintable() or value.strip() != value))
            or (name and re.fullmatch(r'[A-Za-z0-9_-]+', value) is None)):
        raise ContractError('Invalid rich inference identifier')
    return value


def _exact(row, keys):
    if type(row) is not dict or set(row) != set(keys):
        raise ContractError('Invalid rich inference fields')


def _text(value, maximum=32768):
    if type(value) is not str or len(value) > maximum:
        raise ContractError('Invalid rich inference text')
    return value


def continuation(value):
    decoded = object_json(value)
    _exact(decoded, ('provider', 'state'))
    _identifier(decoded['provider'])
    if type(decoded['state']) is not list or not 1 <= len(decoded['state']) <= 64 or any(type(item) is not dict for item in decoded['state']):
        raise ContractError('Invalid opaque provider continuation')
    return decoded


def message_to_wire(message):
    row = dict(role=message.role, content=[part.text for part in message.content],
                calls=[dict(id=call.call_id, name=call.name, arguments=call.arguments) for call in message.calls],
                results=[dict(id=result.call_id, content=[part.text for part in result.content]) for result in message.results])
    if message.continuation is not None:
        continuation(message.continuation)
        row['continuation'] = message.continuation
    return row


def message_from_wire(row):
    keys = ('role', 'content', 'calls', 'results')
    _exact(row, (*keys, 'continuation') if type(row) is dict and 'continuation' in row else keys)
    state = row.get('continuation')
    if 'continuation' in row:
        continuation(state)
        if row['role'] != 'assistant':
            raise ContractError('Provider continuation requires an assistant message')
    if any(type(row[key]) is not list or len(row[key]) > 64 for key in ('content', 'calls', 'results')):
        raise ContractError('Invalid rich inference message arrays')
    calls, results = [], []
    for call in row['calls']:
        _exact(call, ('id', 'name', 'arguments'))
        object_json(call['arguments'])
        calls.append(ToolCall(_identifier(call['id']), _identifier(call['name'], name=True), _text(call['arguments'])))
    for result in row['results']:
        _exact(result, ('id', 'content'))
        if type(result['content']) is not list or len(result['content']) > 64:
            raise ContractError('Invalid rich inference result content')
        results.append(ToolResult(_identifier(result['id']), tuple(TextContent(_text(item)) for item in result['content'])))
    if (row['role'] not in ('system', 'user', 'assistant', 'tool')
            or (calls and row['role'] != 'assistant') or (results and row['role'] != 'tool')
            or (row['role'] == 'tool' and (row['content'] or not results))
            or not (row['content'] or calls or results)):
        raise ContractError('Invalid rich inference message role or content')
    return Message(row['role'], tuple(TextContent(_text(item)) for item in row['content']), tuple(calls), tuple(results), state)


def _bounded(row):
    try:
        if len(json.dumps(row, allow_nan=False, ensure_ascii=False).encode()) <= 131072:
            return
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    raise ContractError('Rich inference exceeds its JSON byte limit')


def request_to_wire(request):
    payload = request.payload
    if payload.generation.temperature is not None:
        raise ContractError('Temperature is outside this inference subset')
    row = dict(version=2, selected_llm=request.selected_llm, request_id=request.request_id,
               messages=[message_to_wire(item) for item in payload.messages],
               tools=[dict(name=tool.name, description=tool.description, schema=tool.input_schema) for tool in payload.tools],
               structured=None if payload.structured is None else dict(name=payload.structured.name, schema=payload.structured.schema),
               max_output_tokens=payload.generation.max_output_tokens, cache_policy=payload.generation.cache_policy)
    request_from_wire(row)
    return row


def request_from_wire(row):
    _exact(row, ('version','selected_llm','request_id','messages','tools','structured','max_output_tokens','cache_policy'))
    _bounded(row)
    try:
        if (type(row['version']) is not int or row['version'] != 2
                or type(row['messages']) is not list or not 1 <= len(row['messages']) <= 64
                or type(row['tools']) is not list or len(row['tools']) > 64):
            raise ValueError()
        if row['max_output_tokens'] is not None and (type(row['max_output_tokens']) is not int or not 1 <= row['max_output_tokens'] <= 4096):
            raise ValueError()
        tools = []
        for item in row['tools']:
            _exact(item, ('name','description','schema'))
            schema(item['schema'])
            tools.append(Tool(_identifier(item['name'], name=True), _text(item['description']), _text(item['schema'])))
        structured = row['structured']
        if structured is not None:
            _exact(structured, ('name','schema'))
            schema(structured['schema'])
            structured = StructuredOutput(_identifier(structured['name'], name=True), _text(structured['schema']))
        if not tools and structured is None:
            raise ValueError()
        messages = tuple(message_from_wire(item) for item in row['messages'])
        declared = {tool.name: schema(tool.input_schema) for tool in tools}
        pending, seen = {}, set()
        for message in messages:
            if pending and message.role != 'tool':
                raise ValueError()
            for call in message.calls:
                if call.call_id in seen or call.name not in declared:
                    raise ValueError()
                if not valid_schema(declared[call.name], object_json(call.arguments)):
                    raise ValueError()
                seen.add(call.call_id)
                pending[call.call_id] = call.name
            for result in message.results:
                if result.call_id not in pending:
                    raise ValueError()
                del pending[result.call_id]
        if pending:
            raise ValueError()
        return LLMRequest(_identifier(row['selected_llm']), CompletionRequest(messages, tuple(tools),
            GenerationOptions(row['max_output_tokens'], cache_policy=row['cache_policy']), structured), _identifier(row['request_id'], empty=True))
    except (ValueError, TypeError):
        raise ContractError('Invalid rich inference request') from None


def result_to_wire(result):
    from .inference import usage_to_wire
    if result.outcome != 'tool_calls' or not result.payload.calls or result.payload.results:
        raise ContractError('Tool-call results require the matching outcome')
    row = dict(version=2, message=message_to_wire(result.payload), outcome=result.outcome,
               usage=usage_to_wire(result.usage), actual_model=result.actual_model,
               service_tier=result.service_tier, serving_revision=result.serving_revision, billing=result.billing)
    result_from_wire(row)
    return row


def result_from_wire(row):
    _exact(row, ('version','message','outcome','usage','actual_model','service_tier','serving_revision','billing'))
    _bounded(row)
    try:
        if type(row['version']) is not int or row['version'] != 2 or row['outcome'] != 'tool_calls':
            raise ValueError()
        message = message_from_wire(row['message'])
        if not message.calls or message.results or len({call.call_id for call in message.calls}) != len(message.calls):
            raise ValueError()
        from .inference import result_from_wire as text_result
        checked = text_result(dict(text='', outcome='completed', usage=row['usage'], actual_model=row['actual_model'],
                                   service_tier=row['service_tier'], serving_revision=row['serving_revision'],billing=row['billing']))
        return LLMResult(message, checked.usage, row['outcome'], checked.actual_model, checked.service_tier,checked.serving_revision,checked.billing)
    except (TypeError, ValueError):
        raise ContractError('Invalid rich inference result') from None
