"""Content-free completion accounting from worker events to suite history."""
import json
import os
from uuid import uuid4, UUID

from api_agent.providers.inference import request_to_wire, result_to_wire


def publish(request, result=None, *, call_id=None, quote=None):
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return None
    selection = request_to_wire(request)['selected_llm']
    call_id = str(uuid4()) if call_id is None else call_id
    row = {'call_id': call_id, 'selection': selection,
           'usage': None if result is None else result_to_wire(result)['usage']}
    if quote is not None:
        from api_agent.providers.costs import validate_quote
        row['quote'] = validate_quote(quote)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'inference_usage', 'node': node.rsplit(' ', 1)[0],
                                 'data': row}, allow_nan=False) + '\n')
    return call_id


def capture(event, tracker):
    row = json.loads(event.text)
    if type(row) is not dict or set(row) not in ({'call_id', 'selection', 'usage'}, {'call_id', 'selection', 'usage', 'quote'}):
        raise ValueError('Invalid inference usage event')
    identifier = row['call_id']
    if type(identifier) is not str or str(UUID(identifier)) != identifier:
        raise ValueError('Invalid inference usage identity')
    selection = row['selection']
    if type(selection) is not str or not selection or len(selection) > 200 or not selection.isprintable():
        raise ValueError('Invalid inference usage selection')
    usage = row['usage']
    tracker.add_cost(identifier, event.identity.id, row.get('quote'))
    if usage is None:
        tracker.record_prompt(turn_id=event.identity.id, prompt_id=identifier, kind='initial')
    else:
        from api_agent.providers.inference import result_from_wire
        values = result_from_wire({'text': '', 'outcome': 'completed', 'usage': usage}).usage
        tracker.add_usage(provider='selected-llm', model=selection, session_id=event.identity.id,
            input_tokens=values.input_tokens, output_tokens=values.output_tokens,
            cache_read_tokens=values.cached_input_tokens, reasoning_tokens=values.reasoning_tokens)
