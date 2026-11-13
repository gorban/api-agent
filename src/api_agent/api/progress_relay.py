"""Order application-projected call progress and tool lifecycle frames behind a turn.

Projection runs in one task so a slow projector never delays the workflow. The turn
waits for it before completing, inside the caller's existing timeout budget.
"""
import asyncio
from contextlib import asynccontextmanager

from ..providers.presentation import call_progress, tool_progress

QUEUE_SIZE = 16
MAX_FRAMES = 4096
MAX_TEXT_BYTES = 65536


def section(update):
    return update.node if update.attempt is None else f'{update.node}:{update.attempt}'


@asynccontextmanager
async def relay_progress(projector, publish, on_failure):
    """Bind progress sinks; publish(name, payload) receives frames in workflow order.

    A projector failure or invalid result skips that update and calls on_failure().
    Frames past the count or text byte limits are dropped, never failing the turn.
    """
    pending = asyncio.Queue(maxsize=QUEUE_SIZE)
    frames = text_bytes = 0

    async def worker():
        nonlocal frames, text_bytes
        while True:
            update = await pending.get()
            try:
                if frames >= MAX_FRAMES:
                    continue
                if type(update) is tuple:
                    frames += 1
                    await publish('tool_' + update[0], {'tool': update[1]})
                    continue
                try:
                    text = await projector(update)
                    if text is not None and type(text) is not str:
                        raise TypeError('Invalid progress text')
                except Exception:
                    on_failure()
                    continue
                size = len((text or '').encode('utf-8'))
                if text and text_bytes + size <= MAX_TEXT_BYTES:
                    frames += 1
                    text_bytes += size
                    await publish('reasoning_token', {'content': text, 'section': section(update)})
            finally:
                pending.task_done()

    async def on_call(update):
        await pending.put(update)

    async def on_tool(kind, tool):
        await pending.put((kind, tool))

    # Created before the sinks bind, so the projector's own calls are never reported back.
    task = asyncio.create_task(worker())
    try:
        with call_progress(on_call), tool_progress(on_tool):
            yield
        await pending.join()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
