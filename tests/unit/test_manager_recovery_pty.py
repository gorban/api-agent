"""Synthetic helpers exercise real controlling-terminal authentication."""
import os
import sys

import pytest

from api_agent.auth.credentials import CredentialError
from api_agent.auth.interaction import Reply
from api_agent.auth.recovery import recover


class Channel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.output = ''
        self.closed = False
        self.sent = 0

    def write(self, text):
        self.output += text

    def read(self, timeout):
        if self.output.count('Password:') > self.sent:
            self.sent += 1
            return next(self.replies, None)
        return None

    def close(self):
        self.closed = True


class Interaction:
    def __init__(self, replies):
        self.channel = Channel(replies)

    def open(self, label):
        assert label == 'Keeper'
        return self.channel


@pytest.mark.skipif(os.name != 'posix', reason='POSIX controlling PTY')
def test_real_getpass_stdout_private():
    interaction = Interaction([Reply('input', 'master-secret')])
    script = "import getpass; assert getpass.getpass('Password: ') == 'master-secret'; print('vault-secret')"
    assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'vault-secret\n'
    assert 'master-secret' not in interaction.channel.output
    assert 'vault-secret' not in interaction.channel.output
    assert interaction.channel.closed


@pytest.mark.parametrize('action', ['cancel', 'eof'])
def test_cancel_and_eof(action):
    interaction = Interaction([Reply(action)])
    with pytest.raises(CredentialError, match='cancelled|input ended'):
        recover([sys.executable, '-c', "import getpass; getpass.getpass('Password: ')"], interaction, 'Keeper', 2)
    assert interaction.channel.closed


def test_reply_hides_value():
    assert 'secret' not in repr(Reply('input', 'secret'))


def test_incorrect_password_then_correct():
    interaction = Interaction([Reply('input', 'wrong'), Reply('input', 'correct')])
    script = "import getpass\nwhile getpass.getpass('Password: ') != 'correct': pass\nprint('secret')"
    assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'secret\n'
    assert 'wrong' not in interaction.channel.output
    assert 'correct' not in interaction.channel.output


def test_timeout_and_nonzero_failures_are_safe():
    for script, pattern in [("import time; time.sleep(10)", 'deadline'),
                            ("import sys; print('private', file=sys.stderr); sys.exit(7)", 'authentication failed')]:
        interaction = Interaction([])
        with pytest.raises(CredentialError, match=pattern) as error:
            recover([sys.executable, '-c', script], interaction, 'Keeper', .15)
        assert 'private' not in str(error.value)
        assert interaction.channel.closed


def test_retry_closes_previous_generation():
    class Generations:
        def __init__(self):
            self.channels = []

        def open(self, label):
            if self.channels:
                assert self.channels[-1].closed
            channel = Channel([Reply('retry')] if not self.channels else [Reply('input', 'ok')])
            self.channels.append(channel)
            return channel

    interaction = Generations()
    script = "import getpass; assert getpass.getpass('Password: ') == 'ok'; print('secret')"
    assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'secret\n'
    assert len(interaction.channels) == 2
    assert all(channel.closed for channel in interaction.channels)


def test_keeper_classification_and_username_preserves_type(tmp_path):
    from api_agent.auth.credentials import CredentialLoginRequiredError, get_username
    from api_agent.auth.password_managers.keeper import classify_login
    assert classify_login('Persistent login failed: private-account')
    assert classify_login('Enter master password:')
    assert not classify_login('Record not found')
    path = tmp_path / 'keeper'
    path.write_text(f'#!{sys.executable}\nimport sys\nprint("Persistent login failed: private-account", file=sys.stderr)\nsys.exit(1)\n')
    path.chmod(0o700)
    source = dict(manager='keeper', cli_path=str(path), vault='vault', user='user')
    with pytest.raises(CredentialLoginRequiredError) as error:
        get_username(source)
    assert 'private-account' not in str(error.value)


