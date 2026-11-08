"""Content-free completion accounting from worker events to suite history."""
import json
import os
from uuid import uuid4, UUID
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_STARTS = {}
_CURRENT_OWNER = object()
_TIMING = {'started_at', 'finished_at', 'attempt', 'turn_id', 'turn_kind', 'outcome', 'service_tier', 'serving_revision', 'reasoning_effort', 'conversation_id', 'invocation_id', 'response'}
_TURN = ContextVar('inference_user_turn', default=None)


_DELEGATED_NODE = ContextVar('delegated_test_node', default=None)


@contextmanager
def delegated_test_node(node):
    """Bind an owning launcher test without changing process-wide pytest state."""
    if type(node) is not str or not node or len(node) > 4096 or not node.isprintable():
        raise ValueError('Invalid delegated test node')
    token = _DELEGATED_NODE.set(node)
    try:
        yield
    finally:
        _DELEGATED_NODE.reset(token)


def _delegated_node():
    node = _DELEGATED_NODE.get()
    if node is not None:
        return node
    current = os.environ.get('PYTEST_CURRENT_TEST')
    return current.rsplit(' ', 1)[0] if current else None


def publish_external_invocation(invocation_id: str, *, turn_id: str | None = None,
                                call_ids=None, complete=False) -> bool:
    """Publish a content-free native graph boundary with no token observation."""
    from api_agent.workflows.run_context import test_evidence_allowed
    if not test_evidence_allowed():
        return False
    if type(invocation_id) is not str or str(UUID(invocation_id)) != invocation_id:
        raise ValueError('Invalid native invocation identity')
    if turn_id is None:
        from api_agent.workflows.run_context import current_turn_id
        turn_id = current_turn_id() or invocation_id
    if type(turn_id) is not str or str(UUID(turn_id)) != turn_id:
        raise ValueError('Invalid native turn identity')
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = _delegated_node()
    if not path or not node:
        return False
    data = {'invocation_id': invocation_id, 'turn_id': turn_id}
    if call_ids is not None:
        _validate_inventory(call_ids, complete)
        data.update(call_ids=call_ids, complete=complete)
    elif complete:
        raise ValueError('Native completion requires call inventory')
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'native_invocation', 'node': node,
                                 'data': data}) + '\n')
    return True


def _validate_inventory(call_ids, complete):
    if type(call_ids) is not list or len(call_ids) > 100000 or type(complete) is not bool:
        raise ValueError('Invalid native call inventory')
    for value in call_ids:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError('Invalid native call identity')
    if len(set(call_ids)) != len(call_ids):
        raise ValueError('Duplicate native call inventory')


def capture_external_invocation(event, tracker) -> bool:
    """Admit only the two UUIDs from an owned native invocation event."""
    data = json.loads(event.text)
    if type(data) is not dict or set(data) not in ({'invocation_id', 'turn_id'}, {'invocation_id', 'turn_id', 'call_ids', 'complete'}):
        raise ValueError('Invalid native invocation event')
    if 'call_ids' in data:
        _validate_inventory(data['call_ids'], data['complete'])
    for value in (data['invocation_id'], data['turn_id']):
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError('Invalid native invocation identity')
    if event.identity is None:
        raise ValueError('Native invocation requires test identity')
    return tracker.record_external_invocation(session_id=event.identity.id,
        invocation_id=data['invocation_id'], turn_id=data['turn_id'],
        call_ids=data.get('call_ids'), complete=data.get('complete', False))


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
    from api_agent.workflows.run_context import test_evidence_allowed
    if not test_evidence_allowed():
        return None
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return None
    selection = request_to_wire(request)['selected_llm']
    call_id = str(uuid4()) if call_id is None else call_id
    now = datetime.now(timezone.utc).isoformat()
    started = _STARTS.setdefault(call_id, now)
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.inference import error_usage
    reported = error_usage(error)
    row = {'call_id': call_id, 'selection': selection,
           'usage': (usage_to_wire(reported) if reported is not None else None)
                    if result is None else result_to_wire(result)['usage']}
    attempts = getattr(result if result is not None else error, 'transport_attempts', None)
    if attempts is not None:
        from api_agent.providers.generation_attempts import validate_transport_attempts
        row['transport_attempts'] = validate_transport_attempts(attempts)
    if request.payload.generation.reasoning_effort is not None:
        row['reasoning_effort'] = request.payload.generation.reasoning_effort or None
    if result is not None:
        for name in ('actual_model', 'service_tier', 'serving_revision'):
            if getattr(result, name) is not None:
                row[name] = getattr(result, name)
    response = result.response if result is not None else getattr(error, 'response', None)
    if response is not None:
        from api_agent.providers.rich_inference import response_evidence_to_wire
        row['response'] = response_evidence_to_wire(response)
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
                                reasoning_effort=row.get('reasoning_effort'),
                                **({'transport_attempts': row['transport_attempts']} if 'transport_attempts' in row else {}),
                                **({'response': row['response']} if 'response' in row else {}))
            from api_agent.workflows.attribution import admit_terminal_call_receipt
            admit_terminal_call_receipt({**row, 'visit_id': visit_id, 'attempt_id': attempt_id}, journal)
        if failed and isinstance(error, BaseException):
            from api_agent.telemetry.outcomes import retain_failure
            retain_failure(error, cause_id=call_id, provider=True)
    return call_id


