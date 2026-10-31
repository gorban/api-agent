"""Terminal presentation for one same-loop local operation."""
import asyncio
import sys
import os
import errno
import threading
import signal
import time
from contextlib import contextmanager
from collections import deque


def _create_task(coroutine):
    """Retain no unscheduled coroutine when a task factory rejects allocation."""
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


class TerminalWriter:
    """Bounded best-effort descriptor delivery; a slow reader never stops work."""
    GAP = b'\n[diagnostics gap: output dropped while consumer was slow]\n'

    def __init__(self, descriptors, *, event_limit=256, byte_limit=1024 * 1024, drain_timeout=.2):
        self.loop = asyncio.get_running_loop()
        self.queue = deque()
        self.bytes = 0
        self.event_limit, self.byte_limit = event_limit, byte_limit
        self.drain_timeout = drain_timeout
        self.flags = {fd: os.get_blocking(fd) for fd in set(descriptors)}
        self.wake = asyncio.Event()
        self.gaps = set()
        self.failed = set()
        self.closed = False
        self.active = set()
        # Duplicated pipe descriptors share one delivery owner, so partial writes
        # cannot interleave events (including multibyte characters).
        self.destinations = {}
        for fd in self.flags:
            stat = os.fstat(fd)
            self.destinations[fd] = (stat.st_dev, stat.st_ino)
        self.descriptor_lock = threading.RLock()
        self.owned = {}
        self.pending_flags = {}
        self.workers = {}
        self.descriptor_cleanup = None
        self.close_task = None
        self.task = None
        try:
            for fd in self.flags:
                self.owned[fd] = os.dup(fd)
                self.pending_flags[fd] = self.flags[fd]
            for fd, owned in self.owned.items():
                os.set_blocking(owned, False)
            self.task = _create_task(self._run())
        except BaseException as error:
            self._release_descriptors()
            # Retry transient rollback failures once without replacing acquisition's error.
            self._release_descriptors()
            if self.owned:
                self._retain_cleanup(error)
            raise

    def submit(self, fd, text):
        if self.closed or fd in self.failed:
            return
        if len(text) > self.byte_limit:
            self.gaps.add(fd)
            self.wake.set()
            return
        data = text.encode('utf-8', errors='backslashreplace')
        if len(self.queue) + len(self.active) >= self.event_limit or self.bytes + len(data) > self.byte_limit:
            self.gaps.add(fd)
        else:
            self.queue.append((fd, data))
            self.bytes += len(data)
        self.wake.set()

    async def _write(self, fd, data):
        fd = self.owned[fd]
        offset = 0
        while offset < len(data):
            try:
                offset += os.write(fd, data[offset:])
            except BlockingIOError:
                ready = self.loop.create_future()
                def writable():
                    if not ready.done():
                        ready.set_result(None)
                self.loop.add_writer(fd, writable)
                try:
                    await ready
                finally:
                    self.loop.remove_writer(fd)

    async def _drain(self, destination):
        try:
            while True:
                gap = next((fd for fd in self.gaps
                            if self.destinations[fd] == destination), None)
                if gap is not None:
                    self.gaps.remove(gap)
                    fd, data, counted = gap, self.GAP, False
                else:
                    item = next((item for item in self.queue
                                 if self.destinations[item[0]] == destination), None)
                    if item is None:
                        return
                    self.queue.remove(item)
                    fd, data = item
                    counted = True
                self.active.add(destination)
                try:
                    if fd not in self.failed:
                        try:
                            await self._write(fd, data)
                        except OSError:
                            self.failed.add(fd)
                finally:
                    self.active.discard(destination)
                    if counted:
                        self.bytes -= len(data)
        finally:
            self.workers.pop(destination, None)

    async def _run(self):
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                for destination in set(self.destinations.values()):
                    if destination not in self.workers:
                        worker = _create_task(self._drain(destination))
                        if worker.done():
                            worker.result()
                        else:
                            self.workers[destination] = worker
        finally:
            workers = tuple(self.workers.values())
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self.workers.clear()

    async def aclose(self):
        if (self.close_task is None or self.close_task.done()
                and (self.close_task.cancelled() or self.close_task.exception() is not None)):
            self.closed = True
            try:
                self.close_task = _create_task(self._close())
            except BaseException as error:
                await self._settle_delivery()
                self._release_descriptors()
                if self.owned:
                    self._retain_cleanup(error)
                raise
        await asyncio.shield(self.close_task)

    def _release_descriptors(self):
        with self.descriptor_lock:
            failures = []
            for fd, owned in tuple(self.owned.items()):
                try:
                    os.set_blocking(owned, self.pending_flags[fd])
                except OSError as error:
                    failures.append(error)
                    if error.errno == errno.EBADF:
                        self.pending_flags.pop(fd)
                        self.owned.pop(fd)
                    continue
                self.pending_flags.pop(fd)
                try:
                    os.close(owned)
                except OSError as error:
                    failures.append(error)
                finally:
                    # Linux releases the number before close errors can be reported.
                    # Never retry it: another owner may already have reused it.
                    self.owned.pop(fd)
            return failures

    async def retry_descriptors(self):
        """Retry only retained restoration duties, independent of the delivery loop."""
        with self.descriptor_lock:
            if (self.task is not None and not self.task.done()
                    or self.close_task is not None and not self.close_task.done()
                    or any(not worker.done() for worker in self.workers.values())):
                raise RuntimeError('Terminal delivery cleanup has not settled.')
            failures = self._release_descriptors()
            if failures:
                if self.owned:
                    self._retain_cleanup(failures[0])
                raise failures[0]

    def _retain_cleanup(self, error):
        with self.descriptor_lock:
            from api_agent.providers.control import CleanupPendingError
            if self.descriptor_cleanup is None:
                self.descriptor_cleanup = CleanupPendingError((self,), self.retry_descriptors)
            pending = self.descriptor_cleanup
            error.terminal_cleanup_pending = pending
            cursor = error
            seen = set()
            while cursor is not pending and id(cursor) not in seen:
                seen.add(id(cursor))
                following = getattr(cursor, 'cleanup_pending', None)
                if following is None:
                    cursor.cleanup_pending = pending
                    break
                cursor = following
            owners = (*getattr(error, 'retained_owners', ()), self)
            error.retained_owners = tuple({id(owner): owner for owner in owners}.values())

    def cleanup_status(self, text):
        """Best-effort nonblocking stderr after normal delivery has settled."""
        fd = sys.stderr.fileno()
        if fd not in self.flags:
            return False
        with self.descriptor_lock:
            try:
                if fd not in self.owned:
                    self.owned[fd] = os.dup(fd)
                    self.pending_flags[fd] = self.flags[fd]
                owned = self.owned[fd]
                os.set_blocking(owned, False)
                os.write(owned, (text + '\n').encode())
            except OSError:
                # The same writer retains every acquired restoration duty.
                pass
        return True

    async def finish(self):
        """Settle delivery and give outstanding restoration one bounded retry."""
        try:
            await self.aclose()
        except BaseException as error:
            if self.close_task is not None:
                while not self.close_task.done():
                    try:
                        await asyncio.shield(self.close_task)
                    except BaseException:
                        pass
                if not self.close_task.cancelled():
                    cleanup_error = self.close_task.exception()
                    if cleanup_error is not None and cleanup_error is not error:
                        error.terminal_cleanup_error = cleanup_error
            await self._settle_delivery()
            self._release_descriptors()
            if self.owned:
                self._retain_cleanup(error)
            raise

    async def _settle_delivery(self):
        """Quiesce existing delivery without allocating another cleanup task."""
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except BaseException:
                    pass
            if not self.task.cancelled():
                self.task.exception()
            # The manager's gather has acknowledged all workers, including those
            # cancelled before their coroutine could enter its own finally.
            self.workers.clear()

    async def _close(self):
        try:
            async with asyncio.timeout(self.drain_timeout):
                while self.queue or self.gaps or self.active:
                    await asyncio.sleep(.001)
        except TimeoutError:
            pass
        finally:
            await self._settle_delivery()
            failures = self._release_descriptors()
            if failures:
                raise failures[0]



