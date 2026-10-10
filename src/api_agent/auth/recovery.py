"""Owned POSIX terminal recovery with credential-private stdout."""
import codecs
import errno
import math
import os
import selectors
import subprocess
import threading
import sys
import time

# Interaction implementations can supply an operation-owned RLock as lock.
# The fallback serializes transports that do not provide their own coordinator.
recovery_lock = threading.RLock()
_LIMIT = 1024 * 1024


def recover(argv, interaction, label, timeout):
    from .credentials import CredentialRecoveryError as CredentialError
    if os.name != 'posix':
        raise CredentialError('Interactive credential recovery requires a POSIX terminal.')
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise CredentialError('Password manager recovery deadline expired.')
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            channel = interaction.open(label)
            try:
                result = _invocation(argv, channel, deadline)
            finally:
                channel.close()
            if result is not None:
                return result
    except CredentialError:
        raise
    except Exception:
        raise CredentialError('Password manager recovery interaction disconnected.') from None


def _invocation(argv, channel, deadline):
    import termios
    from .credentials import CredentialRecoveryError as CredentialError
    master, slave = os.openpty()
    process = None
    disposed = False
    selector = None
    captured = bytearray()
    transcript_size = 0
    terminal_text = _TerminalText()
    input_sent = False
    input_ended = False
    try:
        selector = selectors.DefaultSelector()
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        bootstrap = ('import os,sys,fcntl,termios; '
                     'fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                     'os.execv(sys.argv[1],sys.argv[1:])')
        from .owned_process import OwnedProcess
        process = OwnedProcess([sys.executable, '-c', bootstrap, *argv],
                                   stdin=slave, stderr=slave, stdout=subprocess.PIPE,
                                   shell=False)
        os.close(slave)
        slave = None
        os.set_blocking(master, False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(master, selectors.EVENT_READ, 'terminal')
        selector.register(process.stdout, selectors.EVENT_READ, 'credential')
        while selector.get_map():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise CredentialError('Password manager recovery deadline expired.')
            interval = .05 if remaining is None else min(.05, remaining)
            for key, _ in selector.select(interval):
                try:
                    chunk = os.read(key.fd, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b''
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 'credential':
                    captured.extend(chunk)
                    if len(captured) > _LIMIT:
                        raise CredentialError('Credential CLI output exceeded its limit.')
                else:
                    transcript_size += len(chunk)
                    if transcript_size > _LIMIT:
                        raise CredentialError('Password manager recovery output exceeded its limit.')
                    text = terminal_text.feed(chunk)
                    if text:
                        channel.write(text)
            reply = channel.read(0) if selector.get_map() else None
            if reply is not None:
                if input_ended and reply.action in ('input', 'eof'):
                    continue
                if reply.action == 'eof' and input_sent:
                    os.write(master, attributes[6][termios.VEOF])
                    input_ended = True
                    continue
                if reply.action in ('cancel', 'eof'):
                    explanation = 'cancelled' if reply.action == 'cancel' else 'input ended; log into the CLI and retry'
                    raise CredentialError(f'Password manager recovery {explanation}.')
                if reply.action == 'retry':
                    return None
                if reply.action != 'input':
                    raise CredentialError('Password manager recovery received an invalid action.')
                data = (reply.value + '\n').encode('utf-8')
                if len(data) > 4096:
                    raise CredentialError('Password manager recovery input exceeded its limit.')
                os.write(master, data)
                input_sent = True
        # Streams can close while the helper remains alive. Guardian exit
        # acknowledges group disposal; wait without blocking cancellation.
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise CredentialError('Password manager recovery deadline expired.')
            reply = channel.read(.05)
            if reply is not None and (reply.action in ('cancel', 'retry') or reply.action == 'eof' and not input_ended):
                if reply.action == 'retry':
                    return None
                raise CredentialError('Password manager recovery cancelled or input ended.')
            time.sleep(.01)
        disposed = True
        try:
            process.dispose()
        except ProcessLookupError:
            pass
        code = process.wait()
        if code:
            if input_ended:
                raise CredentialError('Password manager recovery input ended; log into the CLI and retry.')
            raise CredentialError('Password manager authentication failed; log into the CLI and retry.')
        try:
            return captured.decode('utf-8')
        except UnicodeDecodeError:
            raise CredentialError('Credential CLI returned invalid UTF-8 output.') from None
    finally:
        try:
            if process is not None:
                if not disposed:
                    disposed = True
                    try:
                        process.dispose()
                    except ProcessLookupError:
                        pass
                try:
                    if process.returncode is None:
                        process.wait()
                finally:
                    process.dispose()
                    process.stdout.close()
        finally:
            if selector is not None:
                selector.close()
            os.close(master)
            if slave is not None:
                os.close(slave)


class _TerminalText:
    """Incrementally discard terminal commands without buffering their content."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.state = 'text'

    def feed(self, chunk):
        output = []
        for char in self.decoder.decode(chunk):
            if self.state == 'csi':
                if '@' <= char <= '~':
                    self.state = 'text'
            elif self.state == 'osc':
                if char == '\x07':
                    self.state = 'text'
                elif char == '\x1b':
                    self.state = 'osc_escape'
            elif self.state == 'osc_escape':
                self.state = 'text' if char == '\\' else 'osc'
            elif self.state == 'escape':
                self.state = 'csi' if char == '[' else 'osc' if char == ']' else 'text'
            elif char == '\x1b':
                self.state = 'escape'
            elif char == '\x9b':
                self.state = 'csi'
            elif char == '\x9d':
                self.state = 'osc'
            elif char in '\n\r\t' or 32 <= ord(char) < 127 or ord(char) >= 160:
                output.append(char)
        return ''.join(output)
