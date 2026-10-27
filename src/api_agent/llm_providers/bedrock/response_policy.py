"""Response modes remain bounded by the qualified Mantle execution matrix."""
from api_agent.llm_providers.response_policy import resolve_response, native_schema_supported


def prepare_response(request, api_mode):
    def supports(mode):
        if mode in ('text', 'prompted_json'):
            return None
        if mode == 'json':
            return 'unsupported_mode'
        if request.selected_llm != 'zai.glm-5' or api_mode != 'completions':
            return 'unknown_capability'
        if request.payload.tools or request.payload.generation.reasoning_effort:
            return 'unsupported_combination'
        definition = request.payload.response.schema
        if not definition.strict or not native_schema_supported(definition):
            return 'unsupported_schema'
        return None
    return resolve_response(request, supports)
