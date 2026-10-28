"""Concrete official API capability admission, separate from legacy requests.

Verified contracts: https://developers.openai.com/api/docs/guides/structured-outputs
and the exact GPT-5.6 Luna/Terra/Sol, GPT-6 Luna/Sol/Astra and GPT-6.1 Sol
model pages under https://developers.openai.com/api/docs/models/, 2026-10-27.
OpenRouter documents GPT-6 Luna structured output and its Responses API;
its routing must require parameter support for the selected endpoint.
https://openrouter.ai/openai/gpt-6-luna/
https://openrouter.ai/docs/guides/features/structured-outputs
"""
from api_agent.llm_providers.response_policy import resolve_response, native_schema_supported
from .endpoint import OFFICIAL_BASE_URL
from api_agent.providers.rich_inference import response_schema

_NATIVE_MODELS = frozenset(('gpt-4o-mini', 'gpt-4o-mini-2024-07-18',
    'gpt-4o-2024-08-06', 'gpt-6-luna', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-astra', 'gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'))


def prepare_response(request, *, base_url, api_mode, subscription):
    def supports(mode):
        if mode in ('text', 'prompted_json'):
            return None
        if subscription and (api_mode != 'responses' or request.selected_llm != 'gpt-6-luna' or mode != 'structured'):
            return 'unknown_capability'
        official = (base_url == OFFICIAL_BASE_URL and request.selected_llm in _NATIVE_MODELS
                    and api_mode in ('responses', 'completions'))
        router = (not subscription and base_url == 'https://openrouter.ai/api/v1'
                  and request.selected_llm == 'openai/gpt-6-luna'
                  and api_mode in ('responses', 'completions') and mode == 'structured')
        if not (official or router):
            return 'unknown_capability'
        if mode == 'json' and response_schema(request.payload.response.schema.schema).get('type') != 'object':
            return 'unsupported_schema'
        if request.payload.tools:
            return 'unsupported_combination'
        if mode == 'structured' and not native_schema_supported(request.payload.response.schema):
            return 'unsupported_schema'
        return None
    return resolve_response(request, supports)