def test_original_retrieval_after_logged_out_failure(tmp_path):
    from api_agent.auth.credentials import get_password
    path = tmp_path / 'keeper'
    path.write_text(f'#!{sys.executable}\nimport sys,os,getpass\nassert sys.argv[1:] == ["find-password", "vault/user"]\nif not sys.stdin.isatty():\n print("Persistent login failed", file=sys.stderr);sys.exit(1)\nassert getpass.getpass("Password: ") == "master-secret"\nprint("credential-secret")\n')
    path.chmod(0o700)
    interaction = Interaction([Reply('input', 'master-secret')])
    assert get_password(dict(manager='keeper', cli_path=str(path), vault='vault', user='user'),
                        interaction=interaction, recovery_timeout=2) == 'credential-secret'
    assert 'credential-secret' not in interaction.channel.output


def test_concurrent_retrievals_reuse_session_without_sharing_value(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from api_agent.auth.credentials import get_password
    marker = tmp_path / 'authenticated'
    path = tmp_path / 'keeper'
    path.write_text(f'#!{sys.executable}\nimport sys,os,getpass,pathlib\np=pathlib.Path({str(marker)!r})\nif not p.exists():\n if not sys.stdin.isatty():\n  print("Persistent login failed", file=sys.stderr);sys.exit(1)\n assert getpass.getpass("Password: ") == "master-secret"\n p.write_text("authenticated")\nprint(sys.argv[-1])\n')
    path.chmod(0o700)
    class Operation(Interaction):
        lock = threading.RLock()
        opened = 0
        def open(self, label):
            self.opened += 1
            return super().open(label)
    interaction = Operation([Reply('input', 'master-secret')])
    def fetch(user):
        return get_password(dict(manager='keeper', cli_path=str(path), vault='vault', user=user),
                            interaction=interaction, recovery_timeout=2)
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(fetch, ['one', 'two'])) == ['vault/one', 'vault/two']
    assert interaction.opened == 1


@pytest.mark.parametrize('reply,pattern', [(Reply('unknown'), 'invalid action'),
                                         (Reply('input', 's' * 4096), 'input exceeded')])
def test_invalid_input_is_private(reply, pattern):
    interaction = Interaction([reply])
    with pytest.raises(CredentialError, match=pattern):
        recover([sys.executable, '-c', "import getpass; getpass.getpass('Password: ')"], interaction, 'Keeper', 2)
    assert interaction.channel.closed


@pytest.mark.parametrize('script,pattern', [
    ("import sys; sys.stdout.buffer.write(b'\\xff')", 'invalid UTF-8'),
    ("import sys; sys.stdout.write('s' * 1100000)", 'output exceeded'),
    ("import sys; sys.stderr.write('s' * 1100000)", 'output exceeded'),
])
def test_bounded_private_streams(script, pattern):
    interaction = Interaction([])
    with pytest.raises(CredentialError, match=pattern) as error:
        recover([sys.executable, '-c', script], interaction, 'Keeper', 2)
    assert len(str(error.value)) < 150
    assert interaction.channel.closed


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_invalid_recovery_budget(timeout):
    interaction = Interaction([])
    with pytest.raises(CredentialError, match='deadline'):
        recover([sys.executable, '-c', 'pass'], interaction, 'Keeper', timeout)
    assert not interaction.channel.closed


def test_disconnected_transport_is_safe():
    class Disconnected(Interaction):
        def open(self, label):
            raise RuntimeError('private transcript')
    with pytest.raises(CredentialError, match='disconnected') as error:
        recover([sys.executable, '-c', 'pass'], Disconnected([]), 'Keeper', 2)
    assert 'private' not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize('action', ['cancel', 'eof', 'retry', None])
def test_closed_streams_still_poll_transport_and_budget(action):
    class Closed:
        attempts = 0
        def open(self, label):
            self.attempts += 1
            class Waiting(Channel):
                def read(self, timeout):
                    if timeout > 0 and action is not None:
                        return Reply(action)
                    return None
            return Waiting([])
    interaction = Closed()
    script = "import os,time; os.close(0); os.close(1); os.close(2); time.sleep(10)"
    with pytest.raises(CredentialError, match='cancelled|deadline'):
        recover([sys.executable, '-c', script], interaction, 'Keeper', .15)
    if action == 'retry':
        assert interaction.attempts > 1


