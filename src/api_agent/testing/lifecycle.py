"""Shared test-run cancellation handshake, independent of test mode/backend."""
import asyncio
import threading


class TestRunLifecycle:
    __test__ = False

    def __init__(self):
        self.cancel_event = threading.Event()
        self.stopped = asyncio.Event()

    @property
    def active(self):
        return not self.stopped.is_set()

    def finish(self):
        """Call only after runner teardown and mode-specific artifact finalization."""
        self.stopped.set()

    async def stop_and_wait(self):
        self.cancel_event.set()
        await self.stopped.wait()
