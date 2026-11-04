"""Versioned, explicit operation outcomes; never infer failure from user content."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID
from threading import Lock


class OutcomeState(StrEnum):
    SUCCESS = 'success'
    FAILURE = 'failure'
    CANCELLED = 'cancelled'
    PARTIAL = 'partial'
    UNKNOWN = 'unknown'


class OutcomeCause(StrEnum):
    PROVIDER = 'provider'
    TOOL = 'tool'
    VALIDATION = 'validation'
    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'
    EXHAUSTED = 'exhausted'
    EXECUTION = 'execution'


@dataclass(frozen=True)
class ExecutionOutcome:
    state: OutcomeState
    cause: OutcomeCause | None = None
    cause_id: str | None = None
    version: int = 1

    def __post_init__(self):
        if (type(self.state) is not OutcomeState or self.version != 1
                or type(self.version) is not int
                or self.cause is not None and type(self.cause) is not OutcomeCause):
            raise ValueError('Invalid execution outcome')
        if self.state in (OutcomeState.FAILURE, OutcomeState.PARTIAL) and self.cause is None:
            raise ValueError('Failure outcome requires a safe cause')
        if self.state == OutcomeState.SUCCESS and self.cause is not None:
            raise ValueError('Success outcome cannot contain a failure cause')
        if self.cause_id is not None:
            if type(self.cause_id) is not str or str(UUID(self.cause_id)) != self.cause_id:
                raise ValueError('Invalid outcome cause identity')


_CURRENT = ContextVar('api_agent_operation_outcome', default=None)


class OperationOutcome:
    """An operation's final result, independent of recoverable child failures."""
    def __init__(self, runtime, span):
        self.runtime, self.span = runtime, span
        self._lock = Lock()
        self.closed = False
        self._response_complete = False
        self.final = ExecutionOutcome(OutcomeState.UNKNOWN)

    def complete_response(self):
        """The validated final response was emitted; a later close is normal."""
        with self._lock:
            if self.closed:
                return
            self._response_complete = True
            if self.final.state == OutcomeState.UNKNOWN:
                self.final = ExecutionOutcome(OutcomeState.SUCCESS)

    def report(self, outcome):
        if type(outcome) is not ExecutionOutcome:
            raise TypeError('Expected a versioned execution outcome')
        with self._lock:
            if self.closed or (self._response_complete and outcome.state == OutcomeState.CANCELLED):
                return
            # Only explicit final reports enter this owner. Recoverable child
            # attempts never report terminal request failure here.
            precedence = {OutcomeState.UNKNOWN: 0, OutcomeState.SUCCESS: 1,
                          OutcomeState.PARTIAL: 2, OutcomeState.CANCELLED: 3,
                          OutcomeState.FAILURE: 4}
            if precedence[outcome.state] > precedence[self.final.state]:
                self.final = outcome

    def finish(self):
        with self._lock:
            self.closed = True
            return self.final



def current_operation(runtime=None):
    owner = _CURRENT.get()
    return owner if owner is not None and not owner.closed and (runtime is None or owner.runtime is runtime) else None


def report_outcome(outcome, *, runtime):
    if type(outcome) is not ExecutionOutcome:
        raise TypeError('Expected a versioned execution outcome')
    if runtime is None:
        return
    owner = current_operation(runtime)
    if owner is not None:
        owner.report(outcome)


def report_response_complete(*, runtime):
    """Explicit delivery acknowledgement for this runtime's current request."""
    if runtime is not None:
        owner = current_operation(runtime)
        if owner is not None:
            owner.complete_response()


@contextmanager
def bind_operation(owner):
    token = _CURRENT.set(owner)
    try:
        yield owner
    finally:
        _CURRENT.reset(token)
