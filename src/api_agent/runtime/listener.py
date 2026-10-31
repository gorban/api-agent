"""An owned, single-worker HTTPS ASGI listener for local operations."""

import asyncio
import errno
import time

from api_agent.providers.progress import independent_timeout_scope


class ListenerError(RuntimeError):
    _DIAGNOSTICS = {
        'listener_bind_in_use': 'Address already in use; choose an available AGENT_DJANGO_URL port.',
        'listener_bind_unavailable': 'Listener address is unavailable; check AGENT_DJANGO_URL.',
        'listener_bind_denied': 'Listener bind was denied; check AGENT_DJANGO_URL port permissions.',
        'listener_start_failed': 'Listener stopped during startup; check local listener configuration.',
        'listener_readiness_timeout': 'Listener readiness timed out; check local listener configuration.',
        'listener_stopped_before_ready': 'Listener stopped before it became ready.',
    }

    def __init__(self, code):
        super().__init__(code)
        self.code = code

    def diagnostic(self):
        """Return only a recognized, fixed message for local presentation."""
        return self._DIAGNOSTICS.get(self.code) if type(self.code) is str else None


def _start_failure(error):
    """Classify bounded exception context without retaining OS diagnostics."""
    messages = {
        errno.EADDRINUSE: 'listener_bind_in_use',
        errno.EADDRNOTAVAIL: 'listener_bind_unavailable',
        errno.EACCES: 'listener_bind_denied',
    }
    seen = set()
    current = error.__context__
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in messages:
            return messages[current.errno]
        current = current.__cause__ or current.__context__
    return 'listener_start_failed'


class UvicornListener:
    def __init__(self, *, bind='127.0.0.1'):
        self.bind = bind
        self.server = None
        self.task = None

    async def start(self, application, material, endpoint):
        if self.task is not None:
            raise ListenerError('listener_already_started')
        import uvicorn
        configuration = uvicorn.Config(
            application, host=self.bind, port=endpoint.port,
            ssl_certfile=str(material.cert_file), ssl_keyfile=str(material.key_file),
            lifespan='on', workers=1, reload=False, log_level='warning',
        )
        self.server = uvicorn.Server(configuration)
        async def serve():
            try:
                await self.server.serve()
            except SystemExit as error:
                # Uvicorn exits on bind failures; an owned background task must
                # report failure without terminating its coordinator's loop.
                raise ListenerError(_start_failure(error)) from None
        # Connections inherit this task context. Startup bounds acquisition,
        # while later requests own fresh budgets and retain the progress sink.
        with independent_timeout_scope():
            self.task = asyncio.create_task(serve())

    async def wait_ready(self, timeout):
        if self.task is None:
            raise ListenerError('listener_not_started')
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if self.task.done():
                await self.task
                raise ListenerError('listener_stopped_before_ready')
            if time.monotonic() >= deadline:
                raise ListenerError('listener_readiness_timeout')
            await asyncio.sleep(min(.05, max(0, deadline - time.monotonic())))

    async def stop(self):
        if self.task is None:
            return
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, 10)
        except TimeoutError as error:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            raise ListenerError('listener_shutdown_timeout') from error
        finally:
            self.task = None
            self.server = None
