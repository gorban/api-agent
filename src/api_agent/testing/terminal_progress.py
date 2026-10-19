"""Flushed terminal status from structured completions, separate from raw evidence."""
import json
import threading


class TerminalTestProgress:
    """Serialize completed counts without forwarding captured output or tracebacks.

    The caller supplies its credential-scrubbed terminal writer. JSON quoting keeps
    test identities on one terminal line, including control characters.
    """

    def __init__(self, tests, write):
        self._selected = {test.id for test in tests}
        self._completed = set()
        self._write = write
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
            self._write(f'Test progress: {len(self._completed)}/{len(self._selected)} completed '
                        f'— {status} {json.dumps(identity, ensure_ascii=True)}')
