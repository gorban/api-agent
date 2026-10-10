"""Login relay to explicit terminal streams, independent of pytest capture."""
import os
import re
import select
import sys
import threading


class TerminalInteraction:
    def __init__(self, input_stream=None, output_stream=None):
        self.input = sys.__stdin__ if input_stream is None else input_stream
        self.output = sys.__stderr__ if output_stream is None else output_stream
        self.lock = threading.RLock()
        self.buffer = bytearray()
    def open(self, label):
        if os.name != 'posix':
            from .credentials import CredentialRecoveryError
            raise CredentialRecoveryError('Interactive credential recovery requires a POSIX terminal.')
        return _Channel(self)


class _Channel:
    def __init__(self, owner):
        self.owner, self.closed = owner, False
        self.input_ready = False
        self.pending_prompt = ''
        self.fd = owner.input.fileno()
        self.settings = None
        self.terminal = None
        if os.isatty(self.fd):
            import termios
            self.terminal = termios
            self.settings = termios.tcgetattr(self.fd)
            masked = list(self.settings)
            masked[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, masked)
    def write(self, text):
        if not self.closed:
            self.pending_prompt = (self.pending_prompt + text).replace('\r', '\n').rsplit('\n', 1)[-1][-4096:]
            line = self.pending_prompt.rstrip()
            self.input_ready = (line.endswith((':', '?'))
                                or re.search(r'\[[^\[\]]*[/|,][^\[\]]*\]$', line) is not None)
            with self.owner.lock:
                self.owner.output.write(text)
                self.owner.output.flush()
    def read(self, timeout):
        from .interaction import Reply
        if self.closed:
            return Reply('cancel')
        if not self.input_ready:
            return None
        if b'\n' not in self.owner.buffer:
            if not select.select([self.fd], [], [], min(0.1, max(0, timeout)))[0]:
                return None
            chunk = os.read(self.fd, 4096 - len(self.owner.buffer))
            if not chunk:
                return Reply('eof')
            self.owner.buffer.extend(chunk)
            if len(self.owner.buffer) > 4096 or (len(self.owner.buffer) == 4096 and b'\n' not in self.owner.buffer):
                self.owner.buffer.clear()
                return Reply('cancel')
        if b'\n' not in self.owner.buffer:
            return None
        line, _, rest = self.owner.buffer.partition(b'\n')
        self.owner.buffer = bytearray(rest)
        self.input_ready = False
        self.pending_prompt = ''
        return Reply('input', line.decode('utf-8', errors='replace').rstrip('\r'))
    def close(self):
        self.closed = True
        if self.settings is not None:
            self.terminal.tcsetattr(self.fd, self.terminal.TCSANOW, self.settings)
            self.settings = None
