"""Version 1 structural GenAI facts from the validated shared provider boundary."""
from api_agent.providers.llm import LLMRequest, LLMResult


def provider_fields(request, result, provider_name=None):
    if not isinstance(request, LLMRequest) or not isinstance(result, LLMResult):
        raise TypeError('Provider trace facts require validated request and result values')
    options = request.payload.generation
    fields = {'gen_ai.provider.name': provider_name,
              'gen_ai.request.model': request.selected_llm,
              'gen_ai.response.model': result.actual_model,
              'api_agent.response.service_tier': result.service_tier,
              'api_agent.response.serving_revision': result.serving_revision,
              'gen_ai.request.temperature': options.temperature,
              'api_agent.llm.outcome': result.outcome}
    for source, target in (('input_tokens','input_tokens'),('output_tokens','output_tokens'),
                          ('cached_input_tokens','cache_read.input_tokens'),
                          ('cache_write_tokens','cache_write.input_tokens'),
                          ('reasoning_tokens','reasoning.output_tokens')):
        value = getattr(result.usage, source)
        if value is not None:
            fields['gen_ai.usage.' + target] = value
    return fields


def diagnostic_fields(diagnostics, boundary):
    from .inference_diagnostics import scrub_diagnostics
    value = scrub_diagnostics(diagnostics, boundary)
    return {target:value[source] for source,target in (
        ('response_id','gen_ai.response.id'),('finish_reasons','gen_ai.response.finish_reasons'),
        ('response_status','api_agent.response.status'),
        ('max_output_tokens','gen_ai.request.max_tokens'),('reasoning_effort','gen_ai.request.reasoning.level'))
        if source in value}


_USAGE_FIELDS = (('input_tokens','input_tokens'),('output_tokens','output_tokens'),
                 ('cached_input_tokens','cache_read.input_tokens'),
                 ('cache_write_tokens','cache_write.input_tokens'),
                 ('reasoning_tokens','reasoning.output_tokens'))


def ledger_usage(ledger):
    """Standard generation totals require complete knowledge, per source field."""
    attempts = ledger.get('attempts', ())
    executed = [item for item in attempts if item.get('execution') != 'not_started'
                or item.get('usage') is not None]
    complete = bool(ledger.get('complete') is True and executed)
    fields = {}
    for source,target in _USAGE_FIELDS:
        counts = [(item.get('usage') or {}).get(source) for item in executed]
        known = ledger.get('complete') is True and bool(counts) and all(type(value) is int and 0 <= value <= (1 << 53)-1 for value in counts)
        total = sum(counts) if known else None
        known = known and total <= (1 << 53)-1
        if source in ('input_tokens','output_tokens'):
            complete = complete and known
        if known:
            fields['gen_ai.usage.'+target] = total
    fields['api_agent.usage.complete'] = complete
    return fields


def transport_usage(attempt):
    return {'api_agent.transport.usage.'+target:(attempt.get('usage') or {})[source]
            for source,target in _USAGE_FIELDS if (attempt.get('usage') or {}).get(source) is not None}
