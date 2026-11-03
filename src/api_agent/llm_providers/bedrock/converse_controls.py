"""Provider-owned Converse effort qualification and thinking-budget mapping.

AWS contracts: model-parameters-openai.html, claude-messages-adaptive-thinking.html
and claude-messages-extended-thinking.html in the public Bedrock user guide.
"""
from api_agent.providers.contracts import ProviderAccessError

PRESETS = {'low': 1024, 'medium': 4096, 'high': 16384}
ANSWER_HEADROOM = 1024
_ADAPTIVE = frozenset({
    'anthropic.claude-opus-4-8', 'anthropic.claude-opus-4-7',
    'anthropic.claude-opus-4-6-v1', 'anthropic.claude-sonnet-4-6',
    'anthropic.claude-opus-5-5', 'anthropic.claude-sonnet-5-5',
    'anthropic.claude-haiku-5-5',
})
_BUDGET = frozenset({
    'anthropic.claude-opus-4-1-20250805-v1:0',
    'anthropic.claude-opus-4-5-20251101-v1:0',
    'anthropic.claude-haiku-4-5-20251001-v1:0',
    'anthropic.claude-sonnet-4-5-20250929-v1:0',
})


def kind(identifier):
    from .catalog import _RUNTIME, _RUNTIME_OPENAI
    if identifier not in _RUNTIME:
        return None
    base = identifier.split('.', 1)[1] if not identifier.startswith(('anthropic.', 'openai.')) else identifier
    if base in _RUNTIME_OPENAI:
        return 'gpt'
    if base in ('openai.gpt-oss-20b-1:0', 'openai.gpt-oss-120b-1:0'):
        return 'oss'
    if base == 'anthropic.claude-opus-4-5-20251101-v1:0':
        return 'hybrid'
    if base in _ADAPTIVE:
        return 'adaptive'
    return 'budget' if base in _BUDGET else None


def reasoning_efforts(identifier):
    family = kind(identifier)
    if family is None:
        return None
    base = identifier.split('.', 1)[1] if not identifier.startswith(('anthropic.', 'openai.')) else identifier
    disabled = base in _BUDGET or base in {
        'openai.gpt-6-luna', 'openai.gpt-6-astra',
        'anthropic.claude-opus-4-7', 'anthropic.claude-opus-4-8',
        'anthropic.claude-haiku-5-5'}
    return (('off',) if disabled else ()) + tuple(PRESETS)


def effective_request(request, config=None):
    """Use the configured shared ceiling when an effort request omits one."""
    if request.payload.generation.reasoning_effort and request.payload.generation.max_output_tokens is None:
        from dataclasses import replace
        from .catalog import selection
        configured = (config or {}).get('LLM_MAX_OUTPUT_TOKENS', 200000)
        if type(configured) is not int or not 0 < configured <= 1048576:
            raise ProviderAccessError('invalid_request', 'Invalid configured output token ceiling.')
        maximum = selection(request.selected_llm, 'converse', 'runtime').max_output_tokens
        request = replace(request, payload=replace(request.payload,
            generation=replace(request.payload.generation,
                max_output_tokens=min(configured, maximum or configured))))
    return request


