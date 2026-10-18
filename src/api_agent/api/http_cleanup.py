"""Request disposal duties retain acknowledged work and transferred receipts."""
import asyncio
from dataclasses import dataclass

from ..providers.control import CleanupPendingError
from ..providers.progress import independent_timeout_scope, report_timeout


@dataclass
class _Entry:
    action: object
    receipt: object = None
    task: object = None
    done: bool = False
    acknowledged: bool = False


class CallbackState:
    """One callback plus the independently owned duties it transfers."""
    def __init__(self, callback):
        self.entries = [_Entry(callback)]
        self.receipts = {}
        self.lock = asyncio.Lock()
        self.calls = 0

    @property
    def done(self):
        return all(entry.done for entry in self.entries)

    def _failure(self, entry, error):
        unknown = current = interrupted = False
        leaves = [error]
        while leaves:
            leaf = leaves.pop()
            if isinstance(leaf, BaseExceptionGroup):
                leaves.extend(leaf.exceptions)
            elif isinstance(leaf, CleanupPendingError):
                if leaf is entry.receipt:
                    current = True
                elif id(leaf) not in self.receipts:
                    child = _Entry(leaf.retry, leaf)
                    self.receipts[id(leaf)] = child
                    self.entries.append(child)
                elif not self.receipts[id(leaf)].acknowledged:
                    child = self.receipts[id(leaf)]
                    child.done = False
                    if not any(known is child for known in self.entries):
                        self.entries.append(child)
            else:
                unknown = True
                interrupted |= isinstance(leaf, asyncio.CancelledError)
        entry.done = not unknown and not current
        return interrupted

    def start(self, attempted):
        interrupted = False
        for entry in tuple(self.entries):
            if entry.done or id(entry) in attempted:
                continue
            attempted.add(id(entry))
            if entry.task is not None:
                continue
            async def invoke(selected=entry.action):
                return await selected()
            coroutine = invoke()
            try:
                entry.task = asyncio.create_task(coroutine)
            except BaseException as error:
                coroutine.close()
                interrupted |= self._failure(entry, error)
        return interrupted

    def acknowledge_finished(self):
        interrupted = False
        for entry in tuple(self.entries):
            if entry.done or entry.task is None or not entry.task.done():
                continue
            task, entry.task = entry.task, None
            try:
                task.result()
            except BaseException as error:
                interrupted |= self._failure(entry, error)
            else:
                entry.done = entry.acknowledged = True
        return interrupted

    async def settle(self, budget):
        interrupted = False
        if self.lock.locked():
            try:
                async with asyncio.timeout(budget.remaining):
                    await self.lock.acquire()
            except TimeoutError:
                return False
        else:
            await self.lock.acquire()
        try:
            for entry in tuple(self.entries):
                if entry.done or entry.task is None:
                    continue
                while entry.task is not None and not entry.task.done() and budget.remaining > 0:
                    try:
                        await asyncio.wait({entry.task}, timeout=budget.remaining)
                    except asyncio.CancelledError:
                        interrupted = True
            interrupted |= self.acknowledge_finished()
        finally:
            self.lock.release()
        return interrupted


async def close_http_callbacks(callbacks, *, timeout=5):
    """Bound disposal waits without abandoning, cancelling or replaying owned work."""
    interrupted = scope_failed = False
    for index, callback in enumerate(callbacks):
        if not isinstance(callback, CallbackState):
            callbacks[index] = CallbackState(callback)
    receipts = callbacks[0].receipts if callbacks else {}
    for state in callbacks:
        for identifier, entry in state.receipts.items():
            receipts.setdefault(identifier, entry)
    for state in callbacks:
        state.receipts = receipts
    try:
        with independent_timeout_scope():
            async with report_timeout('HTTP request disposal', timeout) as budget:
                attempted = {state: set() for state in callbacks}
                retrying = {state for state in callbacks if state.calls}
                for state in callbacks:
                    state.calls += 1
                while True:
                    for state in tuple(callbacks):
                        interrupted |= state.start(attempted[state])
                    for state in tuple(callbacks):
                        try:
                            interrupted |= await state.settle(budget)
                        except asyncio.CancelledError:
                            interrupted = True
                        if state.done and state in callbacks:
                            callbacks.remove(state)
                    if budget.remaining == 0 or not any(
                            state in retrying and any(not entry.done and entry.task is None
                                                      and id(entry) not in attempted[state]
                                                      for entry in state.entries)
                            for state in callbacks):
                        break
    except BaseException as error:
        scope_failed = True
        interrupted |= isinstance(error, asyncio.CancelledError)
        # Timing setup/teardown is presentation work, but its failed disposal
        # can transfer real ownership. Preserve those children without replaying
        # callbacks which already completed inside the scope.
        for state in tuple(callbacks):
            if not state.lock.locked():
                interrupted |= state.acknowledge_finished()
            if state.done and state in callbacks:
                callbacks.remove(state)
        failure = CallbackState(None)
        failure.receipts = receipts
        interrupted |= failure._failure(failure.entries[0], error)
        failure.entries[0].done = True
        owned = {id(entry) for state in callbacks for entry in state.entries}
        failure.entries = [entry for entry in failure.entries
                           if entry.receipt is not None and id(entry) not in owned]
        if failure.entries:
            callbacks.append(failure)
    entries = {id(entry): entry for state in callbacks for entry in state.entries if not entry.done}
    owners = tuple(owner for entry in entries.values()
                   for owner in (entry.receipt.owners if entry.receipt is not None else (entry.action,)))
    if callbacks or interrupted or scope_failed:
        receipt = CleanupPendingError(owners, lambda: close_http_callbacks(callbacks, timeout=timeout))
        if interrupted:
            error = asyncio.CancelledError()
            error.cleanup_pending = receipt
            raise error
        raise receipt
