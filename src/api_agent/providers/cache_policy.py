"""Apply shared prompt-cache policy without changing explicit call options."""
from dataclasses import replace

from .llm import GenerationOptions, LLMRequest
from .contracts import ContractError, ProviderAccessError


def configured_request(request, policy, *, reasoning_effort=None):
    if not isinstance(request, LLMRequest):
        return request
    configured = GenerationOptions(cache_policy=policy, reasoning_effort=reasoning_effort)
    current = request.payload.generation
    generation = replace(current,
        cache_policy=current.cache_policy if current.cache_policy is not None else configured.cache_policy,
        reasoning_effort=current.reasoning_effort if current.reasoning_effort is not None else configured.reasoning_effort)
    if generation == current:
        return request
    return replace(request, payload=replace(request.payload, generation=generation))


def validate_provider_request(adapter, request):
    if request.payload.generation.cache_policy is not None and 'cache_policy' not in getattr(getattr(adapter, 'capabilities', None), 'features', ()):
        raise ProviderAccessError('unsupported', 'The selected LLM provider does not support LLM_CACHE_POLICY; use an empty policy for provider defaults.')
    if request.payload.generation.reasoning_effort and 'reasoning_effort' not in getattr(getattr(adapter, 'capabilities', None), 'features', ()):
        raise ProviderAccessError('unsupported', 'The selected LLM provider does not support LLM_REASONING_EFFORT; use an empty effort for provider defaults.')
    validator = getattr(adapter, 'validate_request', None)
    if validator is not None:
        if not callable(validator):
            raise ContractError('Provider request validation must be callable.')
        validator(request)
