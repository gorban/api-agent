"""Response modes follow the selected Bedrock model, endpoint and API."""
from api_agent.llm_providers.response_policy import resolve_response, native_schema_supported


def prepare_response(request, api_mode, *, endpoint="mantle", stream=False):
    def supports(mode):
        if mode in ('text', 'prompted_json'):
            return None
        if mode == 'json':
            return 'unsupported_mode'
        if endpoint == 'mantle':
            supported = request.selected_llm == 'zai.glm-5' and api_mode == 'completions'
        else:
            from .catalog import selection
            supported = 'structured' in selection(request.selected_llm, api_mode, endpoint=endpoint).features
        if not supported:
            return 'unknown_capability'
        if endpoint == 'runtime' and stream and api_mode != 'converse':
            return 'unsupported_combination'
        if request.payload.tools or request.payload.generation.reasoning_effort:
            return 'unsupported_combination'
        definition = request.payload.response.schema
        if not definition.strict or not native_schema_supported(definition):
            return 'unsupported_schema'
        return None
    return resolve_response(request, supports)
