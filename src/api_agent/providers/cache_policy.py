"""Apply shared prompt-cache policy without changing explicit call options."""
from dataclasses import replace

from .llm import GenerationOptions, LLMRequest
from .contracts import ContractError, ProviderAccessError


def configured_request(request, policy):
    if not isinstance(request, LLMRequest):
        return request
    configured = GenerationOptions(cache_policy=policy).cache_policy
    if request.payload.generation.cache_policy is not None or configured is None:
        return request
    generation = replace(request.payload.generation, cache_policy=configured)
    return replace(request, payload=replace(request.payload, generation=generation))


def validate_provider_request(adapter, request):
    if request.payload.generation.cache_policy is not None and 'cache_policy' not in getattr(getattr(adapter, 'capabilities', None), 'features', ()):
        raise ProviderAccessError('unsupported', 'The selected LLM provider does not support LLM_CACHE_POLICY; use an empty policy for provider defaults.')
    validator = getattr(adapter, 'validate_request', None)
    if validator is not None:
        if not callable(validator):
            raise ContractError('Provider request validation must be callable.')
        validator(request)
