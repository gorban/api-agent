"""Text and client-tool codecs for the public Bedrock Runtime Converse API.

https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
The SDK owns authentication and transport; this module has no side effects.
"""
import json
import re
from copy import deepcopy

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import LLMRequest, LLMResult, Message, TextContent, ToolCall, Usage
from api_agent.providers.rich_inference import continuation, request_to_wire, schema
from .catalog import selection
from .mantle import _bounded, _fields, _require, _text


def validate_request(request, *, endpoint='runtime', api_mode='converse', config=None):
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock Converse request.')
    from .converse_controls import effective_request
    request = effective_request(request, config)
    selected = selection(request.selected_llm, api_mode, endpoint=endpoint)
    selected.validate(request)
    payload = request.payload
    if any(tool.strict for tool in payload.tools) and 'structured' not in selected.features:
        raise ProviderAccessError('unsupported', 'This Bedrock Converse model does not support strict tools.')
    if (payload.structured is not None and not payload.structured.strict
            or payload.generation.cache_policy is not None
            or any(message.cache_boundary is not None
                   for message in payload.messages)):
        raise ProviderAccessError('unsupported', 'Bedrock Converse request feature is unsupported.')
    if payload.generation.temperature is not None and payload.generation.temperature > 1:
        raise ProviderAccessError('invalid_request', 'Bedrock Converse temperature must be at most 1.')
    from .converse_controls import fields
    fields(request, config)
    for message in payload.messages:
        if message.continuation is not None:
            continuation_message(message, request.selected_llm)
    try:
        request_to_wire(request)
    except ContractError:
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock Converse request.') from None


def _append(messages, role, content):
    if not content:
        return
    if messages and messages[-1]['role'] == role:
        messages[-1]['content'].extend(content)
    else:
        messages.append({'role': role, 'content': content})


def continuation_message(message, selected_llm):
    """Validate exact signed native blocks before sending them back to AWS."""
    try:
        envelope = continuation(message.continuation)
        if re.fullmatch(r'bedrock\.converse(?:\.[0-9a-f]{64})?', envelope['provider']) is None or len(envelope['state']) != 1:
            raise ContractError('Invalid Converse continuation owner')
        native = envelope['state'][0]
        parsed = decode({'output': {'message': native}, 'stopReason':'tool_use' if message.calls else 'end_turn',
                         'usage':{'inputTokens':0,'outputTokens':0,'totalTokens':0}}, selected_llm=selected_llm)
        if (parsed.payload.continuation is None or message.role != 'assistant' or message.results
                or parsed.payload.content != message.content or parsed.payload.calls != message.calls):
            raise ContractError('Converse continuation differs from its visible message')
        return deepcopy(native)
    except (ContractError, ProviderAccessError, ValueError, TypeError):
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock Converse continuation.') from None


def encode(request, api_mode='converse', *, endpoint='runtime', config=None):
    validate_request(request, endpoint=endpoint, api_mode=api_mode, config=config)
    from .converse_controls import effective_request
    request = effective_request(request, config)
    payload = request.payload
    system, messages = [], []
    for message in payload.messages:
        if message.continuation is not None:
            messages.append(continuation_message(message, request.selected_llm))
            continue
        blocks = [{'text': part.text} for part in message.content if part.text]
        if message.role == 'system':
            system.extend(blocks)
            continue
        blocks.extend({'toolUse': {'toolUseId': call.call_id, 'name': call.name,
                                   'input': json.loads(call.arguments)}} for call in message.calls)
        blocks.extend({'toolResult': {'toolUseId': result.call_id,
            'content': [{'text': part.text} for part in result.content]}} for result in message.results)
        _append(messages, 'user' if message.role == 'tool' else message.role, blocks)
    body = {'modelId': request.selected_llm, 'messages': messages,
            'inferenceConfig': {'maxTokens': payload.generation.max_output_tokens or 1024}}
    if payload.structured is not None:
        definition = payload.structured
        schema(definition.schema, definition.strict)
        body['outputConfig'] = {'textFormat': {'type': 'json_schema', 'structure': {
            'jsonSchema': {'name': definition.name, 'schema': definition.schema}}}}
    if system:
        body['system'] = system
    if payload.generation.temperature is not None:
        body['inferenceConfig']['temperature'] = payload.generation.temperature
    if payload.tools:
        body['toolConfig'] = {'tools': [{'toolSpec': {'name': tool.name,
            'description': tool.description,
            **({'strict': tool.strict} if 'structured' in selection(request.selected_llm, api_mode, endpoint=endpoint).features else {}),
            'inputSchema': {'json': schema(tool.input_schema, tool.strict)}}}
            for tool in payload.tools]}
    from .converse_controls import fields, kind
    additional = fields(request, config)
    if payload.structured is not None and kind(request.selected_llm) == 'gpt':
        additional['text'] = {'format': {'strict': True}}
    if additional:
        body['additionalModelRequestFields'] = additional
    return body


