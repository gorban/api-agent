"""Version-two bounded structured/tool inference; version-one text stays valid."""
import json
import re
from hashlib import sha256
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import NoSuchResource, Unresolvable
from .contracts import ContractError, ProviderAccessError
from .llm import (CompletionRequest, GenerationOptions, LLMRequest, LLMResult,
                  Message, TextContent, Tool, ToolCall, ToolResult, StructuredOutput, Usage, CacheBoundary, cache_boundary_indexes, ResponsePolicy, ResponseEvidence)


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
        decoded = _json_value(value)
        if type(decoded) is not dict:
            raise ValueError()
        _bounded(decoded)
        _instance_bound(decoded)
        return decoded
    except (ValueError, TypeError, RecursionError):
        raise ContractError('Invalid rich inference JSON object') from None


def _instance_bound(value):
    pending, count = [(value, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 8192 or depth > 32 or (type(node) is list and len(node) > 1024):
            raise ValueError()
        children = node.values() if type(node) is dict else node if type(node) is list else ()
        pending.extend((child, depth + 1) for child in children)


def _references(value, root=None, depth=0, active=(), budget=None):
    """Bound schema complexity and permit only resolvable local JSON pointers."""
    root = value if root is None else root
    budget = [4096] if budget is None else budget
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise ValueError()
    if type(value) is dict:
        if any(key in value for key in ('$dynamicRef', '$recursiveRef', '$id')):
            raise ValueError()
        if '$ref' in value:
            child = value['$ref']
            if type(child) is not str or not child.startswith('#/') or child in active:
                raise ValueError()
            target = root
            for part in child[2:].split('/'):
                token = part.replace('~1', '/').replace('~0', '~')
                if type(target) is list:
                    if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token.startswith('0')):
                        raise ValueError()
                    target = target[int(token)]
                else:
                    target = target[token]
            if type(target) not in (dict, bool):
                raise ValueError()
            _references(target, root, depth + 1, (*active, child), budget)
        for key in ('$defs', 'definitions', 'properties', 'patternProperties', 'dependentSchemas'):
            children = value.get(key, {})
            if type(children) is dict:
                for child in children.values():
                    _references(child, root, depth + 1, active, budget)
        for key in ('items', 'additionalProperties', 'contains', 'not', 'if', 'then', 'else', 'propertyNames', 'unevaluatedProperties', 'unevaluatedItems'):
            if key in value:
                _references(value[key], root, depth + 1, active, budget)
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            if type(value.get(key)) is list:
                for child in value[key]:
                    _references(child, root, depth + 1, active, budget)


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


def valid_schema(definition, value):
    """Never permit validation to retrieve remote schema resources."""
    return schema_verdict(definition, value) is True


def schema_verdict(definition, value):
    """Separate observed invalid data from unavailable local validation."""
    try:
        _references(definition)
        _instance_bound(value)
        if not _fast_schema(definition):
            from .bounded_schema import isolated_schema_verdict
            return isolated_schema_verdict(definition, value)
        return Draft202012Validator(definition, registry=Registry(retrieve=_no_remote)).is_valid(value)
    except (ValueError, TypeError, KeyError, IndexError, Unresolvable, RecursionError):
        return None



async def async_valid_schema(definition, value):
    return await async_schema_verdict(definition, value) is True


async def async_schema_verdict(definition, value):
    try:
        _references(definition)
        _instance_bound(value)
        if not _fast_schema(definition):
            from .bounded_schema import async_isolated_schema_verdict
            return await async_isolated_schema_verdict(definition, value)
        return Draft202012Validator(definition, registry=Registry(retrieve=_no_remote)).is_valid(value)
    except (ValueError, TypeError, KeyError, IndexError, Unresolvable, RecursionError):
        return None


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


def _fast_schema(definition):
    try:
        _strict(definition)
    except (ValueError, TypeError):
        return False
    pending = [definition]
    while pending:
        node = pending.pop()
        if 'enum' in node:
            return False
        pending.extend(node.get('properties', {}).values())
        if 'items' in node:
            pending.append(node['items'])
    return True


def schema(value, strict=True):
    try:
        _text(value, 32768)
        decoded = object_json(value)
        _references(decoded)
        Draft202012Validator.check_schema(decoded)
        if type(strict) is not bool:
            raise ValueError()
        if strict:
            _strict(decoded)
        if decoded.get('type') != 'object' and (strict or 'type' in decoded or not any(key in decoded for key in ('allOf', 'anyOf', 'oneOf', '$ref'))):
            raise ValueError()
        return decoded
    except (ValueError, TypeError, KeyError, IndexError, SchemaError, RecursionError):
        raise ContractError('Rich inference requires a strict object schema') from None


def _response_dialects(definition):
    """Only draft 2020-12 schema positions may declare an explicit dialect."""
    pending = [definition]
    # _references has already bounded depth and total schema visits.
    seen = set()
    while pending:
        node = pending.pop()
        if type(node) is not dict or id(node) in seen:
            continue
        seen.add(id(node))
        if node.get('$schema', 'https://json-schema.org/draft/2020-12/schema') != 'https://json-schema.org/draft/2020-12/schema':
            raise ValueError()
        pending.extend(DRAFT202012.subresources_of(node))
        if '$ref' in node:
            # _references already admitted this bounded, root-relative pointer.
            target = definition
            for part in node['$ref'][2:].split('/'):
                token = part.replace('~1', '/').replace('~0', '~')
                target = target[int(token)] if type(target) is list else target[token]
            pending.append(target)



def response_schema(value):
    """Admit absent or exact HTTPS 2020-12 dialects at schema positions only."""
    try:
        _text(value, 32768)
        decoded = object_json(value)
        _references(decoded)
        Draft202012Validator.check_schema(decoded)
        _response_dialects(decoded)
        return decoded
    except (ValueError, TypeError, KeyError, IndexError, SchemaError, RecursionError):
        raise ContractError('Invalid original response schema') from None


def _json_value(text, *, allow_markdown_fences=False, previous_width=None, layers=0):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except json.JSONDecodeError:
        if not allow_markdown_fences or layers >= 4:
            raise
        # A closed whole-response envelope, not a general Markdown document.
        opening, _, remainder = text.strip().partition('\n')
        contents, separator, closing = remainder.rpartition('\n')
        envelope = re.fullmatch(r'(`{3,})(?:json)?[ \t]*\r?', opening)
        if envelope is None or not separator or closing.rstrip(' \t\r') != envelope[1]:
            raise ValueError()
        width = len(envelope[1])
        if previous_width is not None and width >= previous_width:
            raise ValueError()
        return _json_value(contents, allow_markdown_fences=True,
                           previous_width=width, layers=layers + 1)


def _response_json(text, *, allow_markdown_fences=False):
    try:
        _text(text)
        value = _json_value(text, allow_markdown_fences=allow_markdown_fences)
        _instance_bound(value)
        _bounded(value)
        return value
    except (ValueError, TypeError, RecursionError):
        raise ContractError('Invalid generated response JSON') from None


def _prompted_json(result):
    return result.response is not None and result.response.actual_mode == 'prompted_json'


def response_json(result):
    """Decode a validated JSON result without changing its provider text/evidence.

    Only recorded prompted-JSON generation permits bounded fence envelopes.
    Missing evidence and native modes retain strict JSON parsing.
    """
    return _response_json(''.join(part.text for part in result.payload.content),
                          allow_markdown_fences=_prompted_json(result))


def _validation_value(text, strict, *, allow_markdown_fences=False):
    return (_response_json(text, allow_markdown_fences=allow_markdown_fences)
            if strict is None else object_json(text))


def _validation_schema(definition, strict):
    return response_schema(definition) if strict is None else schema(definition, strict)


def _identifier(value, *, name=False, empty=False):
    _text(value, 64 if name else 200)
    if ((not value and not empty) or (value and (not value.isprintable() or value.strip() != value))
            or (name and re.fullmatch(r'[A-Za-z0-9_-]+', value) is None)):
        raise ContractError('Invalid rich inference identifier')
    return value


def _exact(row, keys):
    if type(row) is not dict or set(row) != set(keys):
        raise ContractError('Invalid rich inference fields')


def _text(value, maximum=524288):
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
    if message.cache_boundary is not None:
        row['cache_boundary'] = dict(lifetime_seconds=message.cache_boundary.lifetime_seconds)
    if message.continuation is not None:
        continuation(message.continuation)
        row['continuation'] = message.continuation
    return row


def message_from_wire(row):
    keys = ('role', 'content', 'calls', 'results')
    extras = tuple(key for key in ('continuation', 'cache_boundary') if type(row) is dict and key in row)
    _exact(row, (*keys, *extras))
    boundary = row.get('cache_boundary')
    if boundary is not None:
        _exact(boundary, ('lifetime_seconds',))
        boundary = CacheBoundary(boundary['lifetime_seconds'])
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
    return Message(row['role'], tuple(TextContent(_text(item)) for item in row['content']), tuple(calls), tuple(results), state, boundary)


def _bounded(row):
    try:
        json.dumps(row, allow_nan=False, ensure_ascii=False).encode()
        if len(json.dumps(row, allow_nan=False, ensure_ascii=True).encode()) <= 2097152:
            return
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    raise ContractError('Rich inference exceeds its JSON byte limit')


_RESPONSE_KEYS = ('requested_mode', 'fallback', 'actual_mode', 'schema_sha256', 'strict', 'fallback_reason')


def response_evidence_to_wire(evidence):
    if not isinstance(evidence, ResponseEvidence):
        raise ContractError('Invalid response evidence')
    row = {name: getattr(evidence, name) for name in _RESPONSE_KEYS}
    response_evidence_from_wire(row)
    return row


def response_evidence_from_wire(row):
    _exact(row, _RESPONSE_KEYS)
    try:
        return ResponseEvidence(**row)
    except (TypeError, ValueError):
        raise ContractError('Invalid response evidence') from None


def _response_to_wire(policy):
    definition = policy.schema
    return dict(mode=policy.mode, fallback=policy.fallback, schema=None if definition is None else
                dict(name=definition.name, schema=definition.schema, strict=definition.strict))


def _response_from_wire(row):
    _exact(row, ('mode', 'fallback', 'schema'))
    definition = row['schema']
    if definition is not None:
        _exact(definition, ('name', 'schema', 'strict'))
        response_schema(definition['schema'])
        definition = StructuredOutput(_identifier(definition['name'], name=True), _text(definition['schema']), definition['strict'])
    return ResponsePolicy(row['mode'], row['fallback'], definition)


def validate_response_evidence(request, result):
    policy, evidence = request.payload.response, result.response
    if policy is None:
        valid = evidence is None
    elif evidence is None:
        valid = False
    else:
        response_evidence_to_wire(evidence)
        definition = policy.schema
        valid = (evidence.requested_mode == policy.mode and evidence.fallback == policy.fallback
                 and evidence.schema_sha256 == (None if definition is None else sha256(definition.schema.encode()).hexdigest())
                 and evidence.strict == (None if definition is None else definition.strict))
    if not valid:
        raise ProviderAccessError('unknown', 'Response evidence does not match its request', usage=getattr(result, 'usage', None), response=evidence)


def request_to_wire(request, *, _validate_instances=True):
    payload = request.payload
    row = dict(version=2, selected_llm=request.selected_llm, request_id=request.request_id,
               messages=[message_to_wire(item) for item in payload.messages],
               tools=[dict(name=tool.name, description=tool.description, schema=tool.input_schema, **({} if tool.strict else {"strict": False})) for tool in payload.tools],
               structured=None if payload.structured is None else dict(name=payload.structured.name, schema=payload.structured.schema, **({} if payload.structured.strict else {"strict": False})),
               max_output_tokens=payload.generation.max_output_tokens, cache_policy=payload.generation.cache_policy)
    if payload.response is not None:
        row.update(version=3, response=_response_to_wire(payload.response))
    if payload.generation.temperature is not None:
        row['temperature'] = payload.generation.temperature
    if payload.generation.reasoning_effort is not None:
        row['reasoning_effort'] = payload.generation.reasoning_effort
    request_from_wire(row, _validate_instances=_validate_instances)
    return row


def request_from_wire(row, *, _validate_instances=True):
    keys = ('version','selected_llm','request_id','messages','tools','structured','max_output_tokens','cache_policy')
    extras = tuple(key for key in ('reasoning_effort', 'temperature', 'response') if type(row) is dict and key in row)
    _exact(row, (*keys, *extras))
    _bounded(row)
    try:
        if (type(row['version']) is not int or row['version'] not in (2, 3)
                or type(row['messages']) is not list or not 1 <= len(row['messages']) <= 64
                or type(row['tools']) is not list or len(row['tools']) > 64):
            raise ValueError()
        if row['max_output_tokens'] is not None and (type(row['max_output_tokens']) is not int or not 1 <= row['max_output_tokens'] <= 1048576):
            raise ValueError()
        if (row['version'] == 3) != ('response' in row) or (row['version'] == 3 and row['structured'] is not None):
            raise ValueError()
        response = _response_from_wire(row['response']) if row['version'] == 3 else None
        tools = []
        for item in row['tools']:
            _exact(item, ('name','description','schema', *(() if 'strict' not in item else ('strict',))))
            schema(item['schema'], item.get('strict', True))
            tools.append(Tool(_identifier(item['name'], name=True), _text(item['description']), _text(item['schema']), item.get('strict', True)))
        structured = row['structured']
        if structured is not None:
            _exact(structured, ('name','schema', *(() if 'strict' not in structured else ('strict',))))
            schema(structured['schema'], structured.get('strict', True))
            structured = StructuredOutput(_identifier(structured['name'], name=True), _text(structured['schema']), structured.get('strict', True))
        messages = tuple(message_from_wire(item) for item in row['messages'])
        declared = {tool.name: schema(tool.input_schema, tool.strict) for tool in tools}
        pending, seen = {}, set()
        for message in messages:
            if pending and message.role != 'tool':
                raise ValueError()
            for call in message.calls:
                if call.call_id in seen or call.name not in declared:
                    raise ValueError()
                if _validate_instances and not valid_schema(declared[call.name], object_json(call.arguments)):
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
            GenerationOptions(row['max_output_tokens'], temperature=row.get('temperature'), cache_policy=row['cache_policy'], reasoning_effort=row.get('reasoning_effort')), structured, response), _identifier(row['request_id'], empty=True))
    except (ValueError, TypeError):
        raise ContractError('Invalid rich inference request') from None


