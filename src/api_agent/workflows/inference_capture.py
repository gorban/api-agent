"""Credential-authority provider payload capture and workflow attribution."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from .attribution import current_workflow_visit

_COLLECTOR = ContextVar('provider_payload_collector', default=None)
_CALL_ID = ContextVar('provider_payload_call_id', default=None)
_DISPATCH = ContextVar('provider_response_dispatch', default=None)


@contextmanager
def bind_inference_call(call_id):
    """Join adapter-added request capture to the existing canonical call."""
    token = _CALL_ID.set(call_id)
    try:
        yield
    finally:
        _CALL_ID.reset(token)


@contextmanager
def bind_response_dispatch(observer=None):
    """Retain content-free facts across inherited tasks, including cancellation."""
    inherited = _DISPATCH.get()
    state = inherited if inherited is not None and observer is None else {'response': None, 'usage': None, 'observer': observer}
    token = _DISPATCH.set(state)
    try:
        yield state
    finally:
        _DISPATCH.reset(token)


def record_response_dispatch(evidence):
    """Adapters call this after auth/preflight, immediately before dispatch."""
    from api_agent.providers.rich_inference import response_evidence_to_wire
    from api_agent.providers.contracts import ContractError
    response_evidence_to_wire(evidence)
    state = _DISPATCH.get()
    if state is None:
        return
    if state['response'] is not None:
        if state['response'] != evidence:
            raise ContractError('Conflicting dispatched response policy')
        return
    state['response'] = evidence
    if state['observer'] is not None:
        state['observer'](evidence)


def retain_response_failure(error, response, usage=None):
    """Merge admitted dispatch facts without hiding contradictory evidence."""
    if response is None:
        return
    from api_agent.providers.rich_inference import response_evidence_to_wire
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.inference import error_usage
    response_evidence_to_wire(response)
    terminal = getattr(error, 'response', None)
    if terminal is not None and terminal != response:
        conflict = ContractError('Conflicting dispatched response policy')
        conflict.response = response
        conflict.usage = error_usage(error) or usage
        raise conflict
    error.response = response
    if usage is not None and error_usage(error) is None:
        error.usage = usage
        error_usage(error)


def record_response_failure(error):
    """Keep explicitly reported partial usage before existing error wrappers."""
    from api_agent.providers.inference import error_usage
    state = _DISPATCH.get()
    if state is not None and state['response'] is not None:
        usage = error_usage(error)
        if usage is not None:
            state['usage'] = usage


@contextmanager
def collect_inference(boundary=None, call_id=None):
    """Collect only payloads scrubbed inside the provider's auth callback."""
    with bind_response_dispatch() as dispatch:
        collector = {'boundary': boundary, 'call_id': call_id or str(uuid4()), 'rows': [], 'dispatch': dispatch}
        token = _COLLECTOR.set(collector)
        try:
            yield collector
        finally:
            _COLLECTOR.reset(token)


def bind_boundary(boundary):
    collector = _COLLECTOR.get()
    if collector is not None:
        collector['boundary'] = boundary


def capture_rows(rows, call_id):
    """Rebind already scrubbed remote provider phases to the worker visit."""
    owner = current_workflow_visit()
    if owner is None:
        return
    journal, visit_id, _ = owner
    for row in rows:
        direction = 'input' if row['phase'] == 'request' else 'output'
        name = 'llm_request' if direction == 'input' else 'llm_response'
        omitted = row['content'] == {} and row.get('reason') in ('truncated', 'unavailable')
        try:
            journal.capture_json(visit_id=visit_id, direction=direction, cause_id=call_id,
                values={} if omitted else {name: row['content']}, schema={'type': 'object', 'properties': {name: {'type': 'object'}}},
                capture_reason=row.get('reason'))
        except (Exception, asyncio.CancelledError):
            pass


def capture_inference(direction, wire, cause_id, *, auth=None):
    """Scrub before retention/IPC; optional evidence cannot affect inference."""
    from .capture import project_values, capture_reason
    collector = _COLLECTOR.get()
    owner = current_workflow_visit()
    boundary = collector['boundary'] if collector is not None else None
    if boundary is None and owner is not None:
        boundary = getattr(owner[0], 'boundary', None) or getattr(owner[0], '_boundary', None)
    if boundary is None:
        return
    try:
        if type(auth) is str and auth:
            boundary.register_secret(auth)
        report = set()
        allowed = collector is None or boundary.policy.allow_content is True
        name = 'llm_request' if direction == 'input' else 'llm_response'
        projected = project_values({name: wire}, {'type': 'object'}, boundary, report=report) if allowed else {}
        safe = projected.get(name, {})
        row = {'phase': 'request' if direction == 'input' else 'response',
               'content': safe, 'reason': capture_reason(safe, report) if allowed else 'withheld'}
        identifier = collector['call_id'] if collector is not None else _CALL_ID.get() or cause_id
        if collector is not None:
            collector['rows'].append(row)
        else:
            capture_rows((row,), identifier)
    except (Exception, asyncio.CancelledError):
        pass
