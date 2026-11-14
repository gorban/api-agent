"""Order application-projected step, call progress, answer and tool lifecycle frames behind a turn.

Projection runs in one task so a slow projector never delays the workflow. The turn
waits for it before completing, inside the caller's existing timeout budget.
"""
import asyncio
from contextlib import asynccontextmanager

from dataclasses import replace

from ..providers.presentation import (
    AnswerProgress, StepProgress, answer_progress, call_progress, current_progress_projection_scope,
    progress_projection_scope, tool_progress,
)

QUEUE_SIZE = 16
MAX_FRAMES = 4096
MAX_TEXT_BYTES = 65536
MAX_ANSWER_BYTES = 131072
MAX_HELD_ANSWERS = 64


def section(update):
    return update.node if update.attempt is None else f'{update.node}:{update.attempt}'


@asynccontextmanager
async def relay_progress(projector, publish, on_failure, *, on_drop):
    """Bind progress sinks; publish(name, payload) receives frames in workflow order.

    A step start's projected label opens its section; the step's end closes a section
    that a label or projected text opened. A projector failure or invalid result
    skips that update and calls on_failure(). Answer text streams as `token` frames
    without the projector; deltas that find the queue full are held in order, a
    delta joining the previous one of the same section, instead of being dropped.
    Frames past the queue, count or text byte limits are dropped, never failing the
    turn. Queue overflow calls on_drop() once; producers never wait for capacity.
    """
    pending = asyncio.Queue(maxsize=QUEUE_SIZE)
    frames = text_bytes = answer_bytes = 0
    overflow_reported = False
    opened = set()
    held = []

    def report_drop():
        nonlocal overflow_reported
        if not overflow_reported:
            overflow_reported = True
            on_drop()

    def release_held():
        while held and not pending.full():
            pending.put_nowait((held.pop(0), None))

    async def worker():
        nonlocal frames, text_bytes, answer_bytes
        while True:
            update, scope = await pending.get()
            try:
                if frames >= MAX_FRAMES:
                    continue
                if type(update) is tuple:
                    frames += 1
                    await publish('tool_' + update[0], {'tool': update[1]})
                    continue
                if type(update) is AnswerProgress:
                    size = len(update.text.encode('utf-8'))
                    if answer_bytes + size > MAX_ANSWER_BYTES:
                        # Later text cannot follow a gap; the final answer frame restores it.
                        answer_bytes = MAX_ANSWER_BYTES + 1
                        report_drop()
                        continue
                    frames += 1
                    answer_bytes += size
                    await publish('token', {'content': update.text, 'section': section(update),
                                            'presentation': update.presentation})
                    continue
                if type(update) is StepProgress and update.phase == 'end':
                    if section(update) in opened:
                        opened.discard(section(update))
                        frames += 1
                        await publish('reasoning_section_end', {'section': section(update)})
                    continue
                with progress_projection_scope(scope):
                    text = await projector(update)
                if text is not None and type(text) is not str:
                    raise TypeError('Invalid progress text')
                size = len((text or '').encode('utf-8'))
                if text and text_bytes + size <= MAX_TEXT_BYTES:
                    frames += 1
                    text_bytes += size
                    opened.add(section(update))
                    name = 'reasoning_section' if type(update) is StepProgress else 'reasoning_token'
                    await publish(name, {'content': text, 'section': section(update)})
            except Exception:
                on_failure()
            finally:
                pending.task_done()
                release_held()

    async def enqueue(update, scope=None):
        try:
            pending.put_nowait((update, scope))
        except asyncio.QueueFull:
            report_drop()
        # Fairness for a ready worker without awaiting projector or queue space.
        await asyncio.sleep(0)

    async def on_call(update):
        await enqueue(update, current_progress_projection_scope())

    async def on_tool(kind, tool):
        await enqueue((kind, tool))

    async def on_answer(update):
        last = held[-1] if held else None
        if last is not None and (last.node, last.attempt, last.presentation) == (
                update.node, update.attempt, update.presentation):
            held[-1] = replace(last, text=last.text + update.text)
        elif held or pending.full():
            if len(held) < MAX_HELD_ANSWERS:
                held.append(update)
            else:
                report_drop()
        else:
            pending.put_nowait((update, None))
        await asyncio.sleep(0)

    # Created before the sinks bind, so the projector's own calls are never reported back.
    task = asyncio.create_task(worker())
    try:
        with call_progress(on_call), tool_progress(on_tool), answer_progress(on_answer):
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
