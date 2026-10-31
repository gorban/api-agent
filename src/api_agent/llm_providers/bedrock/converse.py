"""Text and client-tool codecs for the public Bedrock Runtime Converse API.

https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
The SDK owns authentication and transport; this module has no side effects.
"""
import json

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import LLMRequest, LLMResult, Message, TextContent, ToolCall, Usage
from api_agent.providers.rich_inference import request_to_wire, schema
from .catalog import selection
from .mantle import _bounded, _fields, _require, _text


def validate_request(request, *, endpoint='runtime', api_mode='converse'):
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock Converse request.')
    selected = selection(request.selected_llm, api_mode, endpoint=endpoint)
    selected.validate(request)
    payload = request.payload
    if any(tool.strict for tool in payload.tools) and 'structured' not in selected.features:
        raise ProviderAccessError('unsupported', 'This Bedrock Converse model does not support strict tools.')
    if (payload.structured is not None and not payload.structured.strict
            or payload.generation.cache_policy is not None
            or payload.generation.reasoning_effort not in (None, '')
            or any(message.cache_boundary is not None or message.continuation is not None
                   for message in payload.messages)):
        raise ProviderAccessError('unsupported', 'Bedrock Converse request feature is unsupported.')
    if payload.generation.temperature is not None and payload.generation.temperature > 1:
        raise ProviderAccessError('invalid_request', 'Bedrock Converse temperature must be at most 1.')
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


def encode(request, api_mode='converse', *, endpoint='runtime'):
    validate_request(request, endpoint=endpoint, api_mode=api_mode)
    payload = request.payload
    system, messages = [], []
    for message in payload.messages:
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
    return body


def format_payload(request, body, evidence):
    from api_agent.llm_providers.response_policy import format_payload as format_shared
    formatted = format_shared(request, body, evidence)
    if formatted is body:
        return body
    # The shared fallback appends one instruction. Translate its content to the
    # Converse union without changing the authored history or evidence position.
    messages = [*formatted['messages']]
    instruction = messages[-1]
    messages[-1] = {**instruction, 'content': [{'text': instruction['content']}]}
    return {**formatted, 'messages': messages}


def _usage(raw):
    _require(type(raw) is dict, 'invalid_usage')
    incoming, outgoing, total = (raw.get(key) for key in ('inputTokens', 'outputTokens', 'totalTokens'))
    _require(all(type(value) is int and 0 <= value <= 2147483647
                 for value in (incoming, outgoing, total)), 'invalid_usage')
    cached, writes = (raw.get(key) for key in ('cacheReadInputTokens', 'cacheWriteInputTokens'))
    _require(all(value is None or type(value) is int and 0 <= value <= 2147483647
                 for value in (cached, writes)), 'invalid_usage')
    # Converse reports ordinary input separately from cache reads and writes.
    # Our shared Usage includes all three disjoint input categories.
    return Usage(incoming + (cached or 0) + (writes or 0), outgoing,
                 cached_input_tokens=cached, cache_write_tokens=writes)


def decode(raw, api_mode='converse', *, selected_llm, endpoint='runtime'):
    selection(selected_llm, api_mode, endpoint=endpoint)
    try:
        _bounded(raw)
        _fields(raw, {'output', 'stopReason', 'usage', 'metrics', 'ResponseMetadata',
                      'serviceTier', 'performanceConfig'})
        output = raw.get('output')
        _require(type(output) is dict and set(output) == {'message'})
        message = output['message']
        _fields(message, {'role', 'content'})
        _require(message.get('role') == 'assistant' and type(message.get('content')) is list
                 and len(message['content']) <= 64)
        text, calls, seen = [], [], set()
        for block in message['content']:
            _require(type(block) is dict and len(block) == 1)
            if 'text' in block:
                text.append(_text(block['text']))
            else:
                _require('toolUse' in block)
                tool = block['toolUse']
                _fields(tool, {'toolUseId', 'name', 'input', 'type'})
                identifier = _text(tool.get('toolUseId'), empty=False)
                _require(identifier not in seen and type(tool.get('input')) is dict)
                seen.add(identifier)
                _require(tool.get('type') in (None, 'function'))
                calls.append(ToolCall(identifier, _text(tool.get('name'), empty=False),
                                      json.dumps(tool['input'], allow_nan=False)))
        reason = raw.get('stopReason')
        outcomes = {'end_turn': 'completed', 'stop_sequence': 'completed',
                    'tool_use': 'tool_calls', 'max_tokens': 'length',
                    'model_context_window_exceeded': 'length',
                    'content_filtered': 'refused', 'guardrail_intervened': 'refused'}
        _require(type(reason) is str and reason in outcomes, 'unsupported_finish_reason')
        _require((reason == 'tool_use') == bool(calls), 'tool_finish_mismatch')
        tier = raw.get('serviceTier')
        if tier is not None:
            _require(type(tier) is dict and set(tier) == {'type'})
            tier = _text(tier['type'], empty=False)
        return LLMResult(Message('assistant', (TextContent(_text(''.join(text))),), tuple(calls)),
                         _usage(raw.get('usage')), outcomes[reason], service_tier=tier)
    except ProviderAccessError as error:
        error.execution = 'possibly_executed'
        if type(raw) is dict and type(raw.get('usage')) is dict:
            try:
                error.usage = _usage(raw['usage'])
            except ProviderAccessError:
                pass
        raise
