"""Bounded caller-scoped chat leases and retryable operation disposal."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import math
import inspect
import time

from api_agent.providers.control import CleanupPendingError, OperationContext, _cancel_once
from api_agent.providers.progress import independent_timeout_scope, report_timeout


def _spawn(coroutine):
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


@dataclass
class Turn:
    owner_id: str
    session_id: str
    context: OperationContext
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = 'running'
    result: object = None
    error: BaseException | None = None
    _heartbeat: float = 0
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _monitor: asyncio.Task | None = None
    _monitor_stopping: bool = False
    _watch_error: BaseException | None = None
    _settlement: asyncio.Task | None = None
    _cancelled: bool = False
    _requested: set = field(default_factory=set)
    _receipt: CleanupPendingError | None = None
    _receipts: list = field(default_factory=list)
    _seen_receipts: dict = field(default_factory=dict)
    _cleanup_task: asyncio.Task | None = None
    _finished: float | None = None
    _lifecycle: object = field(default=None, repr=False, compare=False)
    _diagnostic_errors: tuple = field(default=(), repr=False, compare=False)


class ChatLifecycle:
    """Own tasks and contexts; persistent authentication belongs to the caller.

    All methods run on the creating event loop. Only admitted operations allocate
    a watchdog. A terminal status is published only after disposal settles.
    """
    def __init__(self, *, heartbeat_interval=10, expiry=30, max_active=16,
                 max_terminal=128, cleanup_timeout=5, terminal_ttl=300, on_error=None):
        for value in (heartbeat_interval, expiry, cleanup_timeout, terminal_ttl):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid chat lifetime')
        for value in (max_active, max_terminal):
            if type(value) is not int or value <= 0:
                raise ValueError('Invalid chat capacity')
        if heartbeat_interval >= expiry:
            raise ValueError('Heartbeat must precede expiry')
        self.heartbeat_interval, self.expiry = heartbeat_interval, expiry
        self.max_active, self.max_terminal = max_active, max_terminal
        self.cleanup_timeout, self.terminal_ttl = cleanup_timeout, terminal_ttl
        self._turns = OrderedDict()
        self.closed = False
        self.on_error = on_error

    def report_error(self, error, *, turn=None):
        """Observe failures even after disconnect, within existing turn ownership."""
        if self.on_error is None:
            return
        try:
            has_failure = (error.subgroup(Exception) is not None
                           if isinstance(error, BaseExceptionGroup) else isinstance(error, Exception))
            if not has_failure:
                return
            if turn is not None and any(error is previous for previous in turn._diagnostic_errors):
                return
            self.on_error(error)
        except BaseException:
            # Optional diagnostics never own execution, cancellation or disposal.
            return
        if turn is not None:
            turn._diagnostic_errors = (*turn._diagnostic_errors, error)[-16:]

    def _prune(self):
        terminals = [key for key, turn in self._turns.items() if turn.done.is_set()]
        now = time.monotonic()
        for index, key in enumerate(terminals):
            if (now - self._turns[key]._finished >= self.terminal_ttl
                    or index < len(terminals) - self.max_terminal):
                del self._turns[key]

    def start(self, owner_id, session_id, invoke, *, context):
        self._prune()
        if (self.closed or any(turn.state == 'cleanup_pending' for turn in self._turns.values())
                or sum(not turn.done.is_set() for turn in self._turns.values()) >= self.max_active):
            raise ValueError('Chat admission unavailable')
        if (type(owner_id) is not str or not owner_id or len(owner_id) > 256
                or type(session_id) is not str or not session_id or len(session_id) > 256
                or not isinstance(context, OperationContext) or not callable(invoke)):
            raise ValueError('Invalid chat operation')
        key = owner_id, session_id
        if key in self._turns:
            raise ValueError('Chat session already exists')
        turn = Turn(owner_id, session_id, context, _heartbeat=time.monotonic(), _lifecycle=self)
        self._turns[key] = turn
        try:
            turn.task = _spawn(self._execute(turn, invoke))
        except BaseException:
            del self._turns[key]
            raise
        turn.task.add_done_callback(lambda _: self._execution_done(turn))
        try:
            turn._monitor = _spawn(self._watch(turn))
            turn._monitor.add_done_callback(lambda task: self._watch_done(turn, task))
        except BaseException:
            turn.state, turn._cancelled = 'cleanup_pending', True
            _cancel_once(turn.task, turn._requested)
            raise CleanupPendingError((self, turn), lambda: self._retry(turn)) from None
        return turn

    def _watch_done(self, turn, task):
        error = None if task.cancelled() else task.exception()
        if turn._monitor_stopping or turn.done.is_set():
            return
        turn._watch_error = error
        self.report_error(error, turn=turn)
        turn.state, turn._cancelled = 'cleanup_pending', True
        _cancel_once(turn.task, turn._requested)
        try:
            self._settle_task(turn)
        except CleanupPendingError:
            pass

    def _execution_done(self, turn):
        try:
            self._settle_task(turn)
        except CleanupPendingError:
            pass

    async def _execute(self, turn, invoke):
        invocation = self._invoke(turn, invoke)
        try:
            async with report_timeout('Chat operation', turn.context.remaining() or None):
                turn.result = await turn.context.run(invocation)
        except BaseException as error:
            turn.error = error
            self.report_error(error, turn=turn)
            self._remember_receipts(turn, error)
        finally:
            if inspect.getcoroutinestate(invocation) == inspect.CORO_CREATED:
                invocation.close()

    async def _invoke(self, turn, invoke):
        try:
            return await invoke(turn.context)
        except BaseException as error:
            # Context cancellation/deadline can return before this task. Capture
            # its cleanup ownership before the context consumes its exception.
            self._remember_receipts(turn, error)
            self.report_error(error, turn=turn)
            raise

    def _remember_receipts(self, turn, error):
        if isinstance(error, BaseExceptionGroup):
            captured = [self._remember_receipts(turn, child) for child in error.exceptions]
            return all(captured)
        if isinstance(error, CleanupPendingError):
            if error is turn._receipt:
                if not any(queued is error for queued in turn._receipts):
                    turn._receipts.append(error)
            elif id(error) not in turn._seen_receipts:
                turn._seen_receipts[id(error)] = error
                turn._receipts.append(error)
            return True
        return False

    async def _watch(self, turn):
        with independent_timeout_scope():
            while True:
                delay = max(0, self.expiry - (time.monotonic() - turn._heartbeat))
                if delay == 0:
                    turn._cancelled = True
                    _cancel_once(turn.task, turn._requested)
                    self._settle_task(turn)
                    return
                turn._wake.clear()
                async with report_timeout('Chat heartbeat lease', delay):
                    try:
                        await asyncio.wait_for(turn._wake.wait(), delay)
                    except TimeoutError:
                        pass

    def _settle_task(self, turn):
        if turn._settlement is None or (turn._settlement.done() and not turn.done.is_set()):
            try:
                turn._settlement = _spawn(self._settle(turn))
            except BaseException:
                turn.state = 'cleanup_pending'
                raise CleanupPendingError((self, turn), lambda: self._retry(turn)) from None
        return turn._settlement

    async def _settle(self, turn):
        try:
            with independent_timeout_scope():
                async with report_timeout('Chat operation disposal', self.cleanup_timeout) as budget:
                    done, _ = await asyncio.wait({turn.task}, timeout=budget.remaining)
                    if not done:
                        raise CleanupPendingError((turn,), lambda: self._retry(turn))
                    await turn.context.aclose(timeout=budget.remaining)
                    while turn._receipt is not None or turn._receipts:
                        if turn._receipt is None:
                            turn._receipt = turn._receipts.pop(0)
                        if turn._cleanup_task is None:
                            turn._cleanup_task = _spawn(turn._receipt.retry())
                        settled, _ = await asyncio.wait({turn._cleanup_task}, timeout=budget.remaining)
                        if not settled:
                            raise CleanupPendingError((turn,), lambda: self._retry(turn))
                        cleanup_task, turn._cleanup_task = turn._cleanup_task, None
                        try:
                            cleanup_task.result()
                        except CleanupPendingError as error:
                            turn._seen_receipts[id(error)] = error
                            turn._receipt = error
                            raise
                        except BaseExceptionGroup as error:
                            transferred = self._remember_receipts(turn, error)
                            current_queued = any(queued is turn._receipt for queued in turn._receipts)
                            if not transferred or current_queued:
                                # Ordinary failures do not acknowledge the current
                                # duty. Dispatch named children before retrying it.
                                turn._receipts = [queued for queued in turn._receipts
                                                  if queued is not turn._receipt]
                                turn._receipts.append(turn._receipt)
                            turn._receipt = None
                            raise
                        turn._receipt = None
                    if turn._monitor is not None and not turn._monitor.done():
                        turn._monitor_stopping = True
                        turn._monitor.cancel()
                        await asyncio.gather(turn._monitor, return_exceptions=True)
        except BaseException as error:
            self.report_error(error, turn=turn)
            turn.state = 'cleanup_pending'
            return
        turn.state = ('cancelled' if turn._cancelled or isinstance(turn.error, asyncio.CancelledError)
                      else 'failed' if turn.error is not None else 'completed')
        turn._finished = time.monotonic()
        turn.done.set()
        self._prune()

    async def _retry(self, turn):
        await asyncio.shield(self._settle_task(turn))
        if not turn.done.is_set():
            raise CleanupPendingError((self, turn), lambda: self._retry(turn))

    def heartbeat(self, owner_id, session_id):
        self._prune()
        turn = self._turns.get((owner_id, session_id))
        if turn is None:
            return 'unknown'
        if turn.state == 'running' and not turn._cancelled:
            now = time.monotonic()
            if now - turn._heartbeat >= self.expiry:
                turn.state, turn._cancelled = 'cleanup_pending', True
                _cancel_once(turn.task, turn._requested)
                self._settle_task(turn)
            else:
                turn._heartbeat = now
                turn._wake.set()
        return turn.state

    async def wait(self, owner_id, session_id):
        return await self.wait_turn(owner_id, self._turns[(owner_id, session_id)])

    def _validate_turn(self, owner_id, turn):
        if not isinstance(turn, Turn) or turn._lifecycle is not self or turn.owner_id != owner_id:
            raise ValueError('Chat turn ownership mismatch')

    async def wait_turn(self, owner_id, turn):
        """Wait for the captured admitted operation without resolving its key."""
        self._validate_turn(owner_id, turn)
        if not turn.task.cancelled():
            try:
                await asyncio.shield(turn.task)
            except asyncio.CancelledError:
                if not turn.task.cancelled():
                    raise
        await self._retry(turn)
        return turn

    async def cancel(self, owner_id, session_id):
        self._prune()
        turn = self._turns.get((owner_id, session_id))
        if turn is None:
            return 'unknown'
        if not turn.done.is_set():
            turn._cancelled = True
            _cancel_once(turn.task, turn._requested)
            await self._retry(turn)
        return turn.state

    async def disconnect_turn(self, owner_id, turn):
        """Dispose a captured response's exact turn, even after status eviction.

        A response owner must retain this handle rather than resolve its session
        again: terminal retention can expire before that response is closed.
        """
        self._validate_turn(owner_id, turn)
        if not turn.done.is_set():
            turn._cancelled = True
            _cancel_once(turn.task, turn._requested)
            await self._retry(turn)
        return turn.state

    async def disconnect(self, owner_id, session_id):
        return await self.cancel(owner_id, session_id)

    async def aclose(self):
        self.closed = True
        turns = tuple(self._turns.values())
        # Signal siblings before any resistant operation consumes a disposal
        # budget. Each retained context is then drained by its own settlement.
        for turn in turns:
            if not turn.done.is_set():
                turn._cancelled = True
                _cancel_once(turn.task, turn._requested)
        pending = False
        for turn in turns:
            try:
                await self.cancel(turn.owner_id, turn.session_id)
            except CleanupPendingError:
                pending = True
        if pending:
            raise CleanupPendingError((self,), self.aclose)
