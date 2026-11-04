"""Only validated login probes may display stdout through private recovery."""
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from api_agent.auth import credentials as c, recovery
from api_agent.auth.interaction import Reply
from api_agent.auth.login_recovery import recover_login


class Channel:
    def __init__(self):
        self.output = ''
        self.sent = False
        self.closed = False
    def write(self, text):
        self.output += text
    def read(self, timeout):
        if 'Password:' in self.output and not self.sent:
            self.sent = True
            return Reply('input', 'entered-secret')
        return None
    def close(self):
        self.closed = True


class Interaction:
    def __init__(self): self.channel = Channel()
    def open(self, label): return self.channel


def executable(tmp_path, body):
    path = tmp_path / 'synthetic-cli'
    path.write_text(f'#!{sys.executable}\nimport os,sys,time,getpass\n{body}\n')
    path.chmod(0o700)
    return str(path)


def test_ordinary_password_recovers_with_probe_then_private_read(tmp_path, capsys, caplog):
    marker = tmp_path / 'session'
    cli = executable(tmp_path, f'''
from pathlib import Path
session = Path({str(marker)!r})
if sys.argv[1:] == ['whoami']:
    assert sys.stdin.isatty() and sys.stderr.isatty() and not sys.stdout.isatty()
    print('Touch your security key to continue', flush=True)
    assert getpass.getpass('Password: ') == 'entered-secret'
    session.touch()
    print('Signed in')
elif session.exists():
    assert not sys.stdin.isatty() and sys.stdin.read() == ''
    print('private-credential-result')
else:
    print('login required', file=sys.stderr)
    sys.exit(1)
''')
    interaction = Interaction()
    source = dict(manager='keeper', cli_path=cli, vault='synthetic-vault', user='synthetic-user')
    assert c.get_password(source, timeout=3, interaction=interaction, recovery_timeout=5) == 'private-credential-result'
    assert 'Touch your security key to continue' in interaction.channel.output
    assert 'Signed in' in interaction.channel.output
    assert 'entered-secret' not in interaction.channel.output
    assert 'private-credential-result' not in interaction.channel.output
    assert interaction.channel.closed and interaction.channel.sent
    captured = capsys.readouterr()
    assert not captured.out and not captured.err and not caplog.text


@pytest.mark.parametrize('argv', [None, (), [], ['/other', 'whoami'], ['/cli', 'get', '--unmask'],
                                 ['/cli', 'whoami', '--extra'], ['/cli', 1], ['/cli', 'bad\0value']])
def test_unsafe_adapter_probe_is_rejected_before_recovery(argv, monkeypatch):
    source = NS(manager='synthetic', _registry=NS(get=Mock()))
    adapter = NS(login_probe=lambda *args: argv)
    invoke = Mock()
    monkeypatch.setattr(recovery, 'recover', invoke)
    with pytest.raises(c.CredentialError):
        recover_login(adapter, source, '/cli', Interaction(), 5)
    invoke.assert_not_called()
    source._registry.get.assert_not_called()


@pytest.mark.parametrize('argv', [None, (), [], ['/cli'], ['', 'whoami'], ['/cli\0bad', 'whoami'],
                                 ['/cli', 'find-password'], ['/cli', 'whoami', '--extra']])
def test_transport_rejects_unsafe_login_mode_before_open(argv):
    interaction = NS(open=Mock())
    with pytest.raises(c.CredentialRecoveryError, match='unsafe login probe'):
        recovery.recover(argv, interaction, 'Synthetic', 1, login_only=True)
    interaction.open.assert_not_called()


def test_invalid_recovery_mode_cannot_enable_guidance():
    interaction = NS(open=Mock())
    with pytest.raises(c.CredentialRecoveryError, match='mode is invalid'):
        recovery.recover(['/cli', 'whoami'], interaction, 'Synthetic', 1, login_only='True')
    interaction.open.assert_not_called()


def test_absent_probe_preserves_optional_contract():
    assert recover_login(NS(), None, '/cli', None, 1) is False


@pytest.mark.parametrize('failure', [RuntimeError('private-output'), c.CredentialError('safe failure')])
def test_probe_hook_failure_is_safe(failure):
    adapter = NS(login_probe=Mock(side_effect=failure))
    with pytest.raises(c.CredentialError) as caught:
        recover_login(adapter, None, '/cli', None, 1)
    assert 'private-output' not in str(caught.value)


