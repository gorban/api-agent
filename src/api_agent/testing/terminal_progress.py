"""Flushed terminal status from structured completions, separate from raw evidence."""
import json
import threading


_MAX_LINE_LENGTH = 1024
_TRUNCATION_MARKER = ' … [truncated]'


class TerminalTestProgress:
    """Serialize completed counts without forwarding captured output or tracebacks.

    JSON quoting keeps identities on one line, including control characters.
    Scrub the complete formatted line before bounding its terminal display.
    """

    def __init__(self, tests, write, *, scrub=None):
        self._selected = {test.id for test in tests}
        self._completed = set()
        self._write = write
        self._scrub = scrub if scrub is not None else lambda line: line
        self._lock = threading.Lock()

    def __call__(self, event):
        if event.kind != 'test_finished' or event.identity is None:
            return
        with self._lock:
            identity = event.identity.id
            if identity not in self._selected or identity in self._completed:
                return
            self._completed.add(identity)
            status = event.status if event.status in {'passed', 'failed', 'skipped', 'error', 'cancelled'} else 'error'
            line = self._scrub(f'Test progress: {len(self._completed)}/{len(self._selected)} completed '
                               f'— {status} {json.dumps(identity, ensure_ascii=True)}')
            if len(line) > _MAX_LINE_LENGTH:
                line = line[:_MAX_LINE_LENGTH - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
            self._write(line)
