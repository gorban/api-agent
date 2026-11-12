"""Keep a selected prerequisite contribution alive across confirmation waves."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import copy_context
import math
from pathlib import Path
import threading
import time

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.loading import aload_provider
from api_agent.runtime.tasks import offload
from api_agent.providers.progress import report_timeout_sync
from .suite_lock import CleanupLeaseOwner


def validate_prerequisite_event(event):
    """Admit only content-free phase timings and integer counters."""
    if (type(event) is not dict or not {'phase', 'seconds'} <= event.keys()
            or not event.keys() <= {'phase', 'seconds', 'reuse', 'miss', 'count', 'pending'}
            or type(event['phase']) is not str
            or event['phase'] not in {'prepare', 'readiness', 'cleanup', 'error'}):
        raise ValueError('Invalid prerequisite diagnostic')
    seconds = event['seconds']
    if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
            or any(type(value) is not int or value < 0
                   for key, value in event.items() if key not in {'phase', 'seconds'})):
        raise ValueError('Invalid prerequisite diagnostic')
    return dict(event)


class SuitePrerequisiteProvider(CleanupLeaseOwner):
    def __init__(self, context, config, directory, recovery, *, output=None):
        self.config = dict(config)
        self.context, self.directory, self.recovery = context, Path(directory), recovery
        self.output = output
        self.timeout = self._duration(config.get('E2E_PREREQUISITE_TIMEOUT_SECONDS', 600))
        self.cleanup_timeout = self._duration(config.get('E2E_PREREQUISITE_CLEANUP_TIMEOUT_SECONDS', 30))
        self.loop = self.thread = self.provider = None
        self.disposal_implementation = None
        self.start_future = self.close_future = self.acknowledgement_future = None
        self.failed_owners = []
        self.closed = False
        self._event_lock = threading.Lock()
        self._events = []

    def _event(self, event):
        event = validate_prerequisite_event(event)
        with self._event_lock:
            self._events.append(event)

    @property
    def events(self):
        with self._event_lock:
            return tuple(dict(event) for event in self._events)

    @staticmethod
    def _duration(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Prerequisite lifetime budgets must be finite and positive')
        return value

    async def _start(self):
        try:
            self.provider = await aload_provider(self.config['E2E_PREREQUISITE_PROVIDER'], self.config,
                {'owner_root': self.context.owner_root, 'suite_directory': self.directory,
                 'native_recovery': self.recovery, 'output': self.output, 'prerequisite_event': self._event}, kind='test_prerequisites')
        except BaseException as error:
            self.failed_owners.extend(getattr(error, 'retained_owners', ()))
            raise
        if 'dispose_workers' in self.provider.implementation.capabilities.operations:
            self.disposal_implementation = self.provider.require('test_prerequisites', 'dispose_workers')
        implementation = self.provider.require('test_prerequisites', 'prepare')
        await implementation.aprepare({'suite_directory': str(self.directory)}, auth=None)

    def start(self, *, cancelled=None):
        if self.closed:
            raise ValueError('Prerequisite suite owner is closed')
        if self.start_future is None:
            self.loop = asyncio.new_event_loop()
            def run():
                asyncio.set_event_loop(self.loop)
                self.loop.run_forever()
            context = copy_context()
            try:
                self.thread = threading.Thread(target=context.run, args=(run,), daemon=True)
                self.thread.start()
            except BaseException:
                self.loop.close()
                self.loop = self.thread = None
                raise
            self.start_future = asyncio.run_coroutine_threadsafe(self._start(), self.loop)
        deadline = time.monotonic() + self.timeout
        with report_timeout_sync('Suite prerequisite preparation', self.timeout):
            while True:
                if cancelled is not None and cancelled.is_set():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._pending()
                try:
                    self.start_future.result(timeout=min(0.05, remaining))
                    return True
                except FutureTimeout:
                    if self.start_future.done():
                        raise

    async def _dispose(self):
        if self.start_future is not None:
            try:
                await asyncio.wrap_future(self.start_future)
            except (Exception, asyncio.CancelledError):
                pass
        if self.provider is not None:
            await self.provider.aclose()
        for owner in tuple(self.failed_owners):
            await owner.aclose()
            self.failed_owners.remove(owner)

    def _pending(self):
        async def retry():
            await offload(self.close)
        return CleanupPendingError((self,), retry)

    async def _acknowledge_workers(self):
        # Loading admitted this control hook before aclose could mark the
        # provider closed. Pending cleanup must still accept later disposal
        # proof without reopening ordinary provider operations.
        if self.disposal_implementation is not None:
            await self.disposal_implementation.adispose_workers({'confirmed': True}, auth=None)

    def close(self, *, workers_disposed=False):
        if self.closed:
            return
        if self.loop is not None:
            if workers_disposed or self.acknowledgement_future is not None:
                if (self.acknowledgement_future is None or self.acknowledgement_future.done()
                        and self.acknowledgement_future.exception() is not None):
                    self.acknowledgement_future = asyncio.run_coroutine_threadsafe(self._acknowledge_workers(), self.loop)
                try:
                    self.acknowledgement_future.result(timeout=self.cleanup_timeout)
                except BaseException as error:
                    raise self._pending() from error
            if self.close_future is None or self.close_future.done() and self.close_future.exception() is not None:
                self.close_future = asyncio.run_coroutine_threadsafe(self._dispose(), self.loop)
            try:
                self.close_future.result(timeout=self.cleanup_timeout)
            except BaseException as error:
                raise self._pending() from error
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(self.cleanup_timeout)
            if self.thread.is_alive():
                raise self._pending()
            self.loop.close()
            self.loop = self.thread = None
        self.closed = True
        self._acknowledge_closed()