def result_to_wire(result):
    from .inference import usage_to_wire
    if result.response is None and (result.outcome != 'tool_calls' or not result.payload.calls or result.payload.results):
        raise ContractError('Tool-call results require the matching outcome')
    row = dict(version=2, message=message_to_wire(result.payload), outcome=result.outcome,
               usage=usage_to_wire(result.usage), actual_model=result.actual_model,
               service_tier=result.service_tier, serving_revision=result.serving_revision, billing=result.billing)
    if result.response is not None:
        row.update(version=3, response=response_evidence_to_wire(result.response))
    if result.applied_cache_boundaries:
        row['applied_cache_boundaries'] = list(result.applied_cache_boundaries)
    result_from_wire(row)
    return row


def result_from_wire(row):
    _exact(row, ('version','message','outcome','usage','actual_model','service_tier','serving_revision','billing', *(() if 'applied_cache_boundaries' not in row else ('applied_cache_boundaries',)), *(() if 'response' not in row else ('response',))))
    _bounded(row)
    try:
        if type(row['version']) is not int or row['version'] not in (2, 3):
            raise ValueError()
        if (row['version'] == 3) != ('response' in row):
            raise ValueError()
        response = response_evidence_from_wire(row['response']) if row['version'] == 3 else None
        if row['outcome'] not in ('completed', 'length', 'refused', 'tool_calls') or (row['version'] == 2 and row['outcome'] != 'tool_calls'):
            raise ValueError()
        message = message_from_wire(row['message'])
        if message.role != 'assistant' or message.results or bool(message.calls) != (row['outcome'] == 'tool_calls') or len({call.call_id for call in message.calls}) != len(message.calls):
            raise ValueError()
        from .inference import result_from_wire as text_result, _cache_boundaries
        checked = text_result(dict(text='', outcome='completed', usage=row['usage'], actual_model=row['actual_model'],
                                   service_tier=row['service_tier'], serving_revision=row['serving_revision'],billing=row['billing']))
        return LLMResult(message, checked.usage, row['outcome'], checked.actual_model, checked.service_tier,checked.serving_revision,checked.billing, _cache_boundaries(row), response)
    except (TypeError, ValueError):
        raise ContractError('Invalid rich inference result') from None


