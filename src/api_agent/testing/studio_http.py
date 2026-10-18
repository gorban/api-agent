"""Owned loopback Studio HTTP bridge for integration consumers."""
import asyncio
import re
import socket
from contextlib import AsyncExitStack, asynccontextmanager

from api_agent.api import Runtime, create_application
from api_agent.api.studio import StudioBackend
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.runtime.tasks import offload, settle
from api_agent.testing.studio_retained import StudioCleanup

@asynccontextmanager
async def agent_endpoint(provider, selection, origin, nonce, *, listener=None, reasoning_default=None, policy_config=None):
    """Own the real Agent HTTP bridge for the selected UI runtime."""
    import uvicorn
    async with AsyncExitStack() as cleanup:
        duties = StudioCleanup()
        cleanup.push_async_callback(_cleanup, duties.aclose)
        if listener is None:
            listener = socket.socket()
            duties.push(_close_socket, listener)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.setblocking(False)
        endpoint = f'http://127.0.0.1:{listener.getsockname()[1]}'
        from api_agent.testing.studio_retained import RetainedStudio
        retained = isinstance(provider, RetainedStudio)
        if retained:
            duties.push(provider.aclose)
        async def describe(*, auth):
            return await provider.describe() if retained else await offload(provider.describe)
        async def complete(request, *, auth, context):
            call = provider.complete(request) if retained else offload(provider.complete, request)
            return await context.run(call)
        backend = StudioBackend(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
            policy=ModelSelectionPolicy({'LLM_ALLOWED_MODELS': re.escape(selection),
                                         'LLM_DEFAULT_MODEL': re.escape(selection), **(policy_config or {})}),
            origin=origin, nonce=nonce, scrub=lambda _: 'Selected provider rejected the request.',
            reasoning_default=reasoning_default)
        duties.push(backend.aclose)
        runtime = Runtime('demo', services={'studio_backend': backend})
        application = create_application(runtime)
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False, timeout_graceful_shutdown=1))
        # A long-lived listener must not inherit completed UI startup budgets.
        # Each admitted HTTP request still creates its own bounded context.
        with independent_timeout_scope():
            task = asyncio.create_task(server.serve(sockets=[listener]))
        duties.push(_close_server, server, task)
        async with component_timeout('Studio agent readiness', 10):
            while not server.started:
                assert not task.done(), 'Agent listener stopped before readiness.'
                await asyncio.sleep(.01)
        yield endpoint


async def _close_socket(listener):
    listener.close()


async def _cleanup(action, *args):
    """Settle owned work without losing its original interruption receipt."""
    failures = []
    async def dispose():
        try:
            await action(*args)
        except BaseException as error:
            # Return failures as data through finish_cleanup: a task ending in
            # CancelledError otherwise loses the error's ownership attributes.
            failures.append(error)
    try:
        await finish_cleanup(dispose())
    except asyncio.CancelledError as cancelled:
        if failures:
            failure = failures[0]
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            cancelled.cleanup_pending = (failure if isinstance(failure, CleanupPendingError)
                else CleanupPendingError((action,), lambda: _cleanup(action, *args)))
        raise
    if failures:
        raise failures[0]


async def _close_server(server, task):
    async with report_timeout('Studio agent cleanup', 3) as budget:
        server.should_exit = True
        done, _ = await asyncio.wait({task}, timeout=budget.remaining)
        if not done:
            if not task.cancelling():
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            if not done:
                raise CleanupPendingError((server, task), lambda: _close_server(server, task))
        try:
            await settle(task)
        except asyncio.CancelledError:
            pass
