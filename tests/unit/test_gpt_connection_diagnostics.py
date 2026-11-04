"""Connection file failures retain only fixed, bounded diagnostic context."""
import errno
import os
import stat
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.gpt import connections
from api_agent.llm_providers.gpt.contracts import AuthError


@pytest.fixture
def private_file(tmp_path):
    store = connections.ConnectionStore(tmp_path / 'private')
    store.root.mkdir(mode=0o700)
    path = store.root / 'synthetic.lock'
    path.write_bytes(b'private contents must not be reported')
    path.chmod(0o600)
    return store, path


@pytest.mark.parametrize('stage,index,value', [
    ('file_type', 0, stat.S_IFDIR | 0o600),
    ('owner', 4, -1),
    ('permissions', 0, stat.S_IFREG | 0o644),
    ('link_count', 3, 2),
])
def test_invalid_metadata_keeps_rejection_and_fixed_reason(private_file, monkeypatch, stage, index, value):
    store, path = private_file
    original = os.fstat
    opened = []
    def inspect_file(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            opened.append(fd)
            fields = list(info)
            fields[index] = value
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(connections, 'os', SimpleNamespace(**(vars(os) | {'fstat': inspect_file})))

    with pytest.raises(AuthError) as failure:
        fd = store._open(path, os.O_RDONLY)
        os.close(fd)

    assert str(failure.value) == 'invalid_connection'
    assert failure.value.__notes__ == ['Connection-store file check failed: stage=' + stage + '.']
    assert failure.value.__suppress_context__ and failure.value.__cause__ is None
    for fd in opened:
        with pytest.raises(OSError) as closed:
            original(fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize('stage', ['open', 'metadata'])
@pytest.mark.parametrize('number', [errno.EMFILE, 0, 65535, -1, 65536, 10 ** 100, True, None, 'secret-errno'])
def test_system_failure_never_releases_private_exception_text(private_file, monkeypatch, stage, number):
    store, path = private_file
    private = 'synthetic-private-credential'
    error = OSError(errno.EIO, private, private)
    error.errno = number
    real_open, real_stat = os.open, os.fstat
    opened = []
    def open_file(name, *args, **kwargs):
        if name == path.name and stage == 'open':
            raise error
        fd = real_open(name, *args, **kwargs)
        if name == path.name:
            opened.append(fd)
        return fd
    def inspect_file(fd):
        info = real_stat(fd)
        if stat.S_ISREG(info.st_mode) and stage == 'metadata':
            raise error
        return info
    monkeypatch.setattr(connections, 'os', SimpleNamespace(**(
        vars(os) | {'open': open_file, 'fstat': inspect_file})))

    with pytest.raises(AuthError) as failure:
        store._open(path, os.O_RDONLY)

    expected = 'Connection-store file check failed: stage=' + stage
    if type(number) is int and 0 <= number <= 65535:
        expected += '; errno=' + str(number)
    assert str(failure.value) == 'invalid_connection'
    assert failure.value.__notes__ == [expected + '.']
    assert private not in repr(failure.value) + ''.join(failure.value.__notes__)
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    for fd in opened:
        with pytest.raises(OSError):
            real_stat(fd)


@pytest.mark.parametrize('metadata_fails,failed_closes', [
    (True, {'file'}),
    (True, {'directory'}),
    (True, {'file', 'directory'}),
    (False, {'directory'}),
])
def test_cleanup_failure_cannot_replace_safe_file_error(private_file, monkeypatch, metadata_fails, failed_closes):
    store, path = private_file
    real_open, real_stat, real_close = os.open, os.fstat, os.close
    opened, closed = [], []
    def open_file(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def inspect_file(fd):
        info = real_stat(fd)
        if metadata_fails and stat.S_ISREG(info.st_mode):
            raise OSError(errno.EMFILE, 'synthetic-private-metadata')
        return info
    def close_file(fd):
        kind = 'file' if stat.S_ISREG(real_stat(fd).st_mode) else 'directory'
        closed.append(fd)
        real_close(fd)
        if kind in failed_closes:
            raise OSError(errno.EIO, 'synthetic-private-cleanup')
    monkeypatch.setattr(connections, 'os', SimpleNamespace(**(
        vars(os) | {'open': open_file, 'fstat': inspect_file, 'close': close_file})))

    with pytest.raises(AuthError) as failure:
        store._open(path, os.O_RDONLY)

    stage, number = ('metadata', errno.EMFILE) if metadata_fails else ('directory_close', errno.EIO)
    assert str(failure.value) == 'invalid_connection'
    assert failure.value.__notes__ == [f'Connection-store file check failed: stage={stage}; errno={number}.']
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert sorted(closed) == sorted(opened) and len(set(closed)) == len(closed)
    for fd in opened:
        with pytest.raises(OSError):
            real_stat(fd)


def test_directory_acquisition_cleanup_error_is_safe(private_file, monkeypatch):
    store, path = private_file
    real_open, real_close = os.open, os.close
    opened, closed = [], []
    def open_file(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def inspect_file(fd):
        raise OSError(errno.EMFILE, 'synthetic-private-directory-metadata')
    def close_file(fd):
        closed.append(fd)
        real_close(fd)
        raise OSError(errno.EIO, 'synthetic-private-directory-cleanup')
    monkeypatch.setattr(connections, 'os', SimpleNamespace(**(
        vars(os) | {'open': open_file, 'fstat': inspect_file, 'close': close_file})))

    with pytest.raises(AuthError) as failure:
        store._open(path, os.O_RDONLY)

    assert str(failure.value) == 'invalid_connection'
    assert failure.value.__notes__ == [f'Connection-store file check failed: stage=directory_open; errno={errno.EIO}.']
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert len(opened) == 1 and closed == opened
