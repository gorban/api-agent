"""Drain owned asynchronous and threaded side effects before returning cancellation."""
import asyncio


async def settle(task):
    """Finish an owned side effect despite repeated caller cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def offload(action, *args, cancellation=None, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(action, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancellation is not None:
            cancellation.set()
        try:
            await settle(task)
        except BaseException:
            # The original interruption remains the operation outcome; the
            # manager has finished its rollback before control leaves here.
            pass
        raise
