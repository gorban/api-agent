"""Bounded SDK observations, never raw HTTP bodies or synthesized reasoning."""
import json
import re

_COUNTS = {'output_items', 'message_items', 'reasoning_items', 'tool_items', 'other_items',
           'visible_text_characters', 'reasoning_text_characters', 'decoded_visible_text_characters',
           'max_output_tokens'}
_ENUMS = {
    'observation': {'sdk_decoded'},
    'api': {'responses', 'completions', 'normalized'},
    'response_status': {'completed', 'incomplete', 'failed', 'cancelled', 'in_progress', 'queued'},
    'incomplete_reason': {'max_output_tokens', 'content_filter', 'other'},
    'reasoning_effort': {'none', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'},
    'response_validation': {'rejected'},
    'validation_rule': {'response_object', 'selected_model', 'response_choices', 'choice_index',
                        'assistant_role', 'message_annotations', 'allowed_fields',
                        'response_status', 'response_output', 'finish_reason', 'tool_finish_consistency'},
    'validation_path': {'$', '$.model', '$.choices', '$.choices[].index',
                        '$.choices[].message', '$.choices[].message.role',
                        '$.choices[].message.annotations', '$.choices[].message.tool_calls[]',
                        '$.choices[].message.tool_calls[].function', '$.choices[].finish_reason',
                        '$.status', '$.output', '$.output[]'},
}
_STOPS = {'stop', 'length', 'tool_calls', 'function_call', 'content_filter', 'other'}
_IDENTIFIERS = {'request_id', 'response_id'}
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
    raw = wire.get('provider_response') if phase == 'response' else wire.get('provider_payload')
    if type(raw) is not dict:
        return {}
    data = {'version': 1, 'observation': 'sdk_decoded'}
    if phase == 'request':
        control = raw.get('reasoning')
        effort = control.get('effort') if type(control) is dict else raw.get('reasoning_effort')
        if type(effort) is str and effort in _ENUMS['reasoning_effort']:
            data['reasoning_effort'] = effort
        maximum = raw.get('max_output_tokens', raw.get('max_completion_tokens', raw.get('max_tokens')))
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
