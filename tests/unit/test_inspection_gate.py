"""Publication handoff rejects absent, unsafe and premature readiness."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing import inspection_gate as gate
from api_agent.testing.inspection import IntegrationInspection


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events.jsonl'))
    return tmp_path / gate.MARKER


def inspection(directory, *, closed=False):
    handle = object.__new__(IntegrationInspection)
    handle.closed = closed
    handle.directory = SimpleNamespace(temporary=SimpleNamespace(name=str(directory)))
    return handle


def test_publisher_opens_only_live_worker_channels_after_finalization(channel):
    assert not gate.manual_accounting_ready()
    gate.publish_manual_accounting_ready([object(), inspection(channel.parent, closed=True)])
    assert not channel.exists()
    gate.publish_manual_accounting_ready([inspection(channel.parent)])
    assert json.loads(channel.read_text()) == gate.FINALIZED
    assert channel.stat().st_mode & 0o077 == 0
    assert gate.manual_accounting_ready()


def test_standalone_worker_has_no_implicit_ready_state(monkeypatch):
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    assert not gate.manual_accounting_ready()


@pytest.mark.parametrize('body', ['{}', '[]', 'null', '{',
    '{"version":true,"status":"finalized"}', '{"version":2,"status":"finalized"}',
    '{"version":1,"status":"pending"}', '{"version":1,"status":"finalized","extra":0}',
    'x' * 65537])
def test_invalid_marker_never_allows_manual_requests(channel, body):
    channel.write_text(body)
    channel.chmod(0o600)
    assert not gate.manual_accounting_ready()


@pytest.mark.parametrize('unsafe', ['fifo', 'symlink', 'directory', 'writable'])
def test_untrusted_marker_never_allows_manual_requests(channel, unsafe):
    if unsafe == 'fifo':
        os.mkfifo(channel)
    elif unsafe == 'symlink':
        target = channel.with_name('target')
        target.write_text(json.dumps(gate.FINALIZED))
        channel.symlink_to(target)
    elif unsafe == 'directory':
        channel.mkdir()
    else:
        channel.write_text(json.dumps(gate.FINALIZED))
        channel.chmod(0o666)
    assert not gate.manual_accounting_ready()


def test_missing_parent_never_allows_manual_requests(tmp_path, monkeypatch):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'absent' / 'events'))
    assert not gate.manual_accounting_ready()


def test_metadata_read_failure_stays_unavailable(channel, monkeypatch):
    monkeypatch.setattr(gate, '_read', Mock(side_effect=ValueError('invalid metadata')))
    assert not gate.manual_accounting_ready()


def test_marker_publication_failure_is_not_reported_ready(channel, monkeypatch):
    monkeypatch.setattr(gate, '_write', Mock(side_effect=OSError('cannot publish')))
    with pytest.raises(OSError, match='cannot publish'):
        gate.publish_manual_accounting_ready([inspection(channel.parent)])
    assert not channel.exists()


@pytest.mark.parametrize('target', ['directory', 'marker'])
@pytest.mark.parametrize('problem', ['owner', 'mode', 'type'])
def test_fstat_trust_is_checked_on_open_descriptors(channel, monkeypatch, target, problem):
    channel.write_text(json.dumps(gate.FINALIZED))
    channel.chmod(0o600)
    real = os.fstat
    def changed(fd):
        result = real(fd)
        is_directory = gate.stat.S_ISDIR(result.st_mode)
        if is_directory == (target == 'directory'):
            values = list(result)
            if problem == 'owner':
                values[4] = os.getuid() + 1
            elif problem == 'mode':
                values[0] |= 0o040
            else:
                values[0] = gate.stat.S_IFIFO | 0o600
            return os.stat_result(values)
        return result
    monkeypatch.setattr(os, 'fstat', changed)
    assert not gate.manual_accounting_ready()


def test_marker_growth_after_stat_is_bounded(channel, monkeypatch):
    channel.write_text(json.dumps(gate.FINALIZED))
    channel.chmod(0o600)
    read = Mock(return_value=b'x' * (gate.LIMIT + 1))
    monkeypatch.setattr(os, 'read', read)
    assert not gate.manual_accounting_ready()
    assert read.call_args.args[1] == gate.LIMIT + 1


def test_symlink_directory_is_rejected_without_resolving(tmp_path, monkeypatch):
    owned = tmp_path / 'owned'
    owned.mkdir(mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(owned, target_is_directory=True)
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(alias / 'events'))
    assert not gate.manual_accounting_ready()
    with pytest.raises(OSError):
        gate.publish_manual_accounting_ready([inspection(alias)])
    assert not list(owned.iterdir())


def test_sticky_parent_other_owner_does_not_disqualify_private_leaf(tmp_path, monkeypatch):
    parent = tmp_path / 'sticky'
    parent.mkdir(mode=0o1777)
    parent.chmod(0o1777)
    leaf = parent / 'ipc'
    leaf.mkdir(mode=0o700)
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(leaf / 'events'))
    # Only the held leaf and marker may be fstat'ed; no parent UID assumption.
    real = os.fstat
    seen = []
    def observed(fd):
        result = real(fd)
        seen.append(result.st_ino)
        return result
    monkeypatch.setattr(os, 'fstat', observed)
    gate.publish_manual_accounting_ready([inspection(leaf)])
    assert gate.manual_accounting_ready()
    assert parent.stat().st_ino not in seen


def test_directory_rename_during_read_keeps_original_channel(channel, monkeypatch):
    channel.write_text(json.dumps(gate.FINALIZED))
    channel.chmod(0o600)
    real_read = gate._read
    moved = channel.parent.with_name(channel.parent.name + '-moved')
    def swapped(fd):
        channel.parent.rename(moved)
        channel.parent.mkdir(mode=0o700)
        (channel.parent / gate.MARKER).write_text('{}')
        return real_read(fd)
    monkeypatch.setattr(gate, '_read', swapped)
    assert gate.manual_accounting_ready()


@pytest.mark.parametrize('stage', ['write', 'file_sync', 'replace', 'directory_sync'])
def test_atomic_publication_errors_close_and_remove_sidecars(channel, monkeypatch, stage):
    real_sync = os.fsync
    real_replace = os.replace
    def sync(fd):
        directory = gate.stat.S_ISDIR(os.fstat(fd).st_mode)
        if (stage == 'directory_sync') == directory and stage in {'file_sync', 'directory_sync'}:
            raise OSError('controlled sync failure')
        return real_sync(fd)
    def replace(*args, **kwargs):
        if stage == 'replace':
            raise OSError('controlled replace failure')
        return real_replace(*args, **kwargs)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', replace)
    if stage == 'write':
        monkeypatch.setattr(os, 'fdopen', Mock(side_effect=OSError('controlled stream failure')))
    with pytest.raises(OSError, match='controlled'):
        gate.publish_manual_accounting_ready([inspection(channel.parent)])
    assert not list(channel.parent.glob(gate.MARKER + '.*'))
    if stage != 'directory_sync':
        assert not channel.exists()


def test_sidecar_is_private_and_final_symlink_is_replaced(channel, monkeypatch):
    target = channel.with_name('protected')
    target.write_text('unchanged')
    channel.symlink_to(target)
    real_replace = os.replace
    def replace(source, destination, **kwargs):
        info = os.stat(source, dir_fd=kwargs['src_dir_fd'], follow_symlinks=False)
        assert info.st_mode & 0o777 == 0o600
        assert kwargs['src_dir_fd'] == kwargs['dst_dir_fd']
        return real_replace(source, destination, **kwargs)
    monkeypatch.setattr(os, 'replace', replace)
    gate.publish_manual_accounting_ready([inspection(channel.parent)])
    assert target.read_text() == 'unchanged'
    assert not channel.is_symlink()
    assert gate.manual_accounting_ready()



def test_sidecar_collision_never_removes_an_unowned_existing_entry(channel, monkeypatch):
    monkeypatch.setattr(gate, 'uuid4', lambda: SimpleNamespace(hex='collision'))
    sidecar = channel.with_name(gate.MARKER + '.collision')
    sidecar.write_text('preserve')
    with pytest.raises(FileExistsError):
        gate.publish_manual_accounting_ready([inspection(channel.parent)])
    assert sidecar.read_text() == 'preserve'
    assert not channel.exists()


def test_default_system_temporary_channel_is_usable(monkeypatch):
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='inspection-gate-') as directory:
        monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(Path(directory) / 'events'))
        gate.publish_manual_accounting_ready([inspection(directory)])
        assert gate.manual_accounting_ready()