def format_log(event, source):
    """Keep single-source bytes intact and label each combined-stream line."""
    if source != 'all':
        return event.text if event.source == source else ''
    return ''.join(f'[{event.source}] {line}' for line in event.text.splitlines(keepends=True))


class _TerminalStatus:
    """Present cleanup progress without repeating every retry snapshot."""
    REMINDER_SECONDS = 15

    def __init__(self, diagnostic, clock=time.monotonic):
        self.diagnostic = diagnostic
        self.clock = clock
        self.cleanup_phase = None
        self.scope = None
        self.last_notice = None
        self.startup_activity = None
        self.startup_scope = None

    def __call__(self, snapshot):
        phase = snapshot.phase
        if phase in {'stopping', 'cleanup-pending'}:
            scope = tuple(item.name for item in snapshot.components)
            now = self.clock()
            advanced = phase == 'cleanup-pending' and self.cleanup_phase != 'cleanup-pending'
            first = self.cleanup_phase is None
            changed = scope != self.scope
            if first or advanced:
                self.diagnostic(f'Run local: {phase}')
            elif self.cleanup_phase == 'cleanup-pending' and now - self.last_notice >= self.REMINDER_SECONDS:
                self.diagnostic('Run local: cleanup-pending (still waiting)')
            if scope and changed:
                self.diagnostic('Cleanup scope: ' + ', '.join(scope))
            if first or advanced or changed or now - self.last_notice >= self.REMINDER_SECONDS:
                self.last_notice = now
            self.scope = scope
            if first or advanced:
                self.cleanup_phase = phase
            return
        if phase == 'starting':
            scope = tuple(item.name for item in snapshot.components)
            if self.startup_scope is None:
                self.diagnostic('Run local: starting')
            if scope and scope != self.startup_scope:
                self.diagnostic('Selected components: ' + ', '.join(scope))
            if snapshot.activity and snapshot.activity != self.startup_activity:
                self.diagnostic('Local startup: ' + snapshot.activity)
            self.startup_scope = scope
            self.startup_activity = snapshot.activity
            return
        self.diagnostic(f'Run local: {phase}')
        for destination in snapshot.destinations:
            self.diagnostic(f'{destination.name}: {destination.url}')
        if snapshot.inspection_endpoint:
            self.diagnostic(f'Browser CDP: {snapshot.inspection_endpoint}')


