"""Bounded application values, independent of provider transport vocabulary."""
import json
import math
from uuid import UUID, uuid4

from ..providers.catalog import catalog_to_wire
from ..providers.contracts import ContractError, ProviderAccessError
from ..providers.inference import request_from_wire, result_to_wire
from ..providers.selection import ModelSelectionPolicy


STREAM_BYTES = 1048576
STREAM_EVENTS = 8192

EFFORTS = ('off', 'low', 'medium', 'high')


def session_identifier(value):
    if value is None:
        return str(uuid4())
    if type(value) is not str:
        raise ContractError('Invalid session identifier')
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ContractError('Invalid session identifier') from None
    return value


def catalog(selections, default, reasoning_default):
    rows = catalog_to_wire(selections)
    if default not in {row['selected_llm'] for row in rows}:
        raise ContractError('Invalid application default')
    if reasoning_default is not None and reasoning_default not in EFFORTS:
        raise ContractError('Invalid reasoning default')
    models = []
    for row in rows:
        row['value'] = row.pop('selected_llm')
        row['label'] = row.pop('display_name') or row['value']
        models.append(row)
    return {'models': models, 'default': default, 'reasoning_default': reasoning_default,
            'family_defaults': _family_defaults(selections, default)}


def _family_defaults(selections, default):
    """Resolve advertised families using the ordinary model-default policy."""
    families = {}
    for selection in selections:
        if selection.family is not None:
            families.setdefault(selection.family, []).append(selection)
    policy = ModelSelectionPolicy({'LLM_DEFAULT_MODEL': '.*'})
    defaults = {}
    for family, values in families.items():
        if any(value.selected_llm == default for value in values):
            defaults[family] = default
            continue
        try:
            defaults[family] = policy.select_default(tuple(values))
        except ProviderAccessError:
            # Incomplete or ambiguous metadata cannot identify a family default.
            continue
    return defaults


def completion(row, default, reasoning_default):
    required = {'session_id', 'messages'}
    optional = {'model', 'reasoning_effort', 'max_output_tokens', 'cache_policy'}
    if type(row) is not dict or not required <= set(row) or set(row) - required - optional:
        raise ContractError('Invalid application request')
    session_id = session_identifier(row['session_id'])
    if 'reasoning_effort' in row and (type(row['reasoning_effort']) is not str or row['reasoning_effort'] not in EFFORTS):
        raise ContractError('Invalid reasoning effort')
    if 'max_output_tokens' in row and (type(row['max_output_tokens']) is not int or not 1 <= row['max_output_tokens'] <= 4096):
        raise ContractError('Invalid output limit')
    if 'cache_policy' in row and (type(row['cache_policy']) is not str or row['cache_policy'] not in ('off', 'medium', 'high')):
        raise ContractError('Invalid cache policy')
    if type(row['messages']) is not list:
        raise ContractError('Invalid messages')
    messages = []
    for item in row['messages']:
        if type(item) is not dict or set(item) != {'role', 'content'}:
            raise ContractError('Invalid message')
        messages.append({'role': item['role'], 'text': item['content']})
    wire = {'selected_llm': row.get('model', default), 'request_id': session_id,
            'messages': messages, 'max_output_tokens': row.get('max_output_tokens'),
            'cache_policy': row.get('cache_policy')}
    effort = row.get('reasoning_effort', reasoning_default)
    if effort is not None:
        wire['reasoning_effort'] = effort
    return session_id, request_from_wire(wire)


def result(session_id, value):
    row = result_to_wire(value)
    row['response_text'] = row.pop('text')
    return {'session_id': session_id, **row}


def frame(name, value):
    body = ('event: ' + name + '\ndata: ' + json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode()
    if max(len(body), len(json.dumps(value, separators=(',', ':')).encode())) > 131072:
        raise ContractError('Application event exceeds byte limit')
    return body


def heartbeat(interval, timeout):
    values = (interval, timeout)
    if any(type(value) not in (int, float) or not 0 < value <= 2147483.647
           or not math.isfinite(value) for value in values) or interval >= timeout:
        raise ContractError('Invalid application heartbeat timing')
    return {'heartbeat_interval_ms': interval * 1000,
            'heartbeat_timeout_ms': timeout * 1000}
