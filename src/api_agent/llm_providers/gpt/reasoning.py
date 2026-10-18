"""Exact public model effort contracts; unfamiliar routes remain unknown.

Contracts: https://developers.openai.com/api/docs/guides/reasoning
and https://developers.openai.com/api/docs/guides/deployment-checklist.
OpenRouter contracts: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
and https://openrouter.ai/api/v1/models (verified 2026-10-15).
Luna 5.6 official contract: https://developers.openai.com/api/docs/models/gpt-5.6-luna
(verified 2026-10-18; no dated snapshot listed).
Terra and Sol 5.6 official contracts:
https://developers.openai.com/api/docs/models/gpt-5.6-terra
https://developers.openai.com/api/docs/models/gpt-5.6-sol
(verified 2026-10-18; no dated snapshots listed).
Only published aliases are listed: a date-shaped suffix is not evidence of support.
"""
from api_agent.providers.contracts import ProviderAccessError
from .endpoint import OFFICIAL_BASE_URL

_EFFORTS = {
    'gpt-6-luna': ('off', 'low', 'medium', 'high'),
    'gpt-6-sol': ('off', 'low', 'medium', 'high'),
    'gpt-6-astra': ('low', 'medium', 'high'),
    'gpt-6.1-sol': ('low', 'medium', 'high'),
}


# Official verification does not establish support on another route.
_OFFICIAL_EFFORTS = {**_EFFORTS, **{
    model: ('off', 'low', 'medium', 'high')
    for model in ('gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol')
}}

# These Chat-with-tools route limits are independently documented/retained;
# adding a model's effort choices must not introduce a new tool restriction.
_CHAT_TOOLS_RESPONSES_ONLY = ('gpt-6-astra', 'gpt-6.1-sol')
_CHAT_TOOLS_OFF_ONLY = ('gpt-6-luna', 'gpt-6-sol', 'gpt-5.6-luna')

def reasoning_efforts(selection, *, base_url):
    """Return verified common choices without authentication or discovery I/O."""
    if base_url == OFFICIAL_BASE_URL:
        return _OFFICIAL_EFFORTS.get(selection)
    if base_url == 'https://openrouter.ai/api/v1':
        return {'openai/' + model: choices for model, choices in _EFFORTS.items()}.get(selection)
    return None


def reasoning_fields(request, *, base_url, api_mode):
    """Reject unsupported controls before transport; omission preserves defaults."""
    generation = request.payload.generation
    effort = generation.reasoning_effort
    choices = reasoning_efforts(request.selected_llm, base_url=base_url)
    if effort and (choices is None or effort not in choices):
        raise ProviderAccessError('unsupported', 'Reasoning effort is not verified for this GPT model and route; choose a supported effort or provider default.')
    if effort and generation.temperature is not None:
        raise ProviderAccessError('unsupported', 'GPT inference does not support temperature controls.')
    if base_url == OFFICIAL_BASE_URL and api_mode == 'completions' and request.payload.tools:
        if (request.selected_llm in _CHAT_TOOLS_RESPONSES_ONLY
                or (request.selected_llm in _CHAT_TOOLS_OFF_ONLY and effort != 'off')):
            raise ProviderAccessError('unsupported', 'This GPT model requires Responses for reasoning with tools; select responses mode or a documented disabled-reasoning tool route.')
    if not effort:
        return {}
    native = 'none' if effort == 'off' else effort
    if base_url == 'https://openrouter.ai/api/v1':
        return {'reasoning': {'effort': native},
                'provider': {'require_parameters': True, 'allow_fallbacks': False}}
    return {'reasoning': {'effort': native}} if api_mode == 'responses' else {'reasoning_effort': native}