def test_login_stdout_and_terminal_have_independent_incremental_filters(tmp_path):
    cli = executable(tmp_path, r'''
assert sys.argv[1:] == ['whoami']
assert sys.stdin.isatty() and sys.stderr.isatty() and not sys.stdout.isatty()
sys.stdout.buffer.write(b'\x1b[31')
sys.stdout.flush()
sys.stderr.write('Password: ')
sys.stderr.flush()
time.sleep(.02)
sys.stdout.buffer.write(b'mTouch \xe2')
sys.stdout.flush()
time.sleep(.02)
sys.stdout.buffer.write(b'\x9c\x93\x1b[0m')
sys.stdout.flush()
assert sys.stdin.readline().strip() == 'entered-secret'
''')
    interaction = Interaction()
    assert recovery.recover([cli, 'whoami'], interaction, 'Synthetic', 2, login_only=True) == ''
    assert 'Password: ' in interaction.channel.output and 'Touch ✓' in interaction.channel.output
    assert '\x1b' not in interaction.channel.output and '\ufffd' not in interaction.channel.output


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_probe_guidance_limits_both_streams_and_disposes(tmp_path, monkeypatch, stream):
    monkeypatch.setattr(recovery, '_LIMIT', 32)
    cli = executable(tmp_path, f"sys.{stream}.write('x'*33)\nsys.{stream}.flush()\ntime.sleep(30)")
    interaction = Interaction()
    with pytest.raises(c.CredentialRecoveryError, match='exceeded its limit'):
        recovery.recover([cli, 'whoami'], interaction, 'Synthetic', 2, login_only=True)
    assert interaction.channel.closed


@pytest.fixture
def ordinary_adapter(monkeypatch):
    adapter = NS(command=lambda source, kind, cli: ['/cli', 'read-private', kind],
                 parse=lambda output, kind: output.rstrip('\n'),
                 login_probe=lambda source, cli: [cli, 'whoami'])
    registry = NS(load=lambda manager: adapter, get=lambda manager: NS(label='Synthetic manager'))
    source = NS(manager='synthetic', cli_path='/cli', _registry=registry)
    monkeypatch.setattr(c.CredentialSource, 'from_mapping', lambda *args, **kwargs: source)
    monkeypatch.setattr(c, 'resolve_cli_path', lambda path: '/cli')
    return adapter


@pytest.mark.parametrize('kind', ['password', 'username', 'totp'])
def test_ordinary_probe_retries_only_original_private_command_with_remaining_budget(ordinary_adapter, monkeypatch, kind):
    clock, calls = [0.0], []
    monkeypatch.setattr(c, 'time', NS(monotonic=lambda: clock[0]))
    def run(argv, timeout, **kwargs):
        calls.append((argv, timeout))
        clock[0] += 2
        if len(calls) == 1:
            raise c.CredentialLoginRequiredError('locked')
        return 'private-result\n'
    def recover(argv, interaction, label, timeout, **kwargs):
        assert argv == ['/cli', 'whoami'] and kwargs == {'login_only': True}
        assert timeout == 8
        clock[0] += 100
        return 'not-a-credential'
    monkeypatch.setattr(c, '_run', run)
    monkeypatch.setattr(recovery, 'recover', recover)
    assert c._retrieve({}, kind, 5, interaction=Interaction(), recovery_timeout=10) == 'private-result'
    assert [argv for argv, _ in calls] == [['/cli', 'read-private', kind]] * 2
    assert 4.9 < calls[0][1] <= 5
    assert 2.9 < calls[1][1] <= 3


def test_adapter_without_probe_keeps_original_recovery_stdout_private(ordinary_adapter, monkeypatch):
    del ordinary_adapter.login_probe
    run = Mock(side_effect=c.CredentialLoginRequiredError('locked'))
    recover = Mock(return_value='private-result\n')
    monkeypatch.setattr(c, '_run', run)
    monkeypatch.setattr(recovery, 'recover', recover)
    assert c._retrieve({}, 'password', 5, interaction=Interaction()) == 'private-result'
    run.assert_called_once()
    assert recover.call_args.args[0] == ['/cli', 'read-private', 'password']
    assert not recover.call_args.kwargs


