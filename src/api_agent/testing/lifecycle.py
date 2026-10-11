"""Shared test-run cancellation handshake, independent of test mode/backend."""
import asyncio
import threading
from time import monotonic


class TestRunLifecycle:
    __test__ = False

    def __init__(self):
        self.started_at = monotonic()
        self.finished_at = None
        self.cancel_event = threading.Event()
        self.stopped = asyncio.Event()

    @property
    def active(self):
        return not self.stopped.is_set()

    @property
    def elapsed(self):
        return (self.finished_at if self.finished_at is not None else monotonic()) - self.started_at

    def finish(self):
        """Call only after runner teardown and mode-specific artifact finalization."""
        if self.finished_at is None:
            self.finished_at = monotonic()
        self.stopped.set()

    async def stop_and_wait(self):
        self.cancel_event.set()
        await self.stopped.wait()