def _response_output_checks(request, result):
    if not isinstance(result, LLMResult):
        raise ContractError("Invalid inference result")
    validate_response_evidence(request, result)
    if result.payload.results or bool(result.payload.calls) != (result.outcome == "tool_calls"):
        raise ProviderAccessError("unknown", "Inference outcome does not match its payload", usage=result.usage, response=result.response)
    if request.payload.response is not None and request.payload.response.schema is not None and result.outcome == 'completed':
        definition = request.payload.response.schema
        yield definition.schema, None, ''.join(part.text for part in result.payload.content), 'Response output does not match its original schema'


def _output_checks(request, result, selection=None):
    for check in _response_output_checks(request, result):
        yield (*check, None)
    eligible = () if selection is None else cache_boundary_indexes(request, selection)
    if not set(result.applied_cache_boundaries).issubset(eligible):
        raise ProviderAccessError('unknown', 'Invalid applied cache boundary', usage=result.usage, response=result.response)
    tools = {tool.name: (index, tool) for index, tool in enumerate(request.payload.tools, 1)}
    for call_index, call in enumerate(result.payload.calls, 1):
        declared = tools.get(call.name)
        if declared is None:
            raise ProviderAccessError('unknown', f'Generated tool call {call_index} names an undeclared tool',
                                      code='tool_name_undeclared', type='tool_call_validation',
                                      usage=result.usage, response=result.response, origin='local_output_validation')
        tool_index, tool = declared
        message = f'Generated arguments for tool call {call_index} (declared tool {tool_index})'
        yield tool.input_schema, tool.strict, call.arguments, message, 'tool_arguments_schema'
    if request.payload.structured is not None and result.outcome == 'completed':
        definition = request.payload.structured
        yield definition.schema, definition.strict, ''.join(part.text for part in result.payload.content), 'Structured output does not match its schema', None


