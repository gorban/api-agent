"""Async pipes around a Popen child reserved until acknowledged disposal."""
import asyncio
import subprocess

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.tasks import settle
from api_agent.testing.processes import finalize, observe_exit


class _ReaderProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader, loop):
        super().__init__(reader, loop=loop)
        self.closed = loop.create_future()

    def connection_lost(self, error):
        super().connection_lost(error)
        self.closed.set_result(None)


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self, loop):
        super().__init__(loop=loop)
        self.closed = loop.create_future()

    def connection_lost(self, error):
        super().connection_lost(error)
        self.closed.set_result(None)

    def _get_close_waiter(self, stream):
        # StreamWriter waiters cannot revoke the shared disposal acknowledgment.
        return asyncio.shield(self.closed)


class OwnedWorker:
    def __init__(self, process):
        self.process = process
        self.stdout = None
        self.stdin = None
        self._connections = []

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        return self.process.returncode

    async def wait(self):
        while True:
            future = asyncio.get_running_loop().run_in_executor(None, observe_exit, self.process)
            try:
                code = await asyncio.shield(future)
            except asyncio.CancelledError:
                await settle(future)
                raise
            if code is not None:
                return code
            await asyncio.sleep(.01)

    async def dispose(self):
        # Executor futures survive cancellation of every ordinary asyncio task.
        future = asyncio.get_running_loop().run_in_executor(None, finalize, self.process)
        cancelled = False
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
            await settle(future)
        for transport, protocol in self._connections:
            transport.close()
        for _, protocol in self._connections:
            try:
                await asyncio.shield(protocol.closed)
            except asyncio.CancelledError:
                cancelled = True
                await settle(protocol.closed)
        self.process.stdin.close()
        self.process.stdout.close()
        if cancelled:
            raise asyncio.CancelledError


async def spawn_worker(*argv, limit=65536, **options):
    """Own launch and pipe attachment through cancellation and cleanup refusal."""
    owned = []
    def launch():
        owner = OwnedWorker(subprocess.Popen(argv, **options))
        owned.append(owner)
        return owner
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, launch)
    try:
        owner = await asyncio.shield(future)
        reader = asyncio.StreamReader(limit=limit)
        protocol = _ReaderProtocol(reader, loop)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, owner.process.stdout)
        owner._connections.append((transport, protocol))
        owner.stdout = reader
        protocol = _WriterProtocol(loop)
        transport, _ = await loop.connect_write_pipe(lambda: protocol, owner.process.stdin)
        owner._connections.append((transport, protocol))
        owner.stdin = asyncio.StreamWriter(transport, protocol, None, loop)
        return owner
    except BaseException as primary:
        try:
            await settle(future)
        except BaseException:
            # Launch failure has no child owner; preserve the original outcome.
            pass
        for owner in owned:
            try:
                await owner.dispose()
            except BaseException as cleanup:
                pending = CleanupPendingError((owner,), owner.dispose)
                primary.retained_owners = (owner,)
                primary.cleanup_error = pending
                pending.__cause__ = cleanup
        raise
