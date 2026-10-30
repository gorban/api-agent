"""Login relay to explicit terminal streams, independent of pytest capture."""
import os
import re
import select
import sys
import threading
from collections import deque


class TerminalInteraction:
    def __init__(self, input_stream=None, output_stream=None, *, presentation=None):
        self.input = sys.__stdin__ if input_stream is None else input_stream
        self.output = sys.__stderr__ if output_stream is None else output_stream
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.presentation = presentation
        self.channels = deque()
    def open(self, label):
        if os.name != 'posix':
            from .credentials import CredentialRecoveryError
            raise CredentialRecoveryError('Interactive credential recovery requires a POSIX terminal.')
        with self.lock:
            channel = _Channel(self, label, mask=not self.channels)
            self.channels.append(channel)
            if self.presentation is not None:
                self.presentation.hold()
            return channel


class _Channel:
    def __init__(self, owner, label, *, mask=True):
        self.owner, self.closed = owner, False
        self.label = ''.join(char for char in label[:200] if char.isprintable())
        self.announced = False
        self.input_ready = False
        self.pending_prompt = ''
        self.pending_text = ''
        self.fd = owner.input.fileno()
        self.settings = None
        self.terminal = None
        if mask and os.isatty(self.fd):
            import termios
            self.terminal = termios
            self.settings = termios.tcgetattr(self.fd)
            masked = list(self.settings)
            masked[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, masked)
    def write(self, text):
        with self.owner.lock:
            if self.closed:
                return
            self.pending_prompt = (self.pending_prompt + text).replace('\r', '\n').rsplit('\n', 1)[-1][-4096:]
            line = self.pending_prompt.rstrip()
            self.input_ready = (line.endswith((':', '?'))
                                or re.search(r'\[[^\[\]]*[/|,][^\[\]]*\]$', line) is not None)
            if len(self.pending_text) + len(text) > 65536:
                from .credentials import CredentialRecoveryError
                raise CredentialRecoveryError('Authentication prompt exceeded its display limit.')
            self.pending_text += text
            if self.owner.channels[0] is self:
                self._flush_prompt()

    def _flush_prompt(self):
        title = '' if self.announced else f'Authentication: {self.label}\n'
        self.owner.output.write(title + self.pending_text)
        self.owner.output.flush()
        self.announced = True
        self.pending_text = ''
    def read(self, timeout):
        with self.owner.lock:
            return self._read(timeout)

    def _read(self, timeout):
        from .interaction import Reply
        if self.closed:
            return Reply('cancel')
        if self.owner.channels[0] is not self or not self.input_ready:
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
        with self.owner.lock:
            if self.closed:
                return
            current = self.owner.channels[0] is self
            if self.settings is not None and len(self.owner.channels) == 1:
                self.terminal.tcsetattr(self.fd, self.terminal.TCSANOW, self.settings)
                self.settings = None
            self.closed = True
            self.owner.channels.remove(self)
            self.pending_text = ''
            self.pending_prompt = ''
            try:
                if current and self.owner.channels:
                    following = self.owner.channels[0]
                    following.settings, self.settings = self.settings, None
                    following.terminal = self.terminal
                    if following.pending_text:
                        self.owner.output.write('\n')
                        following._flush_prompt()
            finally:
                if self.owner.presentation is not None:
                    self.owner.presentation.release()