def _output_error(result, message, code, definition, text, verdict, malformed):
    if code is None and malformed and not text.strip():
        return ProviderAccessError('unknown', 'Completed inference returned no final-answer JSON',
            code='empty_final_answer', type='response_validation', origin='local_output_validation',
            usage=result.usage, response=result.response)
    if code is not None:
        if malformed:
            message += ': expected a bounded, unambiguous JSON object'
            code = 'tool_arguments_json'
        elif verdict is None:
            message += ': local schema validation is unavailable'
            code = 'tool_validation_unavailable'
        else:
            message += ' do not match its input schema'
            # Only describe schema positions, never generated values or private
            # field names. Complex schema diagnostics remain in gated evidence.
            value = object_json(text)
            if any(name not in value for name in json.loads(definition).get('required', ())):
                message += ': missing required property'
    return ProviderAccessError('unknown', message, code=code,
                               type='tool_call_validation' if code is not None else None,
                               usage=result.usage, response=result.response, origin='local_output_validation')


def _output_inputs(definition, strict, text, *, allow_markdown_fences=False):
    try:
        parsed = _validation_schema(definition, strict)
    except ContractError:
        return None, None, False
    try:
        return parsed, _validation_value(text, strict, allow_markdown_fences=allow_markdown_fences), False
    except ContractError:
        return parsed, None, True


