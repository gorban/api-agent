"""Versioned, explicit operation outcomes; never infer failure from user content."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID
from threading import Lock, RLock
from weakref import WeakValueDictionary, ref


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


_FAILURE_CAUSES = {'schema_validation': OutcomeCause.VALIDATION,
    'candidate_validation': OutcomeCause.VALIDATION, 'contract_validation': OutcomeCause.VALIDATION,
    'workflow': OutcomeCause.EXECUTION, 'provider': OutcomeCause.PROVIDER,
    'deadline': OutcomeCause.TIMEOUT, 'cancelled': OutcomeCause.CANCELLED, 'execution': OutcomeCause.EXECUTION}
FAILURE_CODES = frozenset(_FAILURE_CAUSES)


@dataclass(frozen=True)
class FailureOrigin:
    """Explicit trusted call boundary receipt, not inferred exception metadata."""
    cause: OutcomeCause
    code: str
    cause_id: str | None = None

    def __post_init__(self):
        if (type(self.cause) is not OutcomeCause or type(self.code) is not str
                or _FAILURE_CAUSES.get(self.code) != self.cause):
            raise ValueError('Invalid failure classification receipt')
        if self.cause_id is not None and (type(self.cause_id) is not str
                or str(UUID(self.cause_id)) != self.cause_id):
            raise ValueError('Invalid failure call identity')


@dataclass(frozen=True, eq=False)
class _IssuedOrigin:
    error: BaseException
    origins: tuple
    overflow: bool = False


# Weak values do not retain exceptions or their tracebacks after their owners
# release them. Identity, rather than value equality, establishes issuance.
_ISSUED_ORIGINS = WeakValueDictionary()
_ORIGIN_LOCK = RLock()
_FAILURE_CALL = ContextVar('api_agent_failure_call', default=None)


@contextmanager
def failure_call():
    """One actual logical invocation shared by its child tasks and offloads."""
    token = _FAILURE_CALL.set(object())
    try:
        yield
    finally:
        _FAILURE_CALL.reset(token)


def _origin_owner():
    import asyncio
    from threading import current_thread
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return task if task is not None else current_thread()


def _issued_origin(error):
    try:
        receipt = getattr(error, '_api_agent_failure_origin', None)
    except Exception:
        return None
    with _ORIGIN_LOCK:
        if (type(receipt) is _IssuedOrigin and _ISSUED_ORIGINS.get(id(receipt)) is receipt
                and receipt.error is error):
            return receipt
    return None


def _failure_origin(error):
    receipt = _issued_origin(error)
    if receipt is None or receipt.overflow:
        return None
    owner = _origin_owner()
    for issuer, group, origin in receipt.origins:
        if issuer() is owner:
            return origin
    if len({group for _, group, _ in receipt.origins}) != 1:
        return None
    origins = {origin for _, _, origin in receipt.origins}
    admitted = {origin for origin in origins if origin.cause_id is not None}
    if len(admitted) == 1 and len({(origin.cause, origin.code) for origin in origins}) == 1:
        return next(iter(admitted))
    return next(iter(origins)) if len(origins) == 1 else None


def _retain_origin(error, origin):
    with _ORIGIN_LOCK:
        prior = _issued_origin(error)
        owner = _origin_owner()
        origins = tuple((issuer, group, value) for issuer, group, value in prior.origins if issuer() is not owner) if prior else ()
        overflow = bool(prior and prior.overflow) or len(origins) >= 64
        group = _FAILURE_CALL.get() or object()
        receipt = _IssuedOrigin(error, () if overflow else (*origins, (ref(owner), group, origin)), overflow)
        _ISSUED_ORIGINS[id(receipt)] = receipt
        try:
            error._api_agent_failure_origin = receipt
        except Exception:
            # Optional correlation cannot replace an exception that rejects metadata.
            pass


def retain_failure(error, *, cause_id=None, provider=False):
    """Bind known provider ownership and, when supplied, its canonical call ID."""
    from api_agent.providers.control import OperationalError
    if not isinstance(error, BaseException) or type(provider) is not bool:
        raise TypeError('Expected an exception and explicit provider ownership')
    prior = None if provider else _failure_origin(error)
    diagnostic = _classify_failure(error, deadline_expired=False, origin=prior)
    if provider and isinstance(error, OperationalError) and diagnostic['code'] == 'execution':
        diagnostic = {'cause': 'provider', 'code': 'provider'}
    origin = FailureOrigin(OutcomeCause(diagnostic['cause']), diagnostic['code'],
        cause_id if cause_id is not None else prior.cause_id if prior is not None else None)
    _retain_origin(error, origin)
    return error


def inherit_failure(error, source):
    """Preserve an explicit failure receipt across a deliberate error wrapper."""
    if not isinstance(error, BaseException) or not isinstance(source, BaseException):
        raise TypeError('Expected failure exceptions')
    origin = _failure_origin(source)
    if origin is None:
        diagnostic = _classify_failure(source, deadline_expired=False, origin=None)
        origin = FailureOrigin(OutcomeCause(diagnostic['cause']), diagnostic['code'])
    _retain_origin(error, origin)
    return error


def failure_diagnostic(error, *, deadline_expired=False):
    """Fixed classifications only; messages, stacks and provider codes stay private."""
    return _classify_failure(error, deadline_expired=deadline_expired, origin=_failure_origin(error))


def _classify_failure(error, *, deadline_expired, origin):
    import asyncio
    from api_agent.providers.contracts import CandidateOutputError, ContractError, ProviderAccessError
    from api_agent.providers.control import OperationalError
    from api_agent.workflows.execution import CandidateCorrection, WorkflowError, _SchemaRejection
    if type(deadline_expired) is not bool:
        raise TypeError('Expected an explicit deadline classification')
    if deadline_expired or isinstance(error, TimeoutError) or (
            isinstance(error, OperationalError) and error.kind == 'deadline'):
        cause, code = OutcomeCause.TIMEOUT, 'deadline'
    elif isinstance(error, asyncio.CancelledError):
        cause, code = OutcomeCause.CANCELLED, 'cancelled'
    elif origin is not None:
        cause, code = origin.cause, origin.code
    elif isinstance(error, (CandidateOutputError, _SchemaRejection)) or (
            isinstance(error, ProviderAccessError) and error.origin == 'local_output_validation'):
        cause, code = OutcomeCause.VALIDATION, 'schema_validation'
    elif isinstance(error, CandidateCorrection):
        cause, code = OutcomeCause.VALIDATION, 'candidate_validation'
    elif isinstance(error, ContractError):
        cause, code = OutcomeCause.VALIDATION, 'contract_validation'
    elif isinstance(error, WorkflowError):
        cause, code = OutcomeCause.EXECUTION, 'workflow'
    elif isinstance(error, ProviderAccessError):
        cause, code = OutcomeCause.PROVIDER, 'provider'
    else:
        cause, code = OutcomeCause.EXECUTION, 'execution'
    return {'version': 1, 'cause': cause.value, 'code': code}


def failure_outcome(error, *, deadline_expired=False):
    """Classify one escaping failure without promoting recoverable child errors."""
    origin = _failure_origin(error)
    diagnostic = _classify_failure(error, deadline_expired=deadline_expired, origin=origin)
    cause = OutcomeCause(diagnostic['cause'])
    return ExecutionOutcome(OutcomeState.CANCELLED if cause == OutcomeCause.CANCELLED
                            else OutcomeState.FAILURE, cause, origin.cause_id if origin is not None else None)


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


def report_failure(error, *, observations=None, scope_ids=(), terminal=False,
                   deadline_expired=False):
    """Report only owned open scopes; only terminal graph failure reaches a request."""
    outcome = failure_outcome(error, deadline_expired=deadline_expired)
    if observations is not None and hasattr(observations, 'report_failure'):
        from .operation import safe_trace
        for scope_id in scope_ids:
            safe_trace(lambda: observations.report_failure(scope_id, error,
                deadline_expired=deadline_expired))
    if terminal:
        runtime = getattr(observations, 'trace', None)
        owner = current_operation(runtime) if runtime is not None else None
        if owner is not None:
            owner.report(outcome)
            if owner.final is outcome:
                from .operation import describe_failure
                describe_failure(owner.span, error, deadline_expired=deadline_expired)
    return outcome


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
