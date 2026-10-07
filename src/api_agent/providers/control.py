"""Safe operation failures and a single monotonic operation budget."""
import asyncio
import inspect
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")
KINDS = frozenset({"authentication", "authorization", "unavailable", "rate_limited", "unsupported", "invalid_request", "deadline", "unknown"})
EXECUTIONS = frozenset({"not_started", "possibly_executed", "completed"})


class OperationalError(RuntimeError):
    """Safe classification; retry decisions belong solely to the operation owner."""
    def __init__(self, kind: str, execution: str = "not_started", retry_after: float | None = None):
        if type(kind) is not str or type(execution) is not str or kind not in KINDS or execution not in EXECUTIONS:
            raise ValueError("invalid operational classification")
        if retry_after is not None and (type(retry_after) not in {int, float} or not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("invalid retry delay")
        self.kind = kind
        self.execution = "possibly_executed" if kind == "unknown" else execution
        self.retry_after = retry_after
        super().__init__(kind)


class CleanupPendingError(RuntimeError):
    """Disposal remains pending; retained owners and an explicit retry survive."""
    def __init__(self, owners: tuple[object, ...], retry: Callable[..., Awaitable[None]]):
        self.owners = tuple(owners)
        self.retry = retry
        super().__init__("cleanup pending")


@dataclass(frozen=True)
class OperationContext:
    correlation_id: str
    deadline: float | None = None
    _tasks: set[asyncio.Future] = field(default_factory=set, init=False, repr=False, compare=False)

    _cancel_requested: set[asyncio.Future] = field(default_factory=set, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.correlation_id, str) or not self.correlation_id:
            raise ValueError("correlation id required")
        if self.deadline is not None and (type(self.deadline) not in {int, float} or not math.isfinite(self.deadline)):
            raise ValueError("invalid deadline")

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    @property
    def retained_tasks(self) -> tuple[asyncio.Future, ...]:
        return tuple(self._tasks)

    def _finished(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        self._cancel_requested.discard(task)
        if not task.cancelled():
            task.exception()

    def _cancel(self, task: asyncio.Future) -> None:
        if task in self._cancel_requested:
            return
        self._cancel_requested.add(task)
        if isinstance(task, asyncio.Task) and task.cancelling():
            return
        task.cancel()

    async def aclose(self, timeout: float = 1.0) -> None:
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid cleanup budget")
        if not self._tasks:
            return
        tasks = tuple(self._tasks)
        for task in tasks:
            self._cancel(task)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise CleanupPendingError((self,), self.aclose)

    async def run(self, awaitable: Awaitable[T]) -> T:
        remaining = self.remaining()
        if remaining == 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            elif isinstance(awaitable, asyncio.Future):
                self._tasks.add(awaitable)
                awaitable.add_done_callback(self._finished)
                self._cancel(awaitable)
            raise OperationalError("deadline", execution="possibly_executed" if isinstance(awaitable, asyncio.Future) else "not_started")
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            done, _ = await asyncio.wait((task,), timeout=remaining)
        except asyncio.CancelledError:
            self._cancel(task)
            raise
        if done:
            return task.result()
        self._cancel(task)
        raise OperationalError("deadline", execution="possibly_executed")