def _bounded_result(result):
    from .inference import result_to_wire
    try:
        result_to_wire(result)
    except (ValueError, TypeError, RecursionError):
        raise ProviderAccessError('unknown', 'Invalid bounded inference result', usage=result.usage, response=result.response) from None
    return result


def validate_result(request, result, *, selection=None):
    """Validate actual output against the originating unmodified schemas."""
    for definition, strict, text, message, code in _output_checks(request, result, selection):
        parsed, value, malformed = _output_inputs(definition, strict, text, allow_markdown_fences=_prompted_json(result))
        verdict = None if parsed is None else False if malformed else schema_verdict(parsed, value)
        if verdict is not True:
            raise _output_error(result, message, code, definition, text, verdict, malformed)
    return _bounded_result(result)


def validate_response_result(request, result):
    """Admit original policy output at IPC without inventing route capabilities."""
    for definition, strict, text, message in _response_output_checks(request, result):
        if not _valid_output(definition, strict, text, allow_markdown_fences=_prompted_json(result)):
            raise ProviderAccessError('unknown', message, usage=result.usage, response=result.response)
    return result


async def async_validate_result(request, result, *, selection=None):
    for definition, strict, text, message, code in _output_checks(request, result, selection):
        parsed, value, malformed = _output_inputs(definition, strict, text, allow_markdown_fences=_prompted_json(result))
        verdict = None if parsed is None else False if malformed else await async_schema_verdict(parsed, value)
        if verdict is not True:
            raise _output_error(result, message, code, definition, text, verdict, malformed)
    return _bounded_result(result)