def test_ordinary_unsafe_probe_never_falls_back_to_credential_command(ordinary_adapter, monkeypatch):
    ordinary_adapter.login_probe = lambda *args: ['/cli', 'get', '--unmask']
    run = Mock(side_effect=c.CredentialLoginRequiredError('locked'))
    recover = Mock()
    monkeypatch.setattr(c, '_run', run)
    monkeypatch.setattr(recovery, 'recover', recover)
    with pytest.raises(c.CredentialRecoveryError, match='unsafe login probe'):
        c._retrieve({}, 'password', 5, interaction=Interaction())
    run.assert_called_once()
    recover.assert_not_called()


def test_private_retry_does_not_restart_exhausted_retrieval_budget(ordinary_adapter, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(c, 'time', NS(monotonic=lambda: clock[0]))
    def run(*args, **kwargs):
        clock[0] = 5
        raise c.CredentialLoginRequiredError('locked')
    monkeypatch.setattr(c, '_run', run)
    monkeypatch.setattr(recovery, 'recover', Mock(return_value='ignored'))
    with pytest.raises(c.CredentialError, match='retrieval deadline expired'):
        c._retrieve({}, 'password', 5, interaction=Interaction())


def test_whoami_shape_alone_does_not_grant_stdout_guidance(tmp_path):
    cli = executable(tmp_path, "print('private-stdout-value')")
    interaction = Interaction()
    result = recovery.recover([cli, 'whoami'], interaction, 'Synthetic', 2)
    assert 'private-stdout-value' not in interaction.channel.output
    assert result == 'private-stdout-value\n'


def test_probe_comparison_spoof_cannot_grant_credential_stdout():
    class CredentialCommand:
        def __eq__(self, other): return True
        def __fspath__(self): return 'find-password'
    interaction = NS(open=Mock())
    with pytest.raises(c.CredentialRecoveryError, match='unsafe login probe'):
        recovery.recover(['/cli', CredentialCommand()], interaction, 'Synthetic', 1, login_only=True)
    interaction.open.assert_not_called()


def test_unflushed_probe_guidance_arrives_before_independent_release(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONUNBUFFERED', raising=False)
    monkeypatch.setenv('SYNTHETIC_PRESERVED_SETTING', 'preserved-value')
    release, pid_file = tmp_path / 'release', tmp_path / 'pid'
    cli = executable(tmp_path, f'''
from pathlib import Path
assert sys.argv[1:] == ['whoami']
assert sys.stdin.isatty() and sys.stderr.isatty() and not sys.stdout.isatty()
assert os.environ['PYTHONUNBUFFERED'] == '1'
assert os.environ['SYNTHETIC_PRESERVED_SETTING'] == 'preserved-value'
Path({str(pid_file)!r}).write_text(str(os.getpid()))
print('Touch your security key now')
while not Path({str(release)!r}).exists(): time.sleep(.01)
''')
    interaction = Interaction()
    original_write = interaction.channel.write
    def write(text):
        original_write(text)
        if 'Touch your security key now' in interaction.channel.output:
            release.touch()
    interaction.channel.write = write
    assert recovery.recover([cli, 'whoami'], interaction, 'Synthetic', 2, login_only=True) == ''
    assert release.exists() and interaction.channel.closed
    assert 'PYTHONUNBUFFERED' not in os.environ
    assert os.environ['SYNTHETIC_PRESERVED_SETTING'] == 'preserved-value'
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize('inherited', [None, '0'])
def test_private_recovery_keeps_inherited_buffering_environment(tmp_path, monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv('PYTHONUNBUFFERED', raising=False)
    else:
        monkeypatch.setenv('PYTHONUNBUFFERED', inherited)
    cli = executable(tmp_path, "print(os.environ.get('PYTHONUNBUFFERED', 'unset'))")
    interaction = Interaction()
    assert recovery.recover([cli, 'whoami'], interaction, 'Synthetic', 2) == (inherited or 'unset') + '\n'
    assert not interaction.channel.output
    assert os.environ.get('PYTHONUNBUFFERED') == inherited