@dataclass(frozen=True)
class DelegatedOwner:
    journal: object
    visit_id: str
    attempt_id: str | None
    turn_id: str
    turn_kind: str
    conversation_id: str | None
    invocation_id: str
    node: str | None = None


def capture_delegated_owner():
    """Freeze journal identity and start context before delegated callbacks run."""
    from api_agent.workflows.attribution import current_workflow_visit
    owner = current_workflow_visit()
    return None if owner is None else _snapshot_owner(owner, at_start=True)


def _snapshot_owner(owner, *, at_start=False):
    if isinstance(owner, DelegatedOwner):
        return owner
    journal, visit_id, attempt_id = owner
    execution = getattr(journal, 'execution_context', None)
    if execution is None:
        execution = getattr(getattr(journal, 'journal', None), 'execution_context', None)
    if execution is None and at_start:
        from api_agent.workflows.run_context import current_execution_context
        execution = current_execution_context()
    conversation_id = execution.get('conversation_id') if type(execution) is dict and execution.get('kind') == 'conversation' else None
    kind = (_TURN.get() or (None, 'initial'))[1] if at_start else 'initial'
    return DelegatedOwner(journal, visit_id, attempt_id, journal.turn_id, kind,
                          conversation_id, journal.invocation_id, _delegated_node() if at_start else None)