def _valid_output(definition, strict, text, *, allow_markdown_fences=False):
    try:
        return valid_schema(_validation_schema(definition, strict),
                            _validation_value(text, strict, allow_markdown_fences=allow_markdown_fences))
    except ContractError:
        return False


def validate_catalog_selection(values, request, *, clamp_output_limit=False):
    from .catalog import catalog_to_wire
    from .control import OperationalError
    catalog_to_wire(values)
    selected = next((value for value in values if value.selected_llm == request.selected_llm), None)
    if selected is None:
        raise OperationalError('unsupported')
    if clamp_output_limit and selected.max_output_tokens is not None:
        from .output_limits import with_output_limit
        maximum = request.payload.generation.max_output_tokens
        if maximum is not None:
            request = with_output_limit(request, min(maximum, selected.max_output_tokens))
    selected.validate(request)
    return selected


async def validate_request_instances(request):
    definitions = {tool.name: schema(tool.input_schema, tool.strict) for tool in request.payload.tools}
    for message in request.payload.messages:
        for call in message.calls:
            if not await async_valid_schema(definitions[call.name], object_json(call.arguments)):
                raise ContractError('Invalid replayed tool arguments')


async def resolve_catalog_selection(operation, request, *, clamp_output_limit=False):
    from .contracts import SetupPrerequisiteError
    from .control import OperationalError
    optional_catalog = (request.payload.generation.max_output_tokens is None
                        and not any(message.cache_boundary is not None for message in request.payload.messages))
    try:
        values = await operation()
    except SetupPrerequisiteError:
        if request.payload.generation.max_output_tokens is None:
            return None
        raise
    except OperationalError as error:
        if request.payload.generation.max_output_tokens is None and error.kind in ('unsupported', 'unavailable'):
            return None
        if optional_catalog:
            if (clamp_output_limit and isinstance(error, ProviderAccessError)
                    and error.kind == 'authorization' and error.status == 403 and error.origin == 'provider'
                    and error.usage is None and error.response is None):
                return None
        raise
    if clamp_output_limit and optional_catalog:
        # Optional ceiling discovery must not introduce unrelated catalog gates.
        from .catalog import catalog_to_wire
        catalog_to_wire(values)
        return next((value for value in values if value.selected_llm == request.selected_llm), None)
    return validate_catalog_selection(values, request, clamp_output_limit=clamp_output_limit)
