"""Verified OpenAI and OpenRouter GPT prompt-cache presets."""
import re
from datetime import date

from api_agent.providers.contracts import ProviderAccessError
from .endpoint import OFFICIAL_BASE_URL, validate_base_url

_EXTENDED = frozenset({
    'gpt-5.5', 'gpt-5.5-pro', 'gpt-5.4', 'gpt-5.2', 'gpt-5.1-codex-max',
    'gpt-5.1', 'gpt-5.1-codex', 'gpt-5.1-codex-mini', 'gpt-5.1-chat-latest',
    'gpt-5', 'gpt-5-codex', 'gpt-4.1',
})
_OPENROUTER_BASE = 'https://openrouter.ai/api/v1'
_OPENROUTER_FAMILIES = frozenset({
    'gpt-5.6-sol', 'gpt-5.6-sol-pro', 'gpt-5.6-terra', 'gpt-5.6-terra-pro',
    'gpt-5.6-luna', 'gpt-5.6-luna-pro',
    'gpt-6-sol', 'gpt-6-sol-pro', 'gpt-6-astra', 'gpt-6-astra-pro',
    'gpt-6-luna', 'gpt-6-luna-pro', 'gpt-6.1-sol', 'gpt-6.1-sol-pro',
})


def prompt_cache_fields(policy, selection, *, base_url, subscription):
    if policy is None or policy == '':
        return {}
    if type(policy) is not str or policy not in ('off', 'medium', 'high'):
        raise ProviderAccessError('invalid_request', 'LLM_CACHE_POLICY must be empty, off, medium or high.')
    if subscription:
        if policy == 'medium':
            # Subscription rejects explicit cache controls. Medium permits its
            # automatic caching without claiming an enforceable lifetime.
            return {}
        requested = 'disable caching' if policy == 'off' else 'request extended cache retention'
        raise ProviderAccessError('unsupported', f'The GPT subscription route cannot {requested}; use medium for automatic caching or an empty policy for provider defaults.')
    base = validate_base_url(base_url)
    if base == _OPENROUTER_BASE:
        family = selection
        snapshot = re.fullmatch(r'(.+)-([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})', selection)
        if snapshot is not None:
            try:
                date.fromisoformat(snapshot[2])
            except ValueError:
                pass
            else:
                family = snapshot[1]
        if not family.startswith('openai/') or family[7:] not in _OPENROUTER_FAMILIES:
            raise ProviderAccessError('unsupported', 'Prompt-cache policy support is unknown for this OpenRouter GPT selection; use an empty policy for provider defaults.')
        if policy == 'off':
            return {'prompt_cache_options': {'mode': 'explicit'}}
        # Enabled presets use the documented explicit contract. The payload
        # renderer marks the final text block to cache the full input prefix.
        return {'prompt_cache_options': {'mode': 'explicit', 'ttl': '30m'}}
    if base != OFFICIAL_BASE_URL:
        raise ProviderAccessError('unsupported', 'LLM_CACHE_POLICY controls are not verified for this compatible backend; use an empty policy for provider defaults.')
    version = re.fullmatch(r'gpt-(\d+)(?:\.(\d+))?(?:-[a-z0-9]+)*', selection)
    if version is not None and (int(version[1]), int(version[2] or 0)) >= (5, 6):
        if policy == 'off':
            return {'prompt_cache_options': {'mode': 'explicit'}}
        return {'prompt_cache_options': {'mode': 'implicit', 'ttl': '30m'}}
    if policy == 'off':
        raise ProviderAccessError('unsupported', 'This GPT selection cannot disable prompt caching; use an empty policy for provider defaults.')
    # Documented families and their dated snapshots; unknown families do not
    # inherit retention capabilities from a similar-looking identifier.
    family = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', selection)
    if family in _EXTENDED:
        return {'prompt_cache_retention': '24h' if policy == 'high' or family in ('gpt-5.5', 'gpt-5.5-pro') else 'in_memory'}
    raise ProviderAccessError('unsupported', 'Prompt-cache policy support is unknown for this GPT selection; use an empty policy for provider defaults.')