def publish_delegated_usage(*, call_id, selection, usage, started_at, finished_at,
                            outcome, actual_model=None, owner=_CURRENT_OWNER, turn_id=None,
                            conversation_id=None, transport_attempts=None):
    """Publish one terminal physical call without request/response content.

    Callers retain the stable provider run UUID across duplicate callback delivery.
    Transport ledgers, when present, replace (never add to) parent call usage.
    Capture ``owner`` at call start; completion may run in another callback context.
    """
    from api_agent.workflows.run_context import (test_evidence_allowed,
        current_turn_id, current_execution_context)
    if not test_evidence_allowed():
        return None
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    owner = capture_delegated_owner() if owner is _CURRENT_OWNER else None if owner is None else _snapshot_owner(owner)
    node = owner.node if owner is not None and owner.node is not None else _delegated_node()
    if not path or not node:
        return None
    if finished_at is None or started_at is None or outcome == 'unknown':
        raise ValueError('Delegated usage requires terminal timing and outcome')
    turn, kind = _TURN.get() or (call_id, 'initial')
    if owner is not None:
        if turn_id is not None and turn_id != owner.turn_id:
            raise ValueError('Conflicting delegated turn owner')
        if conversation_id is not None and conversation_id != owner.conversation_id:
            raise ValueError('Conflicting delegated conversation owner')
        turn_id, kind, conversation_id = owner.turn_id, owner.turn_kind, owner.conversation_id
    row = dict(call_id=call_id, selection=selection, usage=usage,
               actual_model=actual_model, started_at=started_at, finished_at=finished_at,
               attempt=int(os.environ.get('API_AGENT_TEST_ATTEMPT', '1')),
               turn_id=turn_id or current_turn_id() or turn, turn_kind=kind, outcome=outcome)
    execution = current_execution_context() if owner is None else None
    if owner is not None:
        row['invocation_id'] = owner.invocation_id
    if conversation_id is None and execution is not None and execution['kind'] == 'conversation':
        conversation_id = execution['conversation_id']
    if conversation_id is not None:
        row['conversation_id'] = conversation_id
    if transport_attempts is not None:
        row['transport_attempts'] = transport_attempts
    # Reuse the worker ingress validator before emitting any bytes or journal facts.
    from types import SimpleNamespace
    validate_event(SimpleNamespace(text=json.dumps(row, allow_nan=False)))
    if owner is not None:
        journal, visit_id, attempt_id = owner.journal, owner.visit_id, owner.attempt_id
        elapsed = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
        journal.record_call(call_id=call_id, visit_id=visit_id, attempt_id=attempt_id,
            usage=usage, selection=selection, actual_model=actual_model,
            outcome=outcome, elapsed=elapsed,
            **({'transport_attempts': transport_attempts} if transport_attempts is not None else {}))
        from api_agent.workflows.attribution import admit_terminal_call_receipt
        admit_terminal_call_receipt({**row, 'visit_id': visit_id, 'attempt_id': attempt_id}, journal)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'inference_usage', 'node': node,
                                 'data': row}, allow_nan=False) + '\n')
    return call_id


def validate_event(event):
    """Validate raw accounting before privacy transformations can remove fields."""
    row = json.loads(event.text)
    if type(row) is not dict or set(row) - _TIMING - {'transport_attempts'} not in ({'call_id', 'selection', 'usage'}, {'call_id', 'selection', 'usage', 'quote'}, {'call_id', 'selection', 'usage', 'actual_model'}, {'call_id', 'selection', 'usage', 'quote', 'actual_model'}):
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
    if 'invocation_id' in row:
        evidence['invocation_id'] = row['invocation_id']
    if 'conversation_id' in row:
        evidence['conversation_id'] = row['conversation_id']
    if 'reasoning_effort' in row:
        evidence['reasoning_effort'] = row['reasoning_effort']
    if 'transport_attempts' in row:
        evidence['transport_attempts'] = row['transport_attempts']
    if 'response' in row:
        evidence['response'] = row['response']
    validate_evidence(evidence)
    return row, evidence


def capture(event, tracker):
    row, evidence = validate_event(event)
    identifier, selection, usage = row['call_id'], row['selection'], row['usage']
    if not tracker.record_inference(identifier, event.identity.id, evidence):
        return
    tracker.record_prompt(turn_id=event.identity.id, prompt_id=evidence['turn_id'], kind=evidence['turn_kind'])
    ledger = row.get('transport_attempts')
    if ledger is not None:
        from api_agent.providers.generation_attempts import rollup_transport_attempts
        summary = rollup_transport_attempts(ledger)
        names = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
                 'cached_input_tokens': 'cache_read_tokens', 'cache_write_tokens': 'cache_creation_tokens',
                 'reasoning_tokens': 'reasoning_tokens'}
        count = len(ledger['attempts'])
        known = {target: summary['usage'][source] for source, target in names.items()
                 if count > summary['unknown_usage_attempts'][source]}
        if summary['known_total_tokens'] is not None:
            known['total_tokens'] = summary['known_total_tokens']
        exact = {target: None if not count or summary['unknown_remainder'] or summary['unknown_usage_attempts'][source]
                 else summary['usage'][source] for source, target in names.items()}
        tracker.add_usage(provider='selected-llm', model=selection, session_id=event.identity.id,
                          known_subtotals=known, **exact)
        if summary['unknown_remainder'] or summary['unknown_cost_attempts']:
            quote = None
        else:
            quote = {'list_cost_usd': summary['list_cost_usd'], 'payment': None}
        tracker.add_cost(identifier, event.identity.id, quote, known_subtotal=summary['list_cost_usd'])
    else:
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
