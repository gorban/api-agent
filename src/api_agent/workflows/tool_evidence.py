"""Content-free tool execution evidence for ordinary integration reporting."""
import json

from jsonschema import Draft202012Validator

from api_agent.providers.tools import canonical


_LABEL = {'type': 'string', 'pattern': r'^[A-Za-z0-9_./:-]{1,200}$'}
_COUNT = {'type': ['integer', 'null'], 'minimum': 0}
_USAGE = {'type': ['object', 'null'], 'properties': {
    name: _COUNT for name in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                             'reasoning_tokens', 'cache_write_tokens')}, 'additionalProperties': False}
_CALL = {'type': 'object', 'properties': {
    'tool': _LABEL, 'instance': _LABEL, 'invocation': _LABEL,
    'status': {'enum': ['preparing', 'verified', 'success', 'partial', 'failure']},
    'verified_by_inference': {'type': 'boolean'}, 'result_received': {'type': 'boolean'},
    'result_matches': {'type': 'boolean'}},
    'required': ['tool', 'instance', 'invocation', 'status', 'verified_by_inference', 'result_received'],
    'additionalProperties': False}
_INFERENCE = {'type': 'object', 'properties': {
    'request_id': _LABEL, 'kind': {'enum': ['conversation', 'relevance']},
    'outcome': {'enum': ['started', 'failed', 'completed', 'tool_calls', 'length', 'refused']},
    'usage': _USAGE}, 'required': ['request_id', 'kind', 'outcome', 'usage'],
    'additionalProperties': False}
_SCHEMA = {'type': 'object', 'properties': {
    'version': {'const': 1}, 'kind': {'const': 'tool_execution'}, 'graph_id': _LABEL,
    'calls': {'type': 'array', 'items': _CALL, 'maxItems': 128},
    'inference': {'type': 'array', 'items': _INFERENCE, 'maxItems': 256},
    'final_matches': {'type': 'boolean'}, 'completed': {'type': 'boolean'},
    'sharing': {'type': 'object', 'properties': {
        'primary': _LABEL, 'secondary': {'type': 'array', 'items': _LABEL, 'maxItems': 16},
        'derived_slots': {'type': 'array', 'items': {'enum': ['ENDPOINT', 'HEADERS']}, 'maxItems': 2}},
        'required': ['primary', 'secondary', 'derived_slots'], 'additionalProperties': False},
    'ownership': {'type': 'object', 'properties': {
        name: {'type': 'integer', 'minimum': 0, 'maximum': 128} for name in
        ('targets', 'owners_started', 'owners_remaining', 'service_leases')},
        'additionalProperties': False}},
    'required': ['version', 'kind', 'graph_id', 'calls', 'inference', 'completed', 'ownership'],
    'additionalProperties': False}


def validate_tool_evidence(value):
    """Validate exact structural evidence; reject content rather than dropping it."""
    encoded = canonical(value, 65536)
    Draft202012Validator(_SCHEMA).validate(value)
    return json.loads(encoded)


def tool_evidence(journal, *, graph_id, instances, ownership=None, sharing=None):
    """Project only fixed fields; actual payloads and final text remain in-process."""
    calls = []
    for row in journal.get('calls', ()):
        item = {name: row[name] for name in ('tool', 'invocation', 'status')}
        item.update(instance=instances[row['tool']],
                    verified_by_inference=row.get('verified_by_inference') is True,
                    result_received='result' in row)
        if 'result_matches' in row:
            item['result_matches'] = row['result_matches']
        calls.append(item)
    result = {'version': 1, 'kind': 'tool_execution', 'graph_id': graph_id,
              'calls': calls, 'inference': [
                  {key: row[key] for key in ('request_id', 'kind', 'outcome', 'usage')}
                  for row in journal.get('inference', ())],
              'completed': journal.get('completed') is True,
              'ownership': {} if ownership is None else dict(ownership)}
    if 'final_matches' in journal:
        result['final_matches'] = journal['final_matches']
    if sharing is not None:
        result['sharing'] = dict(sharing)
    return validate_tool_evidence(result)