def format_payload(request, body, evidence):
    from api_agent.llm_providers.response_policy import format_payload as format_shared
    formatted = format_shared(request, body, evidence)
    if formatted is body:
        return body
    # The shared fallback appends one instruction. Translate its content to the
    # Converse union without changing the authored history or evidence position.
    messages = [*formatted['messages'][:-1]]
    instruction = {'text': formatted['messages'][-1]['content']}
    if messages and messages[-1]['role'] == 'user':
        messages[-1] = {**messages[-1], 'content': [*messages[-1]['content'], instruction]}
    else:
        messages.append({'role': 'user', 'content': [instruction]})
    return {**formatted, 'messages': messages}


def _usage(raw):
    _require(type(raw) is dict, 'invalid_usage', rule='usage_object', path='$.usage')
    incoming, outgoing, total = (raw.get(key) for key in ('inputTokens', 'outputTokens', 'totalTokens'))
    for key, value in zip(('inputTokens', 'outputTokens', 'totalTokens'), (incoming, outgoing, total)):
        _require(type(value) is int and 0 <= value <= 2147483647,
                 'invalid_usage', rule='usage_count', path='$.usage.' + key)
    cached, writes = (raw.get(key) for key in ('cacheReadInputTokens', 'cacheWriteInputTokens'))
    for key, value in zip(('cacheReadInputTokens', 'cacheWriteInputTokens'), (cached, writes)):
        _require(value is None or type(value) is int and 0 <= value <= 2147483647,
                 'invalid_usage', rule='usage_count', path='$.usage.' + key)
    # Converse reports ordinary input separately from cache reads and writes.
    # Our shared Usage includes all three disjoint input categories.
    return Usage(incoming + (cached or 0) + (writes or 0), outgoing,
                 cached_input_tokens=cached, cache_write_tokens=writes)


def _text_at(value, path, *, empty=True):
    try:
        return _text(value, empty=empty)
    except ProviderAccessError as error:
        error.provider_diagnostics = {'version': 1, 'validation_rule': 'text_value',
                                      'validation_path': path}
        raise


