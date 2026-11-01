"""Response modes follow the selected Bedrock model, endpoint and API."""
from api_agent.llm_providers.response_policy import resolve_response, native_schema_supported


# Exact Runtime Responses qualification; display-family parsing grants nothing.
# Public contracts: model-card-openai-gpt-56-luna.html and
# model-card-openai-gpt-6-luna.html in the AWS Bedrock user guide.
_NATIVE_RESPONSES = frozenset({'us.openai.gpt-5.6-luna', 'us.openai.gpt-6-luna'})


def qualified_native_responses(request, api_mode, endpoint):
    """Keep native reasoning/stream/tool admission scoped to qualified requests."""
    return (endpoint == 'runtime' and api_mode == 'responses'
            and request.selected_llm in _NATIVE_RESPONSES
            and request.payload.generation.reasoning_effort in (None, '', 'low', 'high')
            and not any(tool.strict for tool in request.payload.tools))


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
        if (not qualified_native_responses(request, api_mode, endpoint)
                and ((endpoint == 'runtime' and stream and api_mode != 'converse')
                     or request.payload.tools or request.payload.generation.reasoning_effort)):
            return 'unsupported_combination'
        definition = request.payload.response.schema
        if not definition.strict or not native_schema_supported(definition):
            return 'unsupported_schema'
        return None
    return resolve_response(request, supports)