@contextmanager
def _terminal_stop_signals(diagnostic):
    """Own repeated terminal interrupts until same-loop cleanup is acknowledged."""
    state = {'requested': False}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    previous = signal.getsignal(signal.SIGINT)
    task = asyncio.current_task()
    def interrupted(signum, frame):
        if not state['requested']:
            state['requested'] = True
            task.cancel()
        else:
            diagnostic('Cleanup pending; waiting for owned components to stop.')
    signal.signal(signal.SIGINT, interrupted)
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)


async def _retry_terminal_descriptors(writer, error, diagnostic):
    from api_agent.providers.progress import independent_timeout_scope, report_timeout
    receipt = writer.descriptor_cleanup
    while writer.owned:
        text = 'Terminal output cleanup failed; cleanup pending, retrying descriptor restoration.'
        if not writer.cleanup_status(text):
            diagnostic(text)
        try:
            await asyncio.sleep(1)
            with independent_timeout_scope():
                async with report_timeout('Terminal descriptor cleanup attempt', 15):
                    await writer.retry_descriptors()
        except asyncio.CancelledError as interruption:
            error.terminal_interruption = interruption
        except Exception:
            continue
    writer.descriptor_cleanup = None
    cursor, seen = error, set()
    while id(cursor) not in seen:
        seen.add(id(cursor))
        following = getattr(cursor, 'cleanup_pending', None)
        if following is receipt:
            del cursor.cleanup_pending
            break
        if following is None:
            break
        cursor = following
    if getattr(error, 'terminal_cleanup_pending', None) is receipt:
        del error.terminal_cleanup_pending
    error.retained_owners = tuple(owner for owner in getattr(error, 'retained_owners', ())
                                  if owner is not writer)
    error.terminal_cleanup_acknowledged = True


async def _finish_terminal_writer(writer, retain_cleanup, diagnostic):
    try:
        await writer.finish()
    except BaseException as error:
        if retain_cleanup and writer.owned and writer.descriptor_cleanup is not None:
            await _retry_terminal_descriptors(writer, error, diagnostic)
        raise