def decode(raw, api_mode='converse', *, selected_llm, endpoint='runtime'):
    selection(selected_llm, api_mode, endpoint=endpoint)
    try:
        try:
            _bounded(raw)
        except ProviderAccessError as error:
            error.provider_diagnostics = {'version': 1, 'validation_rule': 'response_object',
                                          'validation_path': '$'}
            raise
        _fields(raw, {'output', 'stopReason', 'usage', 'metrics', 'ResponseMetadata',
                      'serviceTier', 'performanceConfig'})
        output = raw.get('output')
        _require(type(output) is dict and set(output) == {'message'},
                 rule='output_message', path='$.output')
        message = output['message']
        _fields(message, {'role', 'content'}, path='$.output.message')
        _require(message.get('role') == 'assistant' and type(message.get('content')) is list
                 and len(message['content']) <= 64, rule='assistant_message', path='$.output.message')
        text, calls, seen = [], [], set()
        has_reasoning = False
        opaque_total = 0
        display = []
        for block in message['content']:
            _require(type(block) is dict and len(block) == 1,
                     rule='content_block', path='$.output.message.content[]')
            if 'text' in block:
                text.append(_text_at(block['text'], '$.output.message.content[].text'))
            elif 'reasoningContent' in block:
                # Display is separate from the exact signed replay carrier.
                path = '$.output.message.content[].reasoningContent'
                reasoning = block['reasoningContent']
                _require(type(reasoning) is dict and set(reasoning) in ({'reasoningText'}, {'redactedContent'}),
                         rule='reasoning_content', path=path)
                if 'redactedContent' in reasoning:
                    from .converse_state import binary, MAX_OPAQUE_BYTES
                    try:
                        opaque_total += len(binary(reasoning['redactedContent']))
                        _require(opaque_total <= MAX_OPAQUE_BYTES, rule='reasoning_content', path=path)
                    except ContractError:
                        _require(False, rule='reasoning_content', path=path)
                    has_reasoning = True
                    continue
                value = reasoning['reasoningText']
                _require(type(value) is dict and set(value) <= {'text', 'signature'}
                         and bool(value) and ('text' not in value or type(value['text']) is str)
                         and ('signature' not in value or type(value['signature']) is str and bool(value['signature'])),
                         rule='reasoning_content', path=path + '.reasoningText')
                has_reasoning = True
                display.append(_text_at(value.get('text', ''), path + '.reasoningText.text'))
            else:
                _require('toolUse' in block, rule='content_block', path='$.output.message.content[]')
                tool = block['toolUse']
                _fields(tool, {'toolUseId', 'name', 'input', 'type'},
                        path='$.output.message.content[].toolUse')
                identifier = _text_at(tool.get('toolUseId'),
                                      '$.output.message.content[].toolUse.toolUseId', empty=False)
                _require(identifier not in seen, rule='tool_identity',
                         path='$.output.message.content[].toolUse.toolUseId')
                _require(type(tool.get('input')) is dict, rule='tool_input',
                         path='$.output.message.content[].toolUse.input')
                seen.add(identifier)
                _require(tool.get('type') in (None, 'function', 'tool_use'), rule='tool_type',
                         path='$.output.message.content[].toolUse.type')
                name = _text_at(tool.get('name'), '$.output.message.content[].toolUse.name', empty=False)
                arguments = json.dumps(tool['input'], allow_nan=False)
                calls.append(ToolCall(identifier, name, arguments))
        reason = raw.get('stopReason')
        outcomes = {'end_turn': 'completed', 'stop_sequence': 'completed',
                    'tool_use': 'tool_calls', 'max_tokens': 'length',
                    'model_context_window_exceeded': 'length',
                    'content_filtered': 'refused', 'guardrail_intervened': 'refused'}
        _require(type(reason) is str and reason in outcomes, 'unsupported_finish_reason',
                 rule='finish_reason', path='$.stopReason')
        _require((reason == 'tool_use') == bool(calls), 'tool_finish_mismatch',
                 rule='tool_finish_consistency', path='$.stopReason')
        _require(not has_reasoning or (reason in ('end_turn', 'stop_sequence', 'tool_use')
                 and (calls or any(part.strip() for part in text))),
                 rule='unsupported_continuation',
                 path='$.output.message.content[].reasoningContent')
        tier = raw.get('serviceTier')
        if tier is not None:
            _require(type(tier) is dict and set(tier) == {'type'},
                     rule='service_tier', path='$.serviceTier')
            tier = _text_at(tier['type'], '$.serviceTier.type', empty=False)
        state = json.dumps({'provider':'bedrock.converse','state':[message]}, allow_nan=False, separators=(',', ':')) if has_reasoning else None
        return LLMResult(Message('assistant', (TextContent(_text_at(''.join(text), '$.output.message.content[]')),), tuple(calls), continuation=state),
                         _usage(raw.get('usage')), outcomes[reason], service_tier=tier,
                         reasoning_summary='\n'.join(display) or None)
    except ProviderAccessError as error:
        error.execution = 'possibly_executed'
        if type(raw) is dict and type(raw.get('usage')) is dict:
            try:
                error.usage = _usage(raw['usage'])
            except ProviderAccessError:
                pass
        raise
