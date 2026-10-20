"""Current node visit for joining terminal provider accounting observations."""
from contextlib import contextmanager
from contextvars import ContextVar

_VISIT = ContextVar('workflow_leaf_visit', default=None)


@contextmanager
def bind_workflow_visit(journal, visit_id, attempt_id=None):
    token = _VISIT.set((journal, visit_id, attempt_id) if journal is not None else None)
    try:
        yield
    finally:
        _VISIT.reset(token)


def current_workflow_visit():
    return _VISIT.get()