async def run_local_terminal(context, resolve, *, argv=(), schema=None, owner_factory=None,
                             output=None, diagnostic=None, retain_terminal_cleanup=False):
    writer = None
    if output is None or diagnostic is None:
        descriptors = []
        if output is None:
            descriptors.append(sys.stdout.fileno())
        if diagnostic is None:
            descriptors.append(sys.stderr.fileno())
        writer = TerminalWriter(descriptors)
    output = output or (lambda text: writer.submit(sys.stdout.fileno(), text))
    diagnostic = diagnostic or (lambda text: writer.submit(sys.stderr.fileno(), text + '\n'))
    with _terminal_stop_signals(diagnostic) as stop:
        try:
            primary = None
            try:
                return await _run_local_terminal(context, resolve, argv=argv, schema=schema,
                    owner_factory=owner_factory, output=output, diagnostic=diagnostic)
            except BaseException as error:
                primary = error
                raise
            finally:
                if writer is not None:
                    try:
                        cleanup = _create_task(_finish_terminal_writer(writer, retain_terminal_cleanup, diagnostic))
                    except BaseException as error:
                        await writer._settle_delivery()
                        writer._release_descriptors()
                        writer._release_descriptors()
                        if writer.owned:
                            writer._retain_cleanup(error)
                            if retain_terminal_cleanup:
                                await _retry_terminal_descriptors(writer, error, diagnostic)
                        if primary is None:
                            raise
                        if writer.owned:
                            writer._retain_cleanup(primary)
                        primary.terminal_cleanup_error = error
                        raise primary
                    try:
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError as error:
                                if primary is None:
                                    primary = error
                        cleanup.result()
                    except BaseException as error:
                        if primary is None:
                            raise
                        if writer.owned:
                            writer._retain_cleanup(primary)
                        primary.terminal_cleanup_error = error
                    if primary is not None:
                        raise primary
        except asyncio.CancelledError as error:
            terminal_error = getattr(error, 'terminal_cleanup_error', None)
            if (retain_terminal_cleanup and isinstance(terminal_error, Exception)
                    and getattr(terminal_error, 'terminal_cleanup_acknowledged', False)):
                terminal_error.terminal_interruption = error
                raise terminal_error from None
            if (stop['requested'] and getattr(error, 'cleanup_pending', None) is None
                    and getattr(error, 'terminal_cleanup_error', None) is None):
                return 130
            raise


async def _run_local_terminal(context, resolve, *, argv=(), schema=None, owner_factory=None,
                             output=None, diagnostic=None):
    """Run without stdin; retain cleanup receipts when disposal cannot finish."""
    source = resolve().values['RUN_LOG_SOURCE']
    present_status = _TerminalStatus(diagnostic)
    def event_received(event):
        if event.kind in {'log', 'diagnostic'}:
            if (event.kind == 'log' and event.source == 'agent'
                    and present_status.startup_activity is not None
                    and event.text == f'Local startup: {present_status.startup_activity}.\n'):
                return
            output(format_log(event, source))
        elif event.kind == 'status' and event.snapshot is not None:
            present_status(event.snapshot)
    options = {'schema': schema} if schema is not None else {}
    owner = None
    status = 0
    try:
        if owner_factory is None:
            from .local_run import LocalRunOwner
            owner_factory = LocalRunOwner
        owner = owner_factory(context, resolve, argv=argv, on_event=event_received, **options)
        await owner.start()
        credentials = getattr(owner, 'interactive_dashboard_credentials', lambda: None)()
        if credentials is not None:
            account, password = credentials
            diagnostic(f'LangFuse dashboard account: {account}')
            diagnostic(f'LangFuse dashboard password: {password}')
        status = await owner.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        if owner is not None:
            owner.request_stop()
        status = 130
    except Exception as error:
        diagnostic(owner.diagnostic(error) if owner is not None else 'Local operation could not start.')
        status = 1
    finally:
        try:
            if owner is not None:
                await getattr(owner, 'finish_cleanup', owner.aclose)()
        except BaseException as error:
            pending = owner.pending_cleanup or getattr(error, 'cleanup_pending', None)
            if not isinstance(error, Exception):
                if pending is not None:
                    error.cleanup_pending = pending
                raise
            if pending is not None:
                from api_agent.providers.control import CleanupPendingError
                safe = CleanupPendingError((owner,), pending.retry)
                safe.cleanup_pending = pending
                raise safe from None
            raise RuntimeError(owner.diagnostic(error)) from None
    return status


async def open_host_url(url):
    """Delegate a verified destination to the host's default URL handler."""
    import os
    import shutil
    import subprocess
    import webbrowser
    from api_agent.providers.progress import component_timeout
    from api_agent.runtime.tasks import offload
    async with component_timeout('Open local browser destination', 15):
        if os.environ.get('DISTROBOX_ENTER_PATH'):
            host = shutil.which('distrobox-host-exec')
            if host is None:
                raise RuntimeError('Host browser forwarding is unavailable; open the displayed URL on the host.')
            await offload(subprocess.run, [host, 'xdg-open', url], check=True, timeout=10,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not await offload(webbrowser.open, url, new=2):
            raise RuntimeError('The default browser could not open the displayed URL.')
