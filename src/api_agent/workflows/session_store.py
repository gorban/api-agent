"""Where a workflow's committed ``Session`` lives between turns."""
import asyncio
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionSnapshot:
    value: dict
    version: str | None
    saved_at: float | None


class SessionConflict(Exception):
    """expected_version no longer matches the stored version."""


@runtime_checkable
class WorkflowSessionStore(Protocol):
    async def load(self, thread_id: str) -> SessionSnapshot: ...

    async def save(self, thread_id: str, value: dict, *, expected_version: str | None) -> str: ...


# Locks are keyed on the checkpointer itself, so every store over it shares them, and on the
# running loop, because an asyncio lock cannot cross loops. A thread's entry lives only while
# a save holds or awaits it, and a checkpointer's entry goes with the checkpointer.
_LOCKS = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _thread_lock(checkpointer, thread):
    loops = _LOCKS.setdefault(checkpointer, weakref.WeakKeyDictionary())
    threads = loops.setdefault(asyncio.get_running_loop(), {})
    entry = threads.setdefault(thread, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            del threads[thread]


class CheckpointerSessionStore:
    """The session checkpointed by a one-node graph, one thread per ``graph_id:thread_id``.

    Single-process only: saves to the same thread through one checkpointer are serialized
    in this process, whichever store or workflow made them, so the version check and the
    write are atomic here, but a checkpointer shared by several processes has no
    compare-and-swap. Deployments that need ``SessionConflict``
    across processes must supply a store whose ``save`` is atomic (a conditional write).
    """

    def __init__(self, session, graph_id):
        self.session, self.graph_id = session, graph_id

    def _config(self, thread_id):
        # Graph IDs never contain ':', so the prefix keeps graphs that share a checkpointer apart.
        return {'configurable': {'thread_id': f'{self.graph_id}:{thread_id}'}}

    async def load(self, thread_id):
        snapshot = await self.session.aget_state(self._config(thread_id))
        saved_at = None if snapshot.created_at is None else datetime.fromisoformat(snapshot.created_at).timestamp()
        return SessionSnapshot(snapshot.values.get('session', {}),
                               snapshot.config['configurable'].get('checkpoint_id'), saved_at)

    async def save(self, thread_id, value, *, expected_version):
        async with _thread_lock(self.session.checkpointer, self._config(thread_id)['configurable']['thread_id']):
            if (await self.load(thread_id)).version != expected_version:
                raise SessionConflict(thread_id)
            # The version is the checkpoint this write produced, not a later re-read.
            version = None
            async for event in self.session.astream({'session': value}, self._config(thread_id),
                                                    stream_mode='checkpoints'):
                version = event['config']['configurable']['checkpoint_id']
            return version
