"""Bounded SDK observations, never raw HTTP bodies or synthesized reasoning."""
import json
import re

_COUNTS = {'output_items', 'message_items', 'reasoning_items', 'tool_items', 'other_items',
           'visible_text_characters', 'reasoning_text_characters', 'decoded_visible_text_characters',
           'max_output_tokens', 'thinking_budget_tokens', 'scrub_empty_strings', 'scrub_omitted_members', 'scrub_omitted_events', 'stream_sequence'}
_ENUMS = {
    'observation': {'sdk_decoded'},
    'stream_event': {'other', 'eof', 'response.created', 'response.in_progress',
        'response.completed', 'response.incomplete', 'response.output_item.added',
        'response.output_item.done', 'response.function_call_arguments.delta',
        'response.function_call_arguments.done', 'response.content_part.added',
        'response.content_part.done', 'response.output_text.delta', 'response.output_text.done',
        'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done',
        'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
        'response.reasoning_text.delta', 'response.reasoning_text.done'},
    'thinking_mode': {'enabled', 'disabled', 'adaptive'},
    'reasoning_control': {'native_effort', 'budget', 'native_effort_and_budget'},
    'api': {'responses', 'completions', 'converse', 'normalized'},
    'response_status': {'completed', 'incomplete', 'failed', 'cancelled', 'in_progress', 'queued'},
    'incomplete_reason': {'max_output_tokens', 'content_filter', 'other'},
    'reasoning_effort': {'none', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'},
    'response_validation': {'rejected'},
    'validation_rule': {'response_object', 'selected_model', 'response_choices', 'choice_index',
                        'assistant_role', 'message_annotations', 'allowed_fields',
                        'response_status', 'response_output', 'finish_reason', 'tool_finish_consistency',
                        'usage_object', 'usage_count', 'text_value', 'output_message',
                        'assistant_message', 'content_block', 'tool_identity', 'tool_input',
                        'tool_type', 'service_tier', 'reasoning_content',
                        'unsupported_continuation', 'stream_state', 'stream_metadata', 'reasoning_aliases', 'summary_size'},
    'validation_path': {'$', '$.model', '$.service_tier', '$.serving_revision', '$.choices', '$.choices[].index',
                        '$.choices[].message', '$.choices[].message.role', '$.choices[].delta',
                        '$.choices[].message.annotations', '$.choices[].message.tool_calls[]',
                        '$.choices[].message.tool_calls[].function', '$.choices[].finish_reason',
                        '$.status', '$.output', '$.output[]', '$.output[].summary', '$.stopReason', '$.usage',
                        '$.usage.inputTokens', '$.usage.outputTokens', '$.usage.totalTokens',
                        '$.usage.cacheReadInputTokens', '$.usage.cacheWriteInputTokens',
                        '$.output.message', '$.output.message.content[]',
                        '$.output.message.content[].text', '$.output.message.content[].toolUse',
                        '$.output.message.content[].reasoningContent',
                        '$.output.message.content[].reasoningContent.reasoningText',
                        '$.output.message.content[].toolUse.toolUseId',
                        '$.output.message.content[].toolUse.name',
                        '$.output.message.content[].toolUse.input',
                        '$.output.message.content[].toolUse.type', '$.serviceTier',
                        '$.serviceTier.type'},
}
_STOPS = {'stop', 'length', 'tool_calls', 'function_call', 'content_filter', 'other',
          'end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'guardrail_intervened', 'content_filtered'}
_IDENTIFIERS = {'request_id', 'response_id', 'validation_location'}
_BOOLEANS = {'error_present', 'encrypted_reasoning_present'}
_VERSION_KEYS = {'openai', 'httpx'}
_CONTENT_KEYS = {'values', 'prompt', 'messages', 'response', 'provider_payload', 'provider_response',
                 'reasoning', 'reasoning_content', 'thinking_content', 'chain_of_thought',
                 'text', 'input', 'output', 'request', 'response_text', 'message'}


def has_optional_content(payload):
    return type(payload) is dict and any(type(key) is str and key in _CONTENT_KEYS for key in payload)


def valid_diagnostics(value):
    """Admit only the versioned metadata vocabulary, including at IPC boundaries."""
    if type(value) is not dict or value.get('version') != 1 or type(value.get('version')) is not int or len(value) > 32:
        return False
    for key, item in value.items():
        if key == 'version':
            continue
        if key in _COUNTS:
            good = type(item) is int and 0 <= item <= (1 << 53) - 1
        elif key in _ENUMS:
            good = type(item) is str and item in _ENUMS[key]
        elif key in _IDENTIFIERS:
            good = type(item) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', item) is not None
        elif key in _BOOLEANS:
            good = type(item) is bool
        elif key == 'http_status':
            good = type(item) is int and 100 <= item <= 599
        elif key == 'finish_reasons':
            good = type(item) is list and len(item) <= 8 and all(type(v) is str and v in _STOPS for v in item)
        elif key == 'sdk_versions':
            good = (type(item) is dict and set(item) <= _VERSION_KEYS and all(type(v) is str and
                    re.fullmatch(r'[0-9][A-Za-z0-9.+-]{0,47}', v) is not None for v in item.values()))
        else:
            good = False
        if not good:
            return False
    return True


def scrub_diagnostics(value, boundary):
    """Metadata is still subject to the live credential authority."""
    if not valid_diagnostics(value):
        return {}
    clean = boundary.content_export(value)
    # A redacted identifier is not a valid identifier. Omit that field while
    # retaining independent observations; never relabel the redaction as an ID.
    for key in _IDENTIFIERS & clean.keys():
        if not valid_diagnostics({'version': 1, key: clean[key]}):
            clean.pop(key)
    return clean if valid_diagnostics(clean) else {}


def omission_notice(flag):
    if flag not in ('LOG_CONTENT', 'TRACELOOP_TRACE_CONTENT'):
        raise ValueError('Invalid content destination')
    return (f'Optional content omitted. Rerun with {flag}=True to capture provider-returned text, '
            'including reasoning when supplied. Credentials, encrypted reasoning and internal state remain excluded. '
            'This cannot recover content from earlier runs.')


def sdk_observation(wire, phase):
    """Count admitted SDK fields before content filtering; no generated text retained."""
    if type(wire) is not dict:
        return {}
    wrapper = 'provider_response' if phase == 'response' else 'provider_payload'
    if wrapper in wire:
        raw = wire[wrapper]
    else:
        markers = ('choices', 'output') if phase == 'response' else ('messages', 'input', 'inferenceConfig')
        if not any(type(wire.get(key)) in (list, dict) for key in markers):
            return {}
        raw = wire
    if type(raw) is not dict:
        return {}
    data = {'version': 1, 'observation': 'sdk_decoded'}
    if phase == 'request':
        additional = raw.get('additionalModelRequestFields')
        if type(additional) is dict:
            raw = {**raw, **additional}
            thinking = additional.get('thinking')
            if type(thinking) is dict:
                if type(thinking.get('type')) is str and thinking['type'] in _ENUMS['thinking_mode']:
                    data['thinking_mode'] = thinking['type']
                budget = thinking.get('budget_tokens')
                if type(budget) is int and 0 <= budget <= 1048576:
                    data['thinking_budget_tokens'] = budget
            output = additional.get('output_config')
            if type(output) is dict:
                raw = {**raw, 'reasoning_effort': output.get('effort')}
        control = raw.get('reasoning')
        effort = control.get('effort') if type(control) is dict else raw.get('reasoning_effort')
        if type(effort) is str and effort in _ENUMS['reasoning_effort']:
            data['reasoning_effort'] = effort
            if type(additional) is dict:
                data['reasoning_control'] = 'native_effort_and_budget' if 'thinking_budget_tokens' in data else 'native_effort'
        elif 'thinking_budget_tokens' in data:
            data['reasoning_control'] = 'budget'
        inference = raw.get('inferenceConfig')
        native_maximum = inference.get('maxTokens') if type(inference) is dict else None
        maximum = raw.get('max_output_tokens', raw.get('max_completion_tokens', raw.get('max_tokens', native_maximum)))
        if type(maximum) is int and 0 <= maximum <= (1 << 53) - 1:
            data['max_output_tokens'] = maximum
        return data
    data.update(visible_text_characters=0, reasoning_text_characters=0,
                encrypted_reasoning_present=False, error_present=raw.get('error') is not None)
    status = raw.get('status')
    if type(status) is str and status in _ENUMS['response_status']:
        data['response_status'] = status
    detail = raw.get('incomplete_details')
    if type(detail) is dict and detail.get('reason') is not None:
        reason = detail['reason']
        data['incomplete_reason'] = reason if type(reason) is str and reason in _ENUMS['incomplete_reason'] else 'other'
    identifier = raw.get('id')
    if type(identifier) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', identifier):
        data['response_id'] = identifier
    output = raw.get('output')
    if type(output) is list:
        data['api'] = 'responses'
        data.update(output_items=len(output), message_items=0, reasoning_items=0, tool_items=0, other_items=0)
        for item in output:
            if type(item) is not dict:
                data['other_items'] += 1
                continue
            kind = item.get('type')
            field = {'message': 'message_items', 'reasoning': 'reasoning_items', 'function_call': 'tool_items'}.get(kind) if type(kind) is str else None
            data[field or 'other_items'] += 1
            _text_counts(item, data, reasoning=kind == 'reasoning')
    elif type(output) is dict and type(output.get('message')) is dict:
        data['api'] = 'converse'
        if type(raw.get('stopReason')) is str and raw['stopReason'] in _STOPS:
            data['finish_reasons'] = [raw['stopReason']]
        blocks = output['message'].get('content')
        if type(blocks) is list:
            data.update(output_items=len(blocks), message_items=0, reasoning_items=0,
                        tool_items=0, other_items=0)
            for block in blocks:
                if type(block) is not dict or len(block) != 1:
                    data['other_items'] += 1
                elif 'text' in block:
                    data['message_items'] += 1
                    if type(block['text']) is str:
                        data['visible_text_characters'] += len(block['text'])
                elif 'toolUse' in block:
                    data['tool_items'] += 1
                elif 'reasoningContent' in block:
                    data['reasoning_items'] += 1
                    reasoning = block['reasoningContent']
                    if type(reasoning) is dict and type(reasoning.get('reasoningText')) is dict:
                        value = reasoning['reasoningText'].get('text')
                        if type(value) is str:
                            data['reasoning_text_characters'] += len(value)
                else:
                    data['other_items'] += 1
    choices = raw.get('choices')
    if type(choices) is list:
        data['api'] = 'completions'
        data['finish_reasons'] = []
        for item in choices[:8]:
            if type(item) is not dict:
                continue
            stop = item.get('finish_reason')
            if stop is not None:
                data['finish_reasons'].append(stop if type(stop) is str and stop in _STOPS else 'other')
            message = item.get('message')
            if type(message) is dict:
                _text_counts(message, data)
    return data


def _text_counts(item, data, *, reasoning=False, depth=0):
    if depth > 8:
        return
    if type(item) is dict:
        kind = item.get('type')
        reasoning = (reasoning or kind in ('reasoning', 'reasoning_text', 'summary_text', 'thinking')
                     or item.get('channel') in ('analysis', 'reasoning'))
        for key, child in item.items():
            if key in ('encrypted_content', 'encrypted_reasoning', 'redacted_thinking'):
                data['encrypted_reasoning_present'] = True
            elif key in ('reasoning', 'reasoning_content', 'thinking_content', 'chain_of_thought') and type(child) is str:
                data['reasoning_text_characters'] += len(child)
            elif key in ('text', 'content', 'thinking') and type(child) is str:
                field = 'reasoning_text_characters' if reasoning else 'visible_text_characters'
                data[field] += len(child)
            elif key in ('content', 'summary') and type(child) is list:
                for part in child:
                    _text_counts(part, data, reasoning=reasoning, depth=depth + 1)


def diagnostic_attachment(row, boundary, *, enabled):
    """Standard Allure attachment independent of DEBUG/content permission."""
    data = scrub_diagnostics(row.get('diagnostics', {}), boundary)
    # Counts describe observed response content, never a request's capability.
    # Keep the same response field for unknown, empty and nonempty observations.
    if row['phase'] == 'response':
        data.setdefault('reasoning_text_characters', None)
    else:
        data.pop('reasoning_text_characters', None)
    data['capture_reason'] = row.get('reason', 'captured')
    reason = data['capture_reason']
    notice = omission_notice('LOG_CONTENT') if not enabled and reason != 'unavailable' else None
    if notice is not None:
        data['content_notice'] = notice
    if reason in ('truncated', 'unavailable', 'partial'):
        data['capture_notice'] = {
            'truncated': 'Content capture exceeded its bounds; enabling a content flag cannot restore omitted bytes.',
            'unavailable': 'Content capture was unavailable; enabling content flags cannot recover it.',
            'partial': 'Only a partial provider payload was captured; it is not a complete response.',
        }[reason]
    elif reason == 'withheld' and enabled:
        data['content_notice'] = 'Content withheld because its credential authority could not be established; content flags cannot override this safety boundary.'
    data['call_id'] = row['call_id']
    if row.get('transport_attempt_id') is not None:
        data['transport_attempt_id'] = row['transport_attempt_id']
    attempt = row.get('transport_attempt_id')
    return (f"LLM {row['phase']} diagnostics ({row['call_id']}{', attempt ' + attempt if attempt else ''})",
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode(), 'application/json')
