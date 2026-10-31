"""Public Responses/Chat Completions structured and function-call mappings."""
import json
from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall
from api_agent.providers.contracts import ProviderAccessError, ContractError
from api_agent.providers.rich_inference import request_to_wire, result_to_wire, schema, object_json, continuation, valid_schema, _identifier, response_json


from api_agent.providers.response_metadata import _logprob, _logprobs

_PROVIDER = 'openai.responses'


def _native_state(items, message=None):
    """Retain native replay metadata; reasoning remains encrypted-only."""
    calls, text, reasoning, metadata, identifiers = [], [], False, False, set()
    if type(items) is not list or not 1 <= len(items) <= 64:
        raise ContractError('Invalid native provider continuation')
    for item in items:
        if type(item) is not dict:
            raise ContractError('Invalid native provider continuation')
        kind = item.get('type')
        allowed = {'type', 'id', 'status'}
        if kind == 'reasoning':
            allowed |= {'encrypted_content', 'summary'}
            encrypted = item.get('encrypted_content')
            if (type(encrypted) is not str or not encrypted or len(encrypted) > 32768
                    or not encrypted.isprintable() or item.get('summary') != []):
                raise ContractError('Provider continuation requires encrypted reasoning')
            reasoning = True
        elif kind == 'function_call':
            allowed |= {'call_id', 'name', 'arguments'}
            calls.append(ToolCall(_identifier(item.get('call_id')), _identifier(item.get('name'), name=True), item.get('arguments')))
            object_json(item['arguments'])
        elif kind == 'message':
            allowed |= {'role', 'content', 'phase'}
            phase = item.get('phase')
            if phase is not None and (type(phase) is not str or phase not in ('commentary', 'final_answer')):
                raise ContractError('Invalid native message phase')
            metadata |= 'phase' in item
            if item.get('role') != 'assistant' or type(item.get('content')) is not list:
                raise ContractError('Invalid native provider continuation')
            for part in item['content']:
                if type(part) is not dict or set(part) - {'type', 'text', 'annotations', 'logprobs'} or part.get('type') != 'output_text' or type(part.get('text')) is not str or part.get('annotations', []) != []:
                    raise ContractError('Invalid native provider continuation')
                _logprobs(part.get('logprobs'))
                metadata |= 'logprobs' in part
                text.append(TextContent(part['text']))
        else:
            raise ContractError('Invalid native provider continuation')
        identifier = _identifier(item.get('id'))
        if set(item) - allowed or item.get('status') not in (None, 'completed') or identifier in identifiers:
            raise ContractError('Invalid native provider continuation')
        identifiers.add(identifier)
    if not (reasoning or metadata) or not calls or (message is not None and (tuple(calls) != message.calls or tuple(text) != message.content)):
        raise ContractError('Provider continuation does not match assistant output')
    return items


def payload(request, mode, subscription, *, _validate_instances=True):
    try:
        request_to_wire(request, _validate_instances=_validate_instances)
    except ContractError:
        raise ProviderAccessError('invalid_request', 'Invalid structured/tool inference request.') from None
    if request.payload.generation.temperature is not None:
        raise ProviderAccessError('unsupported', 'GPT inference does not support temperature.')
    identifier = request.selected_llm
    if not identifier.startswith(('gpt-', 'openai/gpt-')) or not identifier.isprintable() or identifier.strip() != identifier:
        raise ProviderAccessError('unsupported', 'GPT inference requires an explicit GPT selection.')
    maximum = request.payload.generation.max_output_tokens
    if subscription and maximum is not None:
        raise ProviderAccessError('unsupported', 'GPT subscription inference does not support max_output_tokens.')
    rows = []
    for message in request.payload.messages:
        text = ''.join(part.text for part in message.content)
        if message.continuation is not None:
            envelope = continuation(message.continuation)
            if mode != 'responses' or envelope['provider'] != _PROVIDER:
                raise ProviderAccessError('unsupported', 'GPT inference cannot replay this provider continuation.')
            try:
                rows.extend(_native_state(envelope['state'], message))
            except (ContractError, ValueError):
                raise ProviderAccessError('invalid_request', 'Invalid GPT provider continuation.') from None
            continue
        if mode == 'responses':
            if text:
                rows.append(dict(role='developer' if subscription and message.role == 'system' else message.role, content=text))
            rows.extend(dict(type='function_call', call_id=call.call_id, name=call.name, arguments=call.arguments) for call in message.calls)
            rows.extend(dict(type='function_call_output', call_id=item.call_id, output=''.join(part.text for part in item.content)) for item in message.results)
        else:
            if message.role != 'tool':
                row = dict(role=message.role, content=text or None)
                if message.calls:
                    row['tool_calls'] = [dict(id=call.call_id, type='function', function=dict(name=call.name,arguments=call.arguments)) for call in message.calls]
                rows.append(row)
            rows.extend(dict(role='tool', tool_call_id=item.call_id, content=''.join(part.text for part in item.content)) for item in message.results)
    body = dict(model=identifier, stream=subscription)
    if mode == 'responses':
        body.update(input=rows, store=False, include=['reasoning.encrypted_content'])
        if not subscription:
            body['max_output_tokens'] = maximum or 1024
    else:
        body.update(messages=rows, max_completion_tokens=maximum or 1024)
    tools = [dict(name=tool.name, description=tool.description, parameters=schema(tool.input_schema, strict=tool.strict), strict=tool.strict) for tool in request.payload.tools]
    if tools:
        body['tools'] = [dict(type='function', **tool) if mode == 'responses' else dict(type='function', function=tool) for tool in tools]
        body['parallel_tool_calls'] = False
    structured = request.payload.structured
    if structured is not None:
        definition = dict(name=structured.name, schema=schema(structured.schema, strict=structured.strict), strict=structured.strict)
        if mode == 'responses':
            body['text'] = dict(format=dict(type='json_schema', **definition))
        else:
            body['response_format'] = dict(type='json_schema', json_schema=definition)
    return body


