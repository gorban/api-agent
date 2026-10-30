"""Worker inspection control. Only lifecycle facts cross the process boundary."""
import json
import os
from pathlib import Path
import threading
import time

from .processes import disposal_complete, finalize
from .suite_lock import CleanupLeaseOwner, retain_suite_cleanup


class InspectionDirectory:
    """Transfer the temporary IPC directory with its retained worker."""
    def __init__(self, temporary):
        self.temporary = temporary
        self.retained = False

    def __enter__(self):
        return self.temporary.name

    def __exit__(self, *args):
        if not self.retained:
            self.temporary.cleanup()


class IntegrationInspection(CleanupLeaseOwner):
    """Own a specific pytest process group until acknowledged inspection cleanup."""
    def __init__(self, process, directory, event_file, reader, *, on_event=None, repo='', liveness_fd=None,
                 registry=None):
        self.liveness_fd = liveness_fd
        self.process = process
        self.directory = directory
        self.event_file = Path(event_file)
        self.reader = reader
        self.on_event = on_event
        self.repo = repo
        self.closed = False
        self.pending = False
        self.lost = False
        self._process_reaped = False
        self._reader_drained = False
        self._stdout_closed = False
        self._lock = threading.Lock()
        self._attempt = 0
        try:
            # Register before transfer so interrupted initialization cannot
            # strand an owned worker outside its operation's cleanup guard.
            if registry is not None:
                registry.append(self)
            directory.retained = True
            self._monitor = threading.Thread(target=self._watch, daemon=True)
            self._monitor.start()
        except BaseException:
            directory.retained = False
            if registry is not None and self in registry:
                registry.remove(self)
            raise
        retain_suite_cleanup(self)

    def _rows(self):
        try:
            content = self.event_file.read_text()
        except (OSError, ValueError):
            return []
        rows = []
        for line in content.splitlines(keepends=True):
            if not line.endswith('\n'):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    @property
    def inspection_endpoints(self):
        """Read retained browser addresses from the owned worker event stream."""
        return tuple(row['endpoint'] for row in self._rows()
                     if row.get('kind') == 'inspection_open'
                     and isinstance(row.get('endpoint'), str))

    def _emit(self, kind):
        from .contracts import RunnerEvent
        if self.on_event is not None:
            try:
                self.on_event(RunnerEvent(kind, self.repo))
            except Exception:
                pass

    def _reap(self):
        # A successful group disposal releases its numeric identity. Later
        # reader/stream/directory failures must not signal that identity again.
        if not self._process_reaped:
            self._process_reaped = disposal_complete(self.process)
        if not self._process_reaped:
            try:
                finalize(self.process)
            finally:
                self._process_reaped = disposal_complete(self.process)
            self._process_reaped = True
        if self.liveness_fd is not None:
            os.close(self.liveness_fd)
            self.liveness_fd = None
        if not self._reader_drained:
            self.reader.join(timeout=5)
            if self.reader.is_alive():
                raise TimeoutError('Inspection output reader remains owned')
            self._reader_drained = True
        if not self._stdout_closed:
            self.process.stdout.close()
            self._stdout_closed = True
        self.directory.temporary.cleanup()
        self.closed = True
        self.pending = False
        self._acknowledge_closed()

    def _watch(self):
        while True:
            event = None
            with self._lock:
                if self.closed:
                    return
                if self.process.poll() is not None:
                    rows = self._rows()
                    acknowledged = (any(row.get('kind') == 'inspection_closed' for row in rows)
                                    and not any(row.get('kind') == 'inspection_lost' for row in rows))
                    self.lost = not acknowledged
                    try:
                        self._reap()
                    except Exception:
                        self.pending = True
                        event = 'inspection_cleanup_pending'
                    else:
                        event = 'inspection_closed' if acknowledged else 'inspection_lost'
            if event is not None:
                self._emit(event)
                return
            time.sleep(.03)

    def end_inspection(self, *, timeout=10):
        """Request bounded cleanup; False retains ownership for an explicit retry."""
        event = None
        with self._lock:
            if self.closed:
                self._acknowledge_closed()
                self.pending = False
                return True
            if self.process.poll() is not None:
                rows = self._rows()
                acknowledged = (any(row.get('kind') == 'inspection_closed' for row in rows)
                                and not any(row.get('kind') == 'inspection_lost' for row in rows))
                self.lost = not acknowledged
                try:
                    self._reap()
                except Exception:
                    self.pending = True
                    event = 'inspection_cleanup_pending'
                else:
                    event = 'inspection_closed' if acknowledged else 'inspection_lost'
            else:
                self._attempt += 1
                attempt = self._attempt
                (Path(self.directory.temporary.name) / 'inspection-command.json').write_text(
                    json.dumps({'version': 1, 'attempt': attempt, 'command': 'end_inspection'}))
        if event is not None:
            self._emit(event)
            return self.closed
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.closed:
                    self._acknowledge_closed()
                    self.pending = False
                    return True
                if any(row.get('kind') == 'inspection_cleanup_pending' and row.get('attempt') == attempt
                       for row in self._rows()):
                    self.pending = True
                    break
            time.sleep(.03)
        with self._lock:
            if self.closed:
                self._acknowledge_closed()
                self.pending = False
                return True
            self.pending = True
        self._emit('inspection_cleanup_pending')
        return False

    close = end_inspection

    def wait_closed(self, *, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.closed:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(.03)
        with self._lock:
            try:
                self._acknowledge_closed()
            except BaseException as error:
                self.pending = True
                if not isinstance(error, Exception):
                    raise
                return False
            self.pending = False
            return True

    def abort(self):
        """Synchronous emergency cleanup of this registered owned process group."""
        reaped = False
        with self._lock:
            if not self.closed:
                self._reap()
                reaped = True
            else:
                self._acknowledge_closed()
                self.pending = False
        if reaped:
            self._emit('inspection_closed')
