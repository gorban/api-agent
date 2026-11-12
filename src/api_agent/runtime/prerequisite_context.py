"""Task-scoped borrower capability, independent of resource-pack namespaces."""
from contextlib import contextmanager
from contextvars import ContextVar

_BORROWER = ContextVar('suite_prerequisite_borrower', default=None)


@contextmanager
def bind_prerequisite_borrower(client, scope):
    token = _BORROWER.set((client, scope))
    try:
        yield
    finally:
        _BORROWER.reset(token)


def prerequisite_borrower():
    return _BORROWER.get()
