"""Concrete official API capability admission, separate from legacy requests.

Verified contracts: https://developers.openai.com/api/docs/guides/structured-outputs
and the exact GPT-5.6 Luna/Terra/Sol, GPT-6 Luna/Sol/Astra and GPT-6.1 Sol
model pages under https://developers.openai.com/api/docs/models/, 2026-10-27.
API contracts do not establish alternate-endpoint support. The existing
qualified GPT-6 Luna subscription Responses schema route remains admitted.
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
        if (base_url != OFFICIAL_BASE_URL
                or request.selected_llm not in _NATIVE_MODELS
                or api_mode not in ('responses', 'completions')):
            return 'unknown_capability'
        if mode == 'json' and response_schema(request.payload.response.schema.schema).get('type') != 'object':
            return 'unsupported_schema'
        if request.payload.tools:
            return 'unsupported_combination'
        if mode == 'structured' and not native_schema_supported(request.payload.response.schema):
            return 'unsupported_schema'
        return None
    return resolve_response(request, supports)