def _protected(value, token):
    if type(value) is str:
        return token in ''.join(c for c in value if c.isprintable())
    if type(value) is dict:
        return any(_protected(key, token) or _protected(item, token) for key, item in value.items())
    if type(value) is list:
        return any(_protected(item, token) for item in value)
    return False


def tool_result(data, responses, token):
    from .inference import _invalid, _usage
    calls, text = [], []
    state = None
    try:
        if _protected(data, token):
            raise ValueError()
        if responses:
            if data['status'] != 'completed':
                raise ValueError()
            for item in data['output']:
                if item.get('type') == 'function_call':
                    if item.get('status') != 'completed':
                        raise ValueError()
                    calls.append(ToolCall(item['call_id'], item['name'], item['arguments']))
                elif item.get('type') == 'message' and item.get('role') == 'assistant':
                    for part in item['content']:
                        if part.get('type') != 'output_text' or type(part.get('text')) is not str:
                            raise ValueError()
                        text.append(TextContent(part['text']))
                elif item.get('type') != 'reasoning':
                    raise ValueError()
        else:
            choice, = data['choices']
            if choice['finish_reason'] != 'tool_calls' or choice['message']['role'] != 'assistant':
                raise ValueError()
            message = choice['message']
            if message.get('refusal'):
                raise ValueError()
            if message.get('content'):
                text.append(TextContent(message['content']))
            for item in message['tool_calls']:
                if item['type'] != 'function':
                    raise ValueError()
                calls.append(ToolCall(item['id'], item['function']['name'], item['function']['arguments']))
        if _protected(''.join(part.text for part in text), token):
            raise ValueError()
        if responses and any(item.get('type') == 'reasoning' or
                (item.get('type') == 'message' and ('phase' in item or
                 any('logprobs' in part for part in item['content']))) for item in data['output']):
            native = _native_state(data['output'])
            state = json.dumps(dict(provider=_PROVIDER, state=native), allow_nan=False, separators=(',', ':'))
        if not calls or len(calls) > 64 or len({call.call_id for call in calls}) != len(calls):
            raise ValueError()
        for call in calls:
            if _protected(object_json(call.arguments), token):
                raise ValueError()
        result = LLMResult(Message('assistant', tuple(text), tuple(calls), continuation=state), _usage(data.get('usage'), responses),
                         'tool_calls', data.get('model'), data.get('service_tier'), data.get('serving_revision'))
        result_to_wire(result)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, ContractError):
        raise _invalid('GPT inference returned invalid function calls.') from None


def validate_result(request, result, token=None):
    """Validate successful native output against this call's declared contracts."""
    from .inference import _invalid
    try:
        if request.payload.response is not None:
            from api_agent.providers.rich_inference import validate_result as validate
            validate(request, result)
            if request.payload.response.schema is not None and result.outcome == 'completed':
                value = response_json(result)
                if token is not None and _protected(value, token):
                    raise ValueError()
        if result.payload.calls:
            from api_agent.providers.rich_inference import validate_result as validate
            validate(request, result)
        elif request.payload.structured is not None and result.outcome == 'completed':
            value = object_json(''.join(part.text for part in result.payload.content))
            if token is not None and _protected(value, token):
                raise ValueError()
            if not valid_schema(schema(request.payload.structured.schema, strict=request.payload.structured.strict), value):
                raise ValueError()
        return result
    except (ValueError, ContractError, ProviderAccessError) as cause:
        if isinstance(cause, ProviderAccessError) and cause.origin == 'local_output_validation':
            raise
        error = _invalid('GPT inference returned output outside its declared contract.')
        error.usage = result.usage
        error.response = result.response
        raise error from None


async def async_validate_result(request, result, token=None):
    from api_agent.providers.rich_inference import async_validate_result as validate
    from .inference import _invalid
    try:
        if (request.payload.structured is not None or
                request.payload.response is not None and request.payload.response.schema is not None) and result.outcome == 'completed':
            value = (response_json(result) if request.payload.response is not None else
                     object_json(''.join(part.text for part in result.payload.content)))
            if token is not None and _protected(value, token):
                raise ValueError()
        return await validate(request, result)
    except (ValueError, ContractError, ProviderAccessError) as cause:
        if isinstance(cause, ProviderAccessError) and cause.origin == 'local_output_validation':
            raise
        error = _invalid('GPT inference returned output outside its declared contract.')
        error.usage = result.usage
        error.response = result.response
        raise error from None