def test_unbounded_budget_when_no_parent():
    interaction = Interaction([])
    assert recover([sys.executable, '-c', "print('value')"], interaction, 'Keeper', None) == 'value\n'


def test_operation_deadline_and_cancellation_before_spawn():
    import threading
    from api_agent.auth.credentials import CredentialRecoveryError, get_password
    interaction = Interaction([])
    for timeout in [0, -1, float('nan'), float('inf')]:
        with pytest.raises(CredentialRecoveryError, match='deadline'):
            get_password({}, interaction=interaction, recovery_timeout=timeout)
    interaction.cancel_event = threading.Event()
    interaction.cancel_event.set()
    with pytest.raises(CredentialRecoveryError, match='cancelled'):
        get_password({}, interaction=interaction)


def test_queued_request_cancellation_and_deadline():
    import threading
    from api_agent.auth.credentials import CredentialRecoveryError, get_password
    interaction = Interaction([])
    interaction.lock = threading.Lock()
    interaction.lock.acquire()
    interaction.cancel_event = threading.Event()
    try:
        with pytest.raises(CredentialRecoveryError, match='deadline'):
            get_password({}, interaction=interaction, recovery_timeout=.01)
        interaction.cancel_event.set()
        with pytest.raises(CredentialRecoveryError, match='cancelled'):
            get_password({}, interaction=interaction)
    finally:
        interaction.lock.release()


def test_incremental_terminal_commands_and_utf8_are_safe():
    from api_agent.auth.recovery import _TerminalText
    text = _TerminalText()
    assert text.feed(b'Hello\x1b[') == 'Hello'
    assert text.feed(b'32mworld\x1b]52;c;clipboard-secret') == 'world'
    assert text.feed(b'\x1b') == ''
    assert text.feed(b'\\safe\x1b]title\x07\x00\x7f\xc2\x9b2J') == 'safe'
    assert text.feed(b'\xc2\x9dhidden\x1bXhidden\x07\x1bZ\xc3') == ''
    assert text.feed(b'\xa9\n') == 'é\n'


@pytest.mark.parametrize('hook,pattern', [
    ('classify_login = 42', 'synchronous'),
    ('async def classify_login(stderr): pass', 'synchronous'),
    ('def classify_login(): pass', 'incompatible'),
])
def test_optional_classification_contract(tmp_path, hook, pattern):
    from api_agent.auth.managers import ManagerDefinition, ManagerError, PasswordManagerRegistry
    path = tmp_path / 'adapter.py'
    path.write_text('version=1\ndef command(source,kind,cli): return [cli]\ndef parse(output,kind): return output\n' + hook)
    registry = PasswordManagerRegistry((ManagerDefinition('third', 'Third', path, 'cli'),))
    with pytest.raises(ManagerError, match=pattern):
        registry.load('third')


def test_initial_retrieval_cancel_and_closed_stream_deadline():
    import threading
    from api_agent.auth.credentials import _run, CredentialRecoveryError
    cancel = threading.Event()
    timer = threading.Timer(.05, cancel.set)
    timer.start()
    try:
        with pytest.raises(CredentialRecoveryError, match='cancelled'):
            _run([sys.executable, '-c', 'import time; time.sleep(10)'], 2, cancel_event=cancel)
    finally:
        timer.join()
    with pytest.raises(CredentialError, match='timed out'):
        _run([sys.executable, '-c', 'import os,time;os.close(1);os.close(2);time.sleep(10)'], .1)


def test_exact_deployed_keeper_failure_and_lookup_exclusion():
    from api_agent.auth.password_managers.keeper import classify_login
    assert classify_login('Persistent login is not working in this non-interactive environment (possibly due to an IP/location change). Re-run Commander/Docker setup from this network, then restart the service.')
    assert not classify_login('find-password: Record not found')


def test_success_disposes_descendant_without_inherited_streams(tmp_path):
    import time
    from pathlib import Path
    from api_agent.auth.credentials import _run
    marker = tmp_path / 'child.pid'
    script = f'''import os,time
pid=os.fork()
if pid == 0:
 os.close(0);os.close(1);os.close(2);time.sleep(60);os._exit(0)
open({str(marker)!r}, 'w').write(str(pid))
print('value')
'''
    assert _run([sys.executable, '-c', script], 2) == 'value\n'
    stat = Path(f'/proc/{int(marker.read_text())}/stat')
    deadline = time.monotonic() + .3
    while stat.exists() and stat.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
        time.sleep(.01)
    assert not stat.exists() or stat.read_text().split()[2] == 'Z'


