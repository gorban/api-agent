"""Adapter-only response preparation; never retries or changes routing."""
from dataclasses import replace
import hashlib

from api_agent.providers.control import OperationalError
from api_agent.providers.llm import ResponseEvidence, LLMRequest
from api_agent.providers.rich_inference import schema
from api_agent.providers.contracts import ContractError, ProviderAccessError


def native_schema_supported(definition):
    """Admit a conservative native subset without editing the original schema."""
    try:
        decoded = schema(definition.schema, definition.strict)
    except ContractError:
        return False
    pending = [decoded]
    while pending:
        node = pending.pop()
        if type(node) is not dict or set(node) - {'type', 'properties', 'required', 'additionalProperties',
                       'items', 'enum', 'title', 'description'}:
            return False
        pending.extend(node.get('properties', {}).values())
        if 'items' in node:
            pending.append(node['items'])
        if type(node.get('additionalProperties')) is dict:
            pending.append(node['additionalProperties'])
    return True


def resolve_response(request, supports):
    """Resolve once before dispatch, preserving authored messages and contracts."""
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'Invalid response-policy request.')
    policy = request.payload.response
    if policy is None:
        return request, None
    from api_agent.providers.rich_inference import request_to_wire
    request_to_wire(request, _validate_instances=False)
    choices = {'structured': ('structured', 'json', 'prompted_json'),
               'json': ('json', 'prompted_json'),
               'prompted_json': ('prompted_json',), 'text': ('text',)}[policy.mode]
    reason = None
    for actual in choices:
        rejection = supports(actual)
        if rejection is None:
            break
        reason = reason or rejection
        if policy.fallback == 'reject':
            raise OperationalError('unsupported')
    else:
        raise OperationalError('unsupported')
    definition = policy.schema
    evidence = ResponseEvidence(policy.mode, policy.fallback, actual,
        hashlib.sha256(definition.schema.encode('utf-8')).hexdigest() if definition else None,
        definition.strict if definition else None, reason if actual != policy.mode else None)
    payload = replace(request.payload, response=None,
                      structured=definition if actual == 'structured' else None)
    return replace(request, payload=payload), evidence


def format_payload(request, body, evidence):
    """Append one bounded adapter instruction only after authored-wire admission."""
    if evidence is None or evidence.actual_mode not in ('json', 'prompted_json'):
        return body
    from api_agent.providers.rich_inference import _bounded
    container = 'input' if 'input' in body else 'messages'
    body = {**body, container: [*body[container], {'role': 'user', 'content':
        'Return a complete final answer containing only JSON satisfying this response schema. '
        'Include the final JSON answer even when reasoning is enabled; reasoning is not the answer.\n'
        + request.payload.response.schema.schema}]}
    _bounded(body)
    return body


def capture_payload(request, body, evidence, *, auth):
    """Use the existing scrubbed capture stream, with instruction positions only."""
    from api_agent.workflows.inference_capture import capture_inference
    added = []
    if evidence is not None and evidence.actual_mode in ('json', 'prompted_json'):
        container = 'input' if 'input' in body else 'messages'
        added = [{'role': 'user', 'path': f'/{container}/{len(body[container]) - 1}',
                  'purpose': 'response_policy'}]
    capture_inference('input', {'provider_payload': body, 'adapter_instructions': added},
                      request.request_id, auth=auth)
