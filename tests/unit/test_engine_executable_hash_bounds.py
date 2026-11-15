"""Engine executable identity checks must remain bounded and preserve argv[0]."""
from hashlib import sha256
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.providers import progress
from api_agent.runtime import engine_targets
from api_agent.runtime.engine_targets import EngineTarget, EngineTargetError


def _verifier(environment, *, executable, cancelled):
    return 'a' * 64, 'unix:///engine.sock'


@pytest.mark.parametrize('operation', ['capture', 'verify'])
def test_fifo_does_not_block_target_identity_check(tmp_path, operation):
    executable = tmp_path / 'engine'
    executable.write_text('engine')
    digest = sha256(executable.read_bytes()).hexdigest()
    executable.unlink()
    os.mkfifo(executable, 0o700)
    script = '''
import sys
from api_agent.runtime.engine_targets import EngineTarget, EngineTargetError
target = EngineTarget('engine', sys.argv[1], sys.argv[2], 'a' * 64,
                      {'DOCKER_HOST': 'unix:///engine.sock'})
try:
    verifier = lambda *args, **kwargs: ('a' * 64, 'unix:///engine.sock')
    if sys.argv[3] == 'capture':
        EngineTarget.capture('engine', sys.argv[1], {}, verifier=verifier)
    else:
        target.verify(verifier=verifier)
except EngineTargetError:
    sys.exit(0)
sys.exit(1)
'''
    try:
        result = subprocess.run([sys.executable, '-c', script, str(executable), digest, operation],
                                capture_output=True, text=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail('Engine verification blocked opening a FIFO')
    assert result.returncode == 0, result.stderr


def test_executable_symlink_preserves_original_invocation_path(tmp_path):
    executable = tmp_path / 'dispatcher'
    executable.write_text('multicall')
    executable.chmod(0o700)
    invocation = tmp_path / 'docker'
    invocation.symlink_to(executable)
    paths = []
    def verify(environment, *, executable, cancelled):
        paths.append(executable)
        return _verifier(environment, executable=executable, cancelled=cancelled)

    target = EngineTarget.capture('engine', str(invocation), {}, verifier=verify)
    target.verify(verifier=verify)

    assert target.executable == str(invocation)
    assert paths == [str(invocation), str(invocation)]


def test_capture_rejects_oversized_executable_before_reading(tmp_path, monkeypatch):
    executable = tmp_path / 'engine'
    executable.write_text('small stand-in')
    executable.chmod(0o700)
    real_fstat = os.fstat
    file_inode = executable.stat().st_ino
    def fstat(fd):
        result = real_fstat(fd)
        if result.st_ino == file_inode:
            return SimpleNamespace(st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino,
                st_size=512 * 1024 * 1024 + 1, st_mtime_ns=result.st_mtime_ns, st_ctime_ns=result.st_ctime_ns)
        return result
    monkeypatch.setattr(engine_targets.os, 'fstat', fstat)
    monkeypatch.setattr(engine_targets.os, 'read', lambda *args: pytest.fail('oversized executable read'))
    invoked = []
    def verifier(*args, **kwargs):
        invoked.append(True)
        return _verifier(*args, **kwargs)

    with pytest.raises(EngineTargetError, match='verification failed'):
        EngineTarget.capture('engine', str(executable), {}, verifier=verifier)
    assert not invoked


def test_capture_respects_expired_inherited_deadline(tmp_path, monkeypatch):
    executable = tmp_path / 'engine'
    executable.write_text('engine')
    executable.chmod(0o700)
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(progress, '_clock', lambda: clock.value)
    invoked = []
    def verifier(*args, **kwargs):
        invoked.append(True)
        return _verifier(*args, **kwargs)
    with progress.report_timeout_sync('Parent operation', 1):
        clock.value = 2.0
        with pytest.raises(EngineTargetError, match='verification failed'):
            EngineTarget.capture('engine', str(executable), {}, verifier=verifier)
    assert not invoked


def test_capture_checks_cancellation_before_hashing(tmp_path):
    executable = tmp_path / 'engine'
    executable.write_text('engine')
    executable.chmod(0o700)
    invoked = []
    def verifier(*args, **kwargs):
        invoked.append(True)
        return _verifier(*args, **kwargs)

    with pytest.raises(EngineTargetError, match='verification failed'):
        EngineTarget.capture('engine', str(executable), {}, cancelled=lambda: True, verifier=verifier)
    assert not invoked


@pytest.mark.parametrize('interruption', ['deadline', 'cancellation'])
def test_capture_checks_interruption_during_streaming_hash(tmp_path, monkeypatch, interruption):
    executable = tmp_path / 'engine'
    executable.write_bytes(b'x' * (2 * 1024 * 1024))
    executable.chmod(0o700)
    state = SimpleNamespace(now=0.0, reads=0)
    monkeypatch.setattr(progress, '_clock', lambda: state.now)
    real_read = os.read
    def read(fd, size):
        result = real_read(fd, size)
        state.reads += 1
        if interruption == 'deadline':
            state.now = 2.0
        return result
    monkeypatch.setattr(engine_targets.os, 'read', read)
    invoked = []
    def verifier(*args, **kwargs):
        invoked.append(True)
        return _verifier(*args, **kwargs)
    cancelled = lambda: interruption == 'cancellation' and state.reads > 0

    with progress.report_timeout_sync('Parent operation', 1):
        with pytest.raises(EngineTargetError, match='verification failed'):
            EngineTarget.capture('engine', str(executable), {}, cancelled=cancelled, verifier=verifier)
    assert state.reads >= 1
    assert not invoked


def test_capture_refuses_executable_mutation_during_hash(tmp_path, monkeypatch):
    executable = tmp_path / 'engine'
    executable.write_bytes(b'x' * (2 * 1024 * 1024))
    executable.chmod(0o700)
    real_read = os.read
    changed = []
    def read(fd, size):
        chunk = real_read(fd, size)
        if not changed:
            with executable.open('ab') as stream:
                stream.write(b'changed')
            changed.append(True)
        return chunk
    monkeypatch.setattr(engine_targets.os, 'read', read)
    invoked = []
    def verifier(*args, **kwargs):
        invoked.append(True)
        return _verifier(*args, **kwargs)

    with pytest.raises(EngineTargetError, match='verification failed'):
        EngineTarget.capture('engine', str(executable), {}, verifier=verifier)
    assert changed and not invoked


def test_executable_growth_cannot_exceed_streaming_identity_bound(tmp_path, monkeypatch):
    executable = tmp_path / 'engine'
    executable.write_bytes(b'engine')
    executable.chmod(0o700)
    monkeypatch.setattr(engine_targets, '_MAX_EXECUTABLE_BYTES', 8)
    real_read = os.read
    grew = []

    def read(fd, size):
        chunk = real_read(fd, size)
        if not grew:
            with executable.open('ab') as stream:
                stream.write(b'growth beyond the bound')
            grew.append(True)
        return chunk

    monkeypatch.setattr(engine_targets.os, 'read', read)

    with pytest.raises(ValueError, match=r'^Invalid engine executable\.$'):
        engine_targets._executable_digest(str(executable), cancelled=lambda: False)

    assert grew == [True]