def test_shared_scope_does_not_carry_short_limit_into_human_recovery(tmp_path, monkeypatch):
    from api_agent.auth.credentials import get_password
    from api_agent.providers.progress import progress_sink, report_timeout_sync
    path = tmp_path / 'keeper'
    path.write_text(f'#!{sys.executable}\nimport sys,getpass,time\nif not sys.stdin.isatty():\n print("Persistent login failed", file=sys.stderr);sys.exit(1)\ntime.sleep(.1)\nassert getpass.getpass("Password: ") == "master-secret"\nprint("value")\n')
    path.chmod(0o700)
    source = dict(manager='keeper', cli_path=str(path), vault='vault', user='user')
    # The short budget measures retrieval, not interpreter startup. Force the
    # known logged-out precondition, then exercise actual PTY recovery beyond it.
    from api_agent.auth.credentials import CredentialLoginRequiredError
    def logged_out(*args, **kwargs):
        raise CredentialLoginRequiredError('Keeper login required.')
    monkeypatch.setattr('api_agent.auth.credentials._run', logged_out)
    records = []
    with progress_sink(records.append), report_timeout_sync('Enclosing operation', 2):
        assert get_password(source, timeout=.08, interaction=Interaction([Reply('input', 'master-secret')])) == 'value'
    starts = [record for record in records if record.active]
    retrieval = next(record for record in starts if record.name == 'Password-manager retrieval')
    recovery = next(record for record in starts if record.name == 'Password-manager recovery')
    assert retrieval.timeout == .08
    assert recovery.timeout is None
    assert recovery.parent_id == retrieval.parent_id


def test_human_recovery_respects_inherited_parent_deadline(tmp_path):
    from api_agent.auth.credentials import get_password, CredentialRecoveryError
    from api_agent.providers.progress import report_timeout_sync
    path = tmp_path / 'keeper'
    path.write_text(f'#!{sys.executable}\nimport sys,time\nif not sys.stdin.isatty():\n print("Persistent login failed", file=sys.stderr);sys.exit(1)\ntime.sleep(10)\n')
    path.chmod(0o700)
    source = dict(manager='keeper', cli_path=str(path), vault='vault', user='user')
    with report_timeout_sync('Enclosing operation', .15):
        with pytest.raises(CredentialRecoveryError, match='deadline'):
            get_password(source, interaction=Interaction([]))


