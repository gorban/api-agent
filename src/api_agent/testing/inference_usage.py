"""Content-free completion accounting from worker events to suite history."""
import json
import os
from uuid import uuid4, UUID
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar

_STARTS = {}
_TIMING = {'started_at', 'finished_at', 'attempt', 'turn_id', 'turn_kind', 'outcome', 'service_tier', 'serving_revision', 'reasoning_effort', 'conversation_id'}
_TURN = ContextVar('inference_user_turn', default=None)


@contextmanager
def user_turn(identifier, kind='initial'):
    """Workflow boundary: several model calls may belong to one user prompt."""
    if type(identifier) is not str or str(UUID(identifier)) != identifier or kind not in ('initial', 'clarification', 'approval'):
        raise ValueError('Invalid inference user turn')
    token = _TURN.set((identifier, kind))
    try:
        yield
    finally:
        _TURN.reset(token)

from api_agent.providers.inference import request_to_wire, result_to_wire, usage_to_wire


def publish(request, result=None, *, call_id=None, quote=None, failed=False, attempt=None, error=None):
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return None
    selection = request_to_wire(request)['selected_llm']
    call_id = str(uuid4()) if call_id is None else call_id
    now = datetime.now(timezone.utc).isoformat()
    started = _STARTS.setdefault(call_id, now)
    from api_agent.providers.contracts import ProviderAccessError
    reported = error.usage if isinstance(error, ProviderAccessError) else None
    row = {'call_id': call_id, 'selection': selection,
           'usage': (usage_to_wire(reported) if reported is not None else None)
                    if result is None else result_to_wire(result)['usage']}
    if request.payload.generation.reasoning_effort is not None:
        row['reasoning_effort'] = request.payload.generation.reasoning_effort or None
    if result is not None:
        for name in ('actual_model', 'service_tier', 'serving_revision'):
            if getattr(result, name) is not None:
                row[name] = getattr(result, name)
    if quote is not None:
        from api_agent.providers.costs import validate_quote
        row['quote'] = validate_quote(quote)
    turn_id, turn_kind = _TURN.get() or (call_id, 'initial')
    from api_agent.workflows.run_context import current_execution_context, current_turn_id
    execution = current_execution_context()
    turn_id = current_turn_id() or turn_id
    if execution is not None and execution['kind'] == 'conversation':
        row['conversation_id'] = execution['conversation_id']
    row.update(started_at=started, finished_at=now if result is not None or failed else None, attempt=int(os.environ.get('API_AGENT_TEST_ATTEMPT', '1')) if attempt is None else attempt, turn_id=turn_id, turn_kind=turn_kind, outcome='failed' if failed else 'unknown' if result is None else result.outcome)
    from api_agent.telemetry.accounting import validate_evidence
    validate_evidence({**row, 'actual_model': row.get('actual_model'), 'quote': row.get('quote'), 'service_tier': row.get('service_tier'), 'serving_revision': row.get('serving_revision')})
    if result is not None or failed:
        _STARTS.pop(call_id, None)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'inference_usage', 'node': node.rsplit(' ', 1)[0],
                                 'data': row}, allow_nan=False) + '\n')
    if result is not None or failed:
        from api_agent.workflows.attribution import current_workflow_visit
        owner = current_workflow_visit()
        if owner is not None:
            journal, visit_id, attempt_id = owner
            journal.record_call(call_id=call_id, visit_id=visit_id,
                                attempt_id=attempt_id, usage=row['usage'],
                                selection=selection, outcome=row['outcome'],
                                actual_model=row.get('actual_model'),
                                reasoning_effort=row.get('reasoning_effort'))
            from api_agent.workflows.attribution import admit_terminal_call_receipt
            admit_terminal_call_receipt({**row, 'visit_id': visit_id, 'attempt_id': attempt_id}, journal)
    return call_id


def capture(event, tracker):
    row = json.loads(event.text)
    if type(row) is not dict or set(row) - _TIMING not in ({'call_id', 'selection', 'usage'}, {'call_id', 'selection', 'usage', 'quote'}, {'call_id', 'selection', 'usage', 'actual_model'}, {'call_id', 'selection', 'usage', 'quote', 'actual_model'}):
        raise ValueError('Invalid inference usage event')
    identifier = row['call_id']
    if type(identifier) is not str or str(UUID(identifier)) != identifier:
        raise ValueError('Invalid inference usage identity')
    selection = row['selection']
    if type(selection) is not str or not selection or len(selection) > 200 or not selection.isprintable():
        raise ValueError('Invalid inference usage selection')
    usage = row['usage']
    from api_agent.telemetry.accounting import validate_evidence
    evidence = dict(call_id=identifier, selection=selection, actual_model=row.get('actual_model'), service_tier=row.get('service_tier'), serving_revision=row.get('serving_revision'), usage=usage, quote=row.get('quote'), started_at=row.get('started_at'), finished_at=row.get('finished_at'), attempt=row.get('attempt', 1), turn_id=row.get('turn_id', identifier), turn_kind=row.get('turn_kind', 'initial'), outcome=row.get('outcome', 'unknown' if usage is None else 'completed'))
    if 'conversation_id' in row:
        evidence['conversation_id'] = row['conversation_id']
    if 'reasoning_effort' in row:
        evidence['reasoning_effort'] = row['reasoning_effort']
    validate_evidence(evidence)
    if not tracker.record_inference(identifier, event.identity.id, evidence):
        return
    if usage is None:
        tracker.record_prompt(turn_id=event.identity.id, prompt_id=evidence['turn_id'], kind=evidence['turn_kind'])
    else:
        from api_agent.providers.inference import result_from_wire
        values = result_from_wire({'text': '', 'outcome': 'completed', 'usage': usage}).usage
        tracker.add_usage(provider='selected-llm', model=selection, session_id=event.identity.id,
            input_tokens=values.input_tokens, output_tokens=values.output_tokens,
            cache_read_tokens=values.cached_input_tokens, reasoning_tokens=values.reasoning_tokens,
            cache_creation_tokens=values.cache_write_tokens)

    tracker.add_cost(identifier, event.identity.id, row.get('quote'))
