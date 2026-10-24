"""Visible provider messages attributed to the active parsed-flow node."""
import asyncio

from .attribution import current_workflow_visit


def capture_inference(direction, wire, cause_id, *, auth=None):
    """Best-effort observation; never retain a request object or affect inference.

    The shared provider boundary has already validated this wire representation.
    Restrict capture to visible messages and tool/format contracts; accounting and
    provider identity retain their existing independent structural observations.
    """
    owner = current_workflow_visit()
    if owner is None:
        return
    journal, visit_id, _ = owner
    capture = getattr(journal, 'capture_json', None)
    if not callable(capture):
        return
    fields = ('messages', 'tools', 'structured') if direction == 'input' else ('text', 'message')
    name = 'llm_request' if direction == 'input' else 'llm_response'
    try:
        if type(auth) is str and auth:
            boundary = getattr(journal, 'boundary', None) or getattr(journal, '_boundary', None)
            register = getattr(boundary, 'register_secret', None)
            if not callable(register):
                return
            register(auth)
        capture(visit_id=visit_id, direction=direction, cause_id=cause_id,
            values={name: {key: wire[key] for key in fields if key in wire}},
            schema={'type': 'object', 'properties': {name: {'type': 'object'}}})
    except (Exception, asyncio.CancelledError):
        # Observation failures must not alter validated provider semantics.
        pass
