"""Provider-owned GPT effort version ranges and known exceptions.

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
Numeric family versions inherit their minimum supported generation.
Other backends, malformed identifiers and unrecognized families remain unknown.
"""
from api_agent.providers.contracts import ProviderAccessError
from .endpoint import OFFICIAL_BASE_URL

# Provider-owned minimum versions; later generations inherit the last known
# choices. Explicit later changes are boundaries, not growing alias lists.
from packaging.version import Version
from .model_versions import gpt_version

_COMMON = ('off', 'low', 'medium', 'high')
_REASONING_ONLY = ('low', 'medium', 'high')
_FAMILY_RANGES = {
    'luna': ((Version('5.6'), _COMMON),),
    'terra': ((Version('5.6'), _COMMON),),
    'sol': ((Version('5.6'), _COMMON), (Version('6.1'), _REASONING_ONLY)),
    'astra': ((Version('6'), _REASONING_ONLY),),
}
_CATALOG_MISSING = object()


def _catalog_choices(metadata, defaults):
    if type(metadata) is not dict:
        return None
    efforts = metadata.get('supported_efforts')
    mandatory = metadata.get('mandatory', False)
    if (type(efforts) is not list or len(efforts) > 32 or type(mandatory) is not bool
            or any(type(item) is not str or not item or len(item) > 32 for item in efforts)
            or len(set(efforts)) != len(efforts)):
        return None
    return tuple(item for item in defaults
                 if ('none' if item == 'off' else item) in efforts
                 and (item != 'off' or not mandatory))


# These Chat-with-tools route limits are independently documented/retained;
# adding a model's effort choices must not introduce a new tool restriction.
_CHAT_TOOLS_RESPONSES_ONLY = ('gpt-6-astra', 'gpt-6.1-sol')
_CHAT_TOOLS_OFF_ONLY = ('gpt-6-luna', 'gpt-6-sol', 'gpt-5.6-luna')

# Display summaries require their own route qualification, independent of effort.
_DISPLAY_SUMMARIES = frozenset({'gpt-6-luna'})


def display_reasoning(selection, *, base_url, api_mode):
    return api_mode == 'responses' and (
        base_url == OFFICIAL_BASE_URL and selection in _DISPLAY_SUMMARIES
        or base_url == 'https://openrouter.ai/api/v1' and selection in ('openai/gpt-oss-20b', 'openai/gpt-oss-120b'))

def reasoning_efforts(selection, *, base_url, metadata=_CATALOG_MISSING):
    """Carry minimum-version support forward, honoring narrower catalog facts."""
    if type(selection) is not str:
        return None
    if base_url == OFFICIAL_BASE_URL:
        model = selection
    elif base_url == 'https://openrouter.ai/api/v1' and selection.startswith('openai/'):
        model = selection[7:]
    else:
        return None
    if base_url == 'https://openrouter.ai/api/v1' and model in ('gpt-oss-20b', 'gpt-oss-120b'):
        defaults = _REASONING_ONLY
    else:
        parsed = gpt_version(model)
        if parsed is None:
            return None
        version, family = parsed
        defaults = next((choices for minimum, choices in reversed(_FAMILY_RANGES.get(family, ()))
                         if version >= minimum), None)
        if defaults is None:
            return None
    if metadata is not _CATALOG_MISSING:
        return _catalog_choices(metadata, defaults)
    return defaults


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
    if generation.display_summary and base_url == OFFICIAL_BASE_URL and display_reasoning(request.selected_llm, base_url=base_url, api_mode=api_mode) and effort != 'off':
        return {'reasoning': {'summary': 'auto', **({'effort': effort} if effort else {})}}
    if not effort:
        return {}
    native = 'none' if effort == 'off' else effort
    if base_url == 'https://openrouter.ai/api/v1':
        return {'reasoning': {'effort': native},
                'provider': {'require_parameters': True, 'allow_fallbacks': False}}
    return {'reasoning': {'effort': native}} if api_mode == 'responses' else {'reasoning_effort': native}
