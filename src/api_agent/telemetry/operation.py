"""Request lifetime and explicit final outcomes across asynchronous delivery."""
import asyncio
import logging
from contextlib import contextmanager

from .outcomes import (ExecutionOutcome, OperationOutcome, OutcomeState,
                       OutcomeCause, bind_operation)


def safe_trace(action):
    """Optional tracing cannot replace execution; diagnostic logging still may fail."""
    try:
        return action()
    except Exception:
        logging.getLogger(__name__).error('Tracing observation failed; execution continues without this observation.')
        return None


@contextmanager
def operation(runtime, *, http=None):
    from .http_tracing import HTTPMetadata
    metadata = http if type(http) is HTTPMetadata else None
    span = safe_trace(lambda: runtime.begin(metadata.name if metadata else 'request',
        metadata.attributes() if metadata else {}, kind='SERVER'))
    owner = OperationOutcome(runtime, span)
    loss_owner = getattr(runtime, 'losses', None)
    loss_start = loss_owner.count if type(loss_owner) is TraceLoss else 0
    with bind_operation(owner):
        try:
            yield owner
        except asyncio.CancelledError:
            owner.report(ExecutionOutcome(OutcomeState.CANCELLED, OutcomeCause.CANCELLED))
            raise
        except BaseException as error:
            owner.report(ExecutionOutcome(OutcomeState.FAILURE,
                OutcomeCause.TIMEOUT if isinstance(error, TimeoutError) else OutcomeCause.EXECUTION))
            raise
        finally:
            final = owner.finish()
            if span is not None:
                if type(loss_owner) is TraceLoss and loss_owner.count > loss_start:
                    safe_trace(lambda: span.capture_loss(loss_owner.count - loss_start))
                safe_trace(lambda: span.outcome(final))
                safe_trace(span.end)


class TraceLoss:
    """Bounded diagnostic owner: count every lost action and warn once."""
    def __init__(self):
        from threading import Lock
        self._lock = Lock()
        self.count = 0
        self._diagnostic_failed = False

    def record(self):
        with self._lock:
            self.count += 1
            first = self.count == 1
            failed = self._diagnostic_failed
        if failed:
            raise OSError('Could not record tracing loss diagnostic.')
        if first:
            try:
                logging.getLogger(__name__).error('Tracing observation was lost; execution continues with incomplete traces.')
            except Exception:
                with self._lock:
                    self._diagnostic_failed = True
                raise OSError('Could not record tracing loss diagnostic.') from None

    def run(self, action):
        try:
            return action()
        except Exception:
            self.record()
            return None
