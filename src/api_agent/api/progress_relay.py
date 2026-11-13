"""Order application-projected call progress and tool lifecycle frames behind a turn.

Projection runs in one task so a slow projector never delays the workflow. The turn
waits for it before completing, inside the caller's existing timeout budget.
"""
import asyncio
from contextlib import asynccontextmanager

from ..providers.presentation import (
    call_progress, current_progress_projection_scope, progress_projection_scope, tool_progress,
)

QUEUE_SIZE = 16
MAX_FRAMES = 4096
MAX_TEXT_BYTES = 65536


def section(update):
    return update.node if update.attempt is None else f'{update.node}:{update.attempt}'


@asynccontextmanager
async def relay_progress(projector, publish, on_failure, *, on_drop):
    """Bind progress sinks; publish(name, payload) receives frames in workflow order.

    A projector failure or invalid result skips that update and calls on_failure().
    Frames past the queue, count or text byte limits are dropped, never failing the
    turn. Queue overflow calls on_drop() once; producers never wait for capacity.
    """
    pending = asyncio.Queue(maxsize=QUEUE_SIZE)
    frames = text_bytes = 0
    overflow_reported = False

    async def worker():
        nonlocal frames, text_bytes
        while True:
            update, scope = await pending.get()
            try:
                if frames >= MAX_FRAMES:
                    continue
                if type(update) is tuple:
                    frames += 1
                    await publish('tool_' + update[0], {'tool': update[1]})
                    continue
                with progress_projection_scope(scope):
                    text = await projector(update)
                if text is not None and type(text) is not str:
                    raise TypeError('Invalid progress text')
                size = len((text or '').encode('utf-8'))
                if text and text_bytes + size <= MAX_TEXT_BYTES:
                    frames += 1
                    text_bytes += size
                    await publish('reasoning_token', {'content': text, 'section': section(update)})
            except Exception:
                on_failure()
            finally:
                pending.task_done()

    async def enqueue(update, scope=None):
        nonlocal overflow_reported
        try:
            pending.put_nowait((update, scope))
        except asyncio.QueueFull:
            if not overflow_reported:
                overflow_reported = True
                on_drop()
        # Fairness for a ready worker without awaiting projector or queue space.
        await asyncio.sleep(0)

    async def on_call(update):
        await enqueue(update, current_progress_projection_scope())

    async def on_tool(kind, tool):
        await enqueue((kind, tool))

    # Created before the sinks bind, so the projector's own calls are never reported back.
    task = asyncio.create_task(worker())
    try:
        with call_progress(on_call), tool_progress(on_tool):
            yield
        joining = asyncio.create_task(pending.join())
        try:
            done, _ = await asyncio.wait((task, joining), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                # Independent projector cancellation must not strand the answer.
                on_failure()
        finally:
            joining.cancel()
            await asyncio.gather(joining, return_exceptions=True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Release application scopes even when a failed/cancelled worker left a queue.
        while not pending.empty():
            pending.get_nowait()
            pending.task_done()
