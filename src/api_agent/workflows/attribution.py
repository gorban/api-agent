"""Current node visit for joining terminal provider accounting observations."""
from contextlib import contextmanager
from contextvars import ContextVar

_VISIT = ContextVar('workflow_leaf_visit', default=None)
_CAPTURE = ContextVar('workflow_tool_capture_services', default=None)


@contextmanager
def bind_workflow_visit(journal, visit_id, attempt_id=None, *, capture_services=None):
    from .evidence_finalization import begin, end
    pending_token = begin()
    capture_token = _CAPTURE.set(capture_services)
    token = _VISIT.set((journal, visit_id, attempt_id) if journal is not None else None)
    try:
        yield
    finally:
        _VISIT.reset(token)
        _CAPTURE.reset(capture_token)
        end(pending_token)


def current_workflow_visit():
    return _VISIT.get()


_CALL_RECEIPTS = ContextVar('workflow_terminal_call_receipts', default=None)


@contextmanager
def bind_terminal_call_receipts():
    """Collect bounded structural receipts for this one local provider operation."""
    receipts = []
    token = _CALL_RECEIPTS.set(receipts)
    try:
        yield receipts
    finally:
        _CALL_RECEIPTS.reset(token)


def admit_terminal_call_receipt(row, journal):
    receipts = _CALL_RECEIPTS.get()
    owner = current_workflow_visit()
    if (owner is None or (journal is not owner[0] and journal is not getattr(owner[0], 'journal', None))
            or row.get('visit_id', owner[1]) != owner[1]
            or row.get('attempt_id', owner[2]) != owner[2]):
        return
    prior = next((item for item in receipts or () if item['call_id'] == row['call_id']), None)
    if prior is not None and 'quote' in row:
        if any(prior.get(key) != row.get(key) for key in
               ('selection', 'usage', 'outcome', 'actual_model', 'transport_attempts')) or (
                'quote' in prior and prior['quote'] != row['quote']):
            getattr(journal, 'journal', journal).record_evidence_loss()
            return
        from copy import deepcopy
        prior['quote'] = deepcopy(row['quote'])
    elif receipts is not None and len(receipts) < 128 and prior is None:
        receipts.append({key: row[key] for key in
            ('call_id', 'selection', 'usage', 'outcome', 'actual_model', 'reasoning_effort', 'response', 'transport_attempts', 'quote') if key in row})


def current_tool_capture_services():
    return _CAPTURE.get()