def fields(request, config=None):
    """Resolve request-local controls after the effective output ceiling is set."""
    generation = request.payload.generation
    family = kind(request.selected_llm)
    effort = generation.reasoning_effort
    result = {}
    if not effort:
        if generation.display_summary and family == 'gpt':
            return {'reasoning': {'summary': 'auto'}}
        if generation.display_summary and family == 'adaptive':
            return {'thinking': {'type': 'adaptive', 'display': 'summarized'}}
        return result
    if family is None or effort not in (reasoning_efforts(request.selected_llm) or ()):
        raise ProviderAccessError('unsupported', 'This Converse selection has no admitted reasoning control.')
    if effort == 'off' and family not in ('gpt', 'oss'):
        return {'thinking': {'type': 'disabled'}}
    if family in ('gpt', 'oss'):
        if family == 'gpt':
            result['reasoning'] = {'effort': 'none' if effort == 'off' else effort}
            if generation.display_summary and effort != 'off':
                result['reasoning']['summary'] = 'auto'
        else:
            result['reasoning_effort'] = effort
    else:
        if generation.temperature is not None:
            raise ProviderAccessError('invalid_request', 'Claude thinking cannot be combined with temperature.')
        if family == 'adaptive':
            result = {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': effort}}
            if generation.display_summary:
                result['thinking']['display'] = 'summarized'
        else:
            values = [(config or {}).get('LLM_REASONING_' + name.upper() + '_TOKENS', default)
                      for name, default in PRESETS.items()]
            if (any(type(value) is not int or not 1024 <= value <= 1048576 for value in values)
                    or values != sorted(values)):
                raise ProviderAccessError('invalid_request', 'Converse thinking budgets must be monotonic integers from 1024 to 1048576.')
            ceiling = generation.max_output_tokens or 1024
            budget = min(dict(zip(PRESETS, values))[effort], ceiling - ANSWER_HEADROOM)
            if budget < 1024:
                raise ProviderAccessError('invalid_request', 'Converse thinking requires at least 2048 total output tokens, including 1024 answer tokens.')
            result = {'thinking': {'type': 'enabled', 'budget_tokens': budget}}
            if family == 'hybrid':
                result['output_config'] = {'effort': effort}
                result['anthropic_beta'] = ['effort-2025-11-24']
    return result


def validate_fields(body, selected):
    """Validate the independently received worker controls before SDK dispatch."""
    value = body.get('additionalModelRequestFields', {})
    if type(value) is not dict:
        raise ValueError('Invalid Converse controls')
    family = kind(selected.selected_llm)
    remaining = dict(value)
    if ('temperature' not in selected.features and 'temperature' in body.get('inferenceConfig', {})):
        raise ValueError('Converse selection does not support temperature')
    if family == 'gpt' and 'outputConfig' in body and remaining.get('text') != {'format': {'strict': True}}:
        raise ValueError('Converse GPT JSON schema requires strict format')
    if 'text' in remaining:
        if family != 'gpt' or 'outputConfig' not in body or remaining.pop('text') != {'format': {'strict': True}}:
            raise ValueError('Invalid Converse strict format')
    if not remaining:
        return
    if remaining == {'thinking': {'type': 'disabled'}}:
        if 'off' not in (reasoning_efforts(selected.selected_llm) or ()) or family in ('gpt', 'oss'):
            raise ValueError('Unqualified Converse thinking disable')
        return
    if family == 'gpt':
        reasoning = remaining.get('reasoning')
        if (set(remaining) != {'reasoning'} or type(reasoning) is not dict or not reasoning
                or set(reasoning) - {'effort', 'summary'}
                or 'effort' in reasoning and reasoning['effort'] not in tuple(PRESETS) + (('none',) if 'off' in (reasoning_efforts(selected.selected_llm) or ()) else ())
                or 'summary' in reasoning and reasoning['summary'] != 'auto'):
            raise ValueError('Invalid Converse reasoning effort')
    elif family == 'oss':
        if set(remaining) != {'reasoning_effort'} or remaining['reasoning_effort'] not in PRESETS:
            raise ValueError('Invalid Converse reasoning effort')
    elif family == 'adaptive':
        thinking = remaining.get('thinking')
        if (set(remaining) not in ({'thinking'}, {'thinking', 'output_config'})
                or thinking not in ({'type': 'adaptive'}, {'type': 'adaptive', 'display': 'summarized'})
                or 'output_config' in remaining and (type(remaining['output_config']) is not dict
                    or set(remaining['output_config']) != {'effort'}
                    or remaining['output_config']['effort'] not in PRESETS)):
            raise ValueError('Invalid Converse adaptive effort')
    elif family in ('budget', 'hybrid'):
        thinking = remaining.get('thinking')
        ceiling = body.get('inferenceConfig', {}).get('maxTokens', 1024)
        expected = {'thinking', 'output_config', 'anthropic_beta'} if family == 'hybrid' else {'thinking'}
        if (set(remaining) != expected or type(thinking) is not dict
                or set(thinking) != {'type', 'budget_tokens'} or thinking['type'] != 'enabled'
                or type(ceiling) is not int or type(thinking['budget_tokens']) is not int
                or not 1024 <= thinking['budget_tokens'] <= ceiling - ANSWER_HEADROOM):
            raise ValueError('Invalid Converse thinking budget')
        if family == 'hybrid' and (remaining['anthropic_beta'] != ['effort-2025-11-24']
                or type(remaining['output_config']) is not dict
                or set(remaining['output_config']) != {'effort'}
                or remaining['output_config']['effort'] not in PRESETS):
            raise ValueError('Invalid Converse output effort')
    else:
        raise ValueError('Unqualified Converse reasoning controls')
    if family in ('adaptive', 'budget', 'hybrid') and 'temperature' in body.get('inferenceConfig', {}):
        raise ValueError('Claude thinking cannot be combined with temperature')
