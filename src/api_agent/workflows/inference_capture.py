"""Credential-authority provider payload capture and workflow attribution."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from .attribution import current_workflow_visit

_COLLECTOR = ContextVar('provider_payload_collector', default=None)


@contextmanager
def collect_inference(boundary=None, call_id=None):
    """Collect only payloads scrubbed inside the provider's auth callback."""
    collector = {'boundary': boundary, 'call_id': call_id or str(uuid4()), 'rows': []}
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
        identifier = collector['call_id'] if collector is not None else cause_id
        if collector is not None:
            collector['rows'].append(row)
        else:
            capture_rows((row,), identifier)
    except (Exception, asyncio.CancelledError):
        pass
