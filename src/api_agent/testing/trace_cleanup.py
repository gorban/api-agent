"""Trace-only pending disposal is separate from inference or test outcome."""
from api_agent.providers.control import CleanupPendingError


_ADMITTED = object()


class OptionalTraceCleanupPending(CleanupPendingError):
    def __init__(self, owners, retry, *, origin, backend=None):
        self.origin, self.backend = origin, backend
        self._admission = None
        super().__init__(owners, retry)


def optional_trace_cleanup(error):
    """Only a trusted backend receipt can authorize nonfatal outcome handling."""
    from .setup_backend import SetupBackend, _SetupOwner
    return (type(error) is OptionalTraceCleanupPending and type(error.backend) is SetupBackend
            and type(error.origin) is _SetupOwner
            and error._admission is _ADMITTED
            and getattr(error.backend.registry, '_owner', None) is error.origin)


def admit_optional_trace_receipt(backend, origin):
    from .setup_backend import SetupBackend, _SetupOwner
    if (type(backend) is not SetupBackend or type(origin) is not _SetupOwner
            or getattr(backend.registry, '_owner', None) is not origin
            or origin._nontrace_settled is not True or origin._trace_cleanup_failed is not True):
        raise TypeError('Trace cleanup receipt has no stopped provider owner')
    receipt = OptionalTraceCleanupPending((backend,), backend.aclose, origin=origin, backend=backend)
    receipt._admission = _ADMITTED
    return receipt
