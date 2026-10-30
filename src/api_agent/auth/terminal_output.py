"""Defer ordinary terminal output while private authentication owns the screen."""
import json
import sys
import tempfile
import threading


class TerminalOutput:
    """Queue only already-scrubbed output, never login transcripts or replies.

    A spool bounds memory without truncating failure evidence during a long
    browser sign-in. Both terminal streams share one ordering lock and queue.
    """

    def __init__(self, stdout=None, stderr=None):
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.lock = threading.RLock()
        self.active = 0
        self.pending = None

    def hold(self):
        with self.lock:
            self.active += 1

    def write(self, text, *, error=False, end='\n'):
        with self.lock:
            text += end
            if self.active:
                if self.pending is None:
                    self.pending = tempfile.SpooledTemporaryFile(
                        mode='w+', encoding='utf-8', max_size=65536)
                self.pending.write(json.dumps((error, text)) + '\n')
            else:
                self._emit(error, text)

    def _emit(self, error, text):
        stream = self.stderr if error else self.stdout
        stream.write(text)
        stream.flush()

    def release(self):
        with self.lock:
            self.active -= 1
            if not self.active:
                # Input is masked: the terminal need not have echoed its newline.
                try:
                    self._emit(True, '\n')
                except BaseException:
                    pending, self.pending = self.pending, None
                    if pending is not None:
                        pending.close()
                    raise
                self._drain()

    def _drain(self):
        pending, self.pending = self.pending, None
        if pending is not None:
            with pending:
                pending.seek(0)
                for row in pending:
                    self._emit(*json.loads(row))

    def close(self):
        """Flush after cleanup, or retain output with any still-owned channel."""
        with self.lock:
            if not self.active:
                self._drain()