def test_recovery_rejects_unsupported_platform(monkeypatch):
    from types import SimpleNamespace
    from api_agent.auth import recovery
    monkeypatch.setattr(recovery, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(CredentialError, match='POSIX'):
        recover([], Interaction([]), 'Keeper', 1)


def test_start_failure_closes_allocated_terminal(monkeypatch):
    from api_agent.auth import recovery
    original = os.openpty
    owned = []
    def allocate():
        pair = original()
        owned.extend(pair)
        return pair
    def fail(*args, **kwargs):
        raise OSError('private helper detail')
    monkeypatch.setattr(recovery.os, 'openpty', allocate)
    monkeypatch.setattr(recovery.subprocess, 'Popen', fail)
    interaction = Interaction([])
    with pytest.raises(CredentialError, match='disconnected') as error:
        recover([sys.executable, '-c', 'pass'], interaction, 'Keeper', 1)
    assert 'private' not in str(error.value)
    assert interaction.channel.closed
    for descriptor in owned:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_terminal_io_failure_disposes_process(monkeypatch):
    import errno
    from api_agent.auth import recovery
    original_allocate = os.openpty
    original_read = os.read
    masters = []
    def allocate():
        pair = original_allocate()
        masters.append(pair[0])
        return pair
    def read(descriptor, size):
        if descriptor in masters:
            raise OSError(errno.EINVAL, 'private terminal failure')
        return original_read(descriptor, size)
    monkeypatch.setattr(recovery.os, 'openpty', allocate)
    monkeypatch.setattr(recovery.os, 'read', read)
    interaction = Interaction([])
    with pytest.raises(CredentialError, match='disconnected'):
        recover([sys.executable, '-c', "import sys,time;print('prompt',file=sys.stderr);time.sleep(10)"], interaction, 'Keeper', 1)
    assert interaction.channel.closed


def test_already_exited_group_cleanup_is_safe(monkeypatch):
    from api_agent.auth import recovery
    from api_agent.auth.owned_process import OwnedProcess
    original = OwnedProcess.dispose
    calls = []
    def dispose(process):
        if process.writer is None:
            return
        calls.append(process.pid)
        original(process)
        raise ProcessLookupError('synthetic concurrent exit after group disposal')
    monkeypatch.setattr(OwnedProcess, 'dispose', dispose)
    interaction = Interaction([Reply('cancel')])
    with pytest.raises(CredentialError, match='cancelled'):
        recover([sys.executable, '-c', "import getpass;getpass.getpass('Password: ')"], interaction, 'Keeper', 1)
    assert len(calls) == 1
    assert interaction.channel.closed


def test_terminal_only_escape_chunks_are_not_relayed():
    interaction = Interaction([])
    script = "import os,time;os.write(2,b'\\x1b[');time.sleep(.03);os.write(2,b'32m');print('value')"
    assert recover([sys.executable, '-c', script], interaction, 'Keeper', 1) == 'value\n'
    assert interaction.channel.output == ''


def test_inherited_deadline_bounds_queue_wait():
    import threading
    import time
    from api_agent.auth.credentials import get_password, CredentialRecoveryError
    from api_agent.providers.progress import report_timeout_sync
    interaction = Interaction([])
    interaction.lock = threading.Lock()
    interaction.lock.acquire()
    started = time.monotonic()
    try:
        with report_timeout_sync('Enclosing operation', .05):
            with pytest.raises(CredentialRecoveryError, match='deadline'):
                get_password({}, interaction=interaction)
        assert time.monotonic() - started < .2
    finally:
        interaction.lock.release()


def test_terminal_initialization_failure_closes_allocated_descriptors(monkeypatch):
    import termios
    from api_agent.auth import recovery
    original = os.openpty
    owned = []
    def allocate():
        pair = original()
        owned.extend(pair)
        return pair
    def fail(*args):
        raise termios.error('private terminal setup')
    monkeypatch.setattr(recovery.os, 'openpty', allocate)
    monkeypatch.setattr(termios, 'tcgetattr', fail)
    with pytest.raises(CredentialError, match='disconnected'):
        recover([sys.executable, '-c', 'pass'], Interaction([]), 'Keeper', 1)
    for descriptor in owned:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_post_reap_stream_close_failure_never_resignals_released_group(monkeypatch):
    from api_agent.auth import recovery
    original_popen = recovery.subprocess.Popen
    from api_agent.auth.owned_process import OwnedProcess
    original_dispose = OwnedProcess.dispose
    original_allocate = os.openpty
    groups = []
    owned = []
    def allocate():
        pair = original_allocate()
        owned.extend(pair)
        return pair
    def dispose(process):
        if process.writer is not None:
            groups.append(process.pid)
        original_dispose(process)
    class Stream:
        def __init__(self, original):
            self.original = original
        def fileno(self):
            return self.original.fileno()
        def close(self):
            self.original.close()
            raise OSError('private post-reap close error')
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        process.stdout = Stream(process.stdout)
        return process
    monkeypatch.setattr(recovery.os, 'openpty', allocate)
    monkeypatch.setattr(OwnedProcess, 'dispose', dispose)
    monkeypatch.setattr(recovery.subprocess, 'Popen', popen)
    with pytest.raises(CredentialError, match='disconnected'):
        recover([sys.executable, '-c', "print('value')"], Interaction([]), 'Keeper', 1)
    assert len(groups) == 1
    for descriptor in owned:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_successful_already_exited_group_disposal(monkeypatch):
    from api_agent.auth import recovery
    from api_agent.auth.owned_process import OwnedProcess
    original = OwnedProcess.dispose
    calls = []
    def dispose(process):
        if process.writer is None:
            return
        calls.append(process.pid)
        original(process)
        raise ProcessLookupError('synthetic concurrent group exit')
    monkeypatch.setattr(OwnedProcess, 'dispose', dispose)
    assert recover([sys.executable, '-c', "print('value')"], Interaction([]), 'Keeper', 1) == 'value\n'
    assert len(calls) == 1


def test_selector_initialization_failure_closes_terminal(monkeypatch):
    from api_agent.auth import recovery
    original = os.openpty
    descriptors = []
    def allocate():
        pair = original()
        descriptors.extend(pair)
        return pair
    def fail():
        raise OSError('private selector setup')
    monkeypatch.setattr(recovery.os, 'openpty', allocate)
    monkeypatch.setattr(recovery.selectors, 'DefaultSelector', fail)
    with pytest.raises(CredentialError, match='disconnected'):
        recover([sys.executable, '-c', 'pass'], Interaction([]), 'Keeper', 1)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize('success', [True, False])
def test_eof_after_valid_input_is_forwarded_to_helper(success):
    class FiniteChannel(Channel):
        def read(self, timeout):
            return next(self.replies, None)
    interaction = Interaction([])
    interaction.channel = FiniteChannel([Reply('input', 'valid'), Reply('eof')])
    script = (
        "import sys,time; assert sys.stdin.readline() == 'valid\\n'; "
        "assert sys.stdin.readline() == ''; "
        + ("print('credential')" if success else 'sys.exit(7)')
    )
    if success:
        assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'credential\n'
    else:
        with pytest.raises(CredentialError, match='input ended'):
            recover([sys.executable, '-c', script], interaction, 'Keeper', 2)
    assert interaction.channel.closed


def test_finite_terminal_input_is_consumed_once_per_invocation():
    import io
    from api_agent.auth.terminal_interaction import TerminalInteraction
    reader, writer = os.pipe()
    os.write(writer, b'first\nsecond\n')
    os.close(writer)
    with os.fdopen(reader) as stream:
        interaction = TerminalInteraction(stream, io.StringIO())
        script = "import sys,time;sys.stderr.write('Password: ');sys.stderr.flush();value=sys.stdin.readline();time.sleep(.1);sys.stdout.write(value)"
        assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'first\n'
        assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'second\n'
        with pytest.raises(CredentialError, match='input ended'):
            recover([sys.executable, '-c', script], interaction, 'Keeper', 2)


def test_cancellation_remains_responsive_after_forwarded_eof():
    class FiniteChannel(Channel):
        def read(self, timeout):
            return next(self.replies, None)
    interaction = Interaction([])
    interaction.channel = FiniteChannel([
        Reply('input', 'valid'), Reply('eof'), Reply('eof'),
        Reply('input', 'discarded-after-eof'), Reply('cancel')])
    script = "import sys,time;sys.stdin.readline();sys.stdin.readline();time.sleep(60)"
    with pytest.raises(CredentialError, match='cancelled'):
        recover([sys.executable, '-c', script], interaction, 'Keeper', 2)
    assert interaction.channel.closed


@pytest.mark.parametrize('progress', [
    "sys.stderr.write('Authentication succeeded\\n');sys.stderr.flush();",
    "sys.stderr.write('Authentication ');sys.stderr.flush();time.sleep(.02);"
    "sys.stderr.write('succeeded\\n');sys.stderr.flush();",
])
def test_terminal_progress_does_not_consume_next_invocation_reply(progress):
    import io
    from api_agent.auth.terminal_interaction import TerminalInteraction
    reader, writer = os.pipe()
    try:
        os.write(writer, b'first\nsecond\n')
        with os.fdopen(reader) as stream:
            interaction = TerminalInteraction(stream, io.StringIO())
            script = (
                "import sys,time;sys.stderr.write('Pass');sys.stderr.flush();time.sleep(.02);"
                "sys.stderr.write('word: ');sys.stderr.flush();value=sys.stdin.readline();"
                + progress + "time.sleep(.05);sys.stdout.write(value)"
            )
            assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'first\n'
            assert recover([sys.executable, '-c', script], interaction, 'Keeper', 2) == 'second\n'
    finally:
        os.close(writer)
