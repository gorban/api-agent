"""Optional failure content: explicit public fields, never exception formatting."""
import json
import math


def failure_content(error, *, candidate_feedback=None, include_candidate=True, candidate_text=None):
    from api_agent.providers.contracts import CandidateOutputError, ProviderAccessError
    from .outcomes import failure_diagnostic
    value = failure_diagnostic(error)
    if isinstance(error, ProviderAccessError):
        provider = {}
        for name, limit in (('message', 512), ('code', 100), ('type', 100), ('request_id', 100)):
            item = getattr(error, name, None)
            if type(item) is str and 0 < len(item) <= limit and item.isprintable():
                provider[name] = item
        status = getattr(error, 'status', None)
        if type(status) is int and 100 <= status <= 599:
            provider['status'] = status
        if provider:
            value['provider'] = provider
    confirmed = candidate_feedback is not None and type(candidate_text) is str
    if type(error) is CandidateOutputError and include_candidate and (confirmed or candidate_feedback is None):
        visible = candidate_text if confirmed else ''.join(part.text for part in error.result.payload.content)
        value['candidate'] = {'visible_text': visible}
    if type(error) is CandidateOutputError and include_candidate and confirmed:
        feedback = candidate_feedback
        safe = {}
        if feedback.get('reason') in ('response_schema', 'response_json'):
            safe['reason'] = feedback['reason']
        facts = []
        for fact in feedback.get('errors', [])[:16]:
            if type(fact) is not dict:
                continue
            # Only numeric constraints/counts, never schema literals or instances.
            if fact.get('keyword') not in ('maxLength', 'minLength', 'maxItems', 'minItems',
                    'maxProperties', 'minProperties', 'minimum', 'maximum', 'exclusiveMinimum',
                    'exclusiveMaximum', 'multipleOf'):
                continue
            path = fact.get('path')
            if type(path) is not list or len(path) > 16 or any(
                    not (type(part) is int and part >= 0 or type(part) is str and len(part) <= 200)
                    for part in path):
                continue
            selected = {'path': path, 'keyword': fact['keyword']}
            for name in ('constraint', 'actual'):
                item = fact.get(name)
                if (type(item) in (int, float) and math.isfinite(item)
                        and (name != 'actual' or fact['keyword'] in ('maxLength', 'minLength',
                             'maxItems', 'minItems', 'maxProperties', 'minProperties')
                             and type(item) is int and item >= 0)):
                    selected[name] = item
            facts.append(selected)
        safe['errors'] = facts
        value['candidate']['feedback'] = safe
    if len(json.dumps(value, allow_nan=False).encode()) > 65536:
        raise ValueError('Optional failure content exceeds its bound')
    return value
