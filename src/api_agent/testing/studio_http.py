"""Owned loopback Studio HTTP bridge for integration consumers."""
import asyncio
import re
import secrets
import socket
import sys
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager

from api_agent.api import Runtime, create_application
from api_agent.api.studio import StudioBackend
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.runtime.tasks import offload, settle
from api_agent.testing.studio_retained import StudioCleanup

_DEFAULT_TIMEOUT = object()

@asynccontextmanager
async def agent_endpoint(provider, selection, origin, nonce, *, listener=None, reasoning_default=None, policy_config=None, timeout=_DEFAULT_TIMEOUT, application=None, tracing=None):
    """Own the real Agent HTTP bridge for the selected UI runtime."""
    import uvicorn
    graph_timeout = getattr(application, 'timeouts', {}).get('GRAPH')
    if timeout is _DEFAULT_TIMEOUT:
        timeout = graph_timeout if graph_timeout is not None else 120
    async with AsyncExitStack() as cleanup:
        duties = StudioCleanup()
        cleanup.push_async_exit(lambda kind, value, traceback: _endpoint_cleanup(duties, value))
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
            selections = await provider.describe() if retained else await offload(provider.describe)
            if tracing is not None:
                tracing.bind_catalog(selections)
            return selections
        async def provider_complete(request, *, context):
            call = provider.complete(request) if retained else offload(provider.complete, request)
            return await context.run(call)
        async def complete(request, *, auth, context):
            if application is None:
                return await provider_complete(request, context=context)
            from types import SimpleNamespace
            return await application.complete(request,
                llm=SimpleNamespace(complete=provider_complete), context=context)
        backend = StudioBackend(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
            policy=ModelSelectionPolicy({'LLM_ALLOWED_MODELS': re.escape(selection),
                                         'LLM_DEFAULT_MODEL': re.escape(selection), **(policy_config or {})}),
            origin=origin, nonce=nonce, scrub=lambda _: 'Selected provider rejected the request.',
            reasoning_default=reasoning_default, timeout=timeout,
            on_error=lambda error: _execution_error(provider, nonce, error), trace_owner=tracing)
        duties.push(backend.aclose)
        if tracing is not None:
            tracing.bind_http(origin, nonce, backend=backend)
        runtime = Runtime('demo', services={'studio_backend': backend}, tracing=tracing)
        duties.push(_close_runtime, runtime)
        asgi_application = create_application(runtime)
        server = uvicorn.Server(uvicorn.Config(asgi_application, log_level='error', access_log=False, timeout_graceful_shutdown=1))
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


_TRACE_RECEIPT = object()


class StudioTraceCleanupReceipt(CleanupPendingError):
    """Only an owned Studio runtime may classify its stopped trace-only duty."""
    def __init__(self, owners, retry, *, runtime, admission=None):
        self.runtime, self._admission = runtime, admission
        super().__init__(owners, retry)


def studio_trace_pending(error):
    from .studio_tracing import StudioTraceOwner
    return (type(error) is StudioTraceCleanupReceipt and error._admission is _TRACE_RECEIPT
            and type(error.runtime) is Runtime and type(error.runtime.tracing) is StudioTraceOwner
            and error.runtime.tracing._cleanup_fatal is False)


async def _close_runtime(runtime):
    try:
        await runtime.aclose()
    except CleanupPendingError as error:
        from .studio_tracing import StudioTraceOwner
        trace = runtime.tracing
        if (type(trace) is StudioTraceOwner and error.owners == (trace,)
                and trace._cleanup_fatal is False):
            raise StudioTraceCleanupReceipt((runtime,), runtime.aclose, runtime=runtime,
                                            admission=_TRACE_RECEIPT) from None
        raise


async def _endpoint_cleanup(duties, primary):
    try:
        await _cleanup(duties.aclose)
    except BaseException as error:
        if studio_trace_pending(duties.pending):
            error = StudioTraceCleanupReceipt((duties,), duties.aclose,
                runtime=duties.pending.runtime, admission=_TRACE_RECEIPT)
        if primary is not None:
            from .suite_lock import _cleanup_receipts
            previous = tuple(_cleanup_receipts(primary))
            if previous:
                following = getattr(primary, 'cleanup_pending', None)
                receipts = tuple({id(receipt): receipt for receipt in
                                  (*previous, *_cleanup_receipts(error))}.values())
                joined = StudioCleanup(tuple((lambda selected=receipt: selected.retry())
                                             for receipt in receipts))
                pending = CleanupPendingError(receipts, joined.aclose)
                pending.cleanup_pending = (following if isinstance(primary, CleanupPendingError)
                                           else previous[0])
                error = pending
            primary.cleanup_pending = error
            return False
        raise error
    return False


def _execution_error(provider, nonce, error):
    from .diagnostics import write_exception_diagnostic
    owner = getattr(provider, 'manual', provider)
    def write(text):
        if sys.stderr.write(text) != len(text):
            raise OSError('Could not record Studio execution diagnostic.')
        sys.stderr.flush()
    write_exception_diagnostic(error, write, authority=getattr(owner, 'emissions', None),
        secret_values=(nonce,), label='Studio execution failed')


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


def studio_backend_factory(llm_provider, llm_selection, *, backend_config=None, application=None):
    """Keep one backend owner, with optional test-local defaults."""
    retained = []
    @asynccontextmanager
    async def backend(values):
        values = {**values, **(backend_config or {})}
        nonce = secrets.token_urlsafe(32)
        provider = llm_provider
        if values.get('DEBUG') is True:
            from api_agent.testing.provider_setup import _read_snapshot, _worker_manager_registry
            from api_agent.testing.studio_retained import RetainedStudio, retained_provider_factory
            from api_agent.testing.inspection_gate import manual_accounting_ready
            data = _read_snapshot()
            provider = RetainedStudio(llm_provider,
                retained_provider_factory(data, _worker_manager_registry(data)), manual_accounting_ready)
            retained.append(provider)
        tracing = None
        from api_agent.runtime.tracing_services import tracing_requested
        if tracing_requested(values):
            from .provider_setup import _read_snapshot
            from .studio_tracing import StudioTraceOwner, collector_from_snapshot
            from api_agent.workflows.capture_authority import resolve_capture_services
            snapshot = _read_snapshot()
            tracing = StudioTraceOwner(values,snapshot.get('capture_sources',{}),provider,
                owner_root=Path(snapshot['owner_root']), collector_snapshot=snapshot,
                capture_services=resolve_capture_services({}),
                collector_factory=lambda selected:collector_from_snapshot(snapshot,selected))
        async with agent_endpoint(provider, llm_selection,
                                  values['UI_LISTENER_URL'], nonce,
                                  reasoning_default=values.get('LLM_REASONING_EFFORT') or None,
                                  policy_config={key: values[key] for key in
                                    ('LLM_ALLOWED_MODELS', 'LLM_DEFAULT_MODEL') if key in values},
                                  application=application, tracing=tracing) as endpoint:
            try:
                yield {'agent_url': endpoint, 'access_token': nonce}
            finally:
                if provider is not llm_provider:
                    provider.retire_test()
    def retire_test():
        for provider in retained:
            provider.retire_test()
    backend.retire_test = retire_test
    return backend
