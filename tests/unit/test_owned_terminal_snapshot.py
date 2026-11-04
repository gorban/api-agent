"""System snapshots can prove terminal ownership only for a reserved child."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.testing import processes


@pytest.fixture
def reserved_child():
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    try:
        assert processes.wait_owned(child, timeout=5) == 0
        assert child.returncode is None
        yield child
    finally:
        child.wait(timeout=5)


@pytest.mark.parametrize('case,expected', [
    ('leader', True), ('zombie-descendant', True), ('unrelated-live', True),
    ('missing', False), ('member-without-leader', False), ('wrong-parent', False), ('wrong-group', False),
    ('live-leader', False), ('live-descendant', False),
])
def test_terminal_proof_requires_owned_leader_and_every_member_zombie(monkeypatch, reserved_child, case, expected):
    pid = reserved_child.pid
    leader = (pid, os.getpid(), pid, 'Z+')
    rows = [leader]
    if case == 'zombie-descendant': rows.append((pid + 1, pid, pid, 'Z'))
    if case == 'unrelated-live': rows.append((pid + 1, 1, pid + 1, 'S'))
    if case == 'missing': rows = []
    if case == 'member-without-leader': rows = [(pid + 1, os.getpid(), pid, 'Z')]
    if case == 'wrong-parent': rows = [(pid, os.getpid() + 1, pid, 'Z')]
    if case == 'wrong-group': rows = [(pid, os.getpid(), pid + 1, 'Z')]
    if case == 'live-leader': rows = [(pid, os.getpid(), pid, 'S')]
    if case == 'live-descendant': rows.append((pid + 1, pid, pid, 'S'))
    calls = []
    def snapshot(**kwargs):
        calls.append(kwargs)
        return rows
    monkeypatch.setattr(processes, '_process_snapshot', snapshot)

    assert processes._owned_group_terminal(reserved_child, timeout=.7) is expected
    assert calls == [{'timeout': .7}]
    assert reserved_child.returncode is None


@pytest.mark.parametrize('reaped', [False, True])
def test_unreserved_handles_cannot_use_terminal_snapshot(monkeypatch, reserved_child, reaped):
    def forbidden(**kwargs):
        pytest.fail('unreserved handle inspected a reusable PID')
    monkeypatch.setattr(processes, '_process_snapshot', forbidden)
    handle = reserved_child if reaped else SimpleNamespace(pid=reserved_child.pid, returncode=None)
    if reaped: reserved_child.wait(timeout=5)
    assert processes._owned_group_terminal(handle) is False


def test_external_reap_rejects_before_snapshot(monkeypatch, reserved_child):
    _, status = os.waitpid(reserved_child.pid, 0)
    monkeypatch.setattr(processes, '_process_snapshot', lambda **kwargs: pytest.fail('lost reservation inspected'))
    try:
        with pytest.raises(RuntimeError, match='no longer reserved'):
            processes._owned_group_terminal(reserved_child)
    finally:
        reserved_child.returncode = os.waitstatus_to_exitcode(status)


@pytest.mark.parametrize('listing,message', [
    ('1 2 3 Z\n1 2 3 Z', 'duplicate'),
    ('-1 2 3 Z', 'invalid'), ('1 +2 3 Z', 'invalid'),
    ('١ 2 3 Z', 'invalid'), ('1 2 3', 'invalid'),
])
def test_snapshot_rejects_ambiguous_identity(monkeypatch, listing, message):
    monkeypatch.setattr(processes, 'subprocess', SimpleNamespace(**(vars(subprocess) | {
        'run': lambda *args, **kwargs: SimpleNamespace(stdout=listing)})))
    with pytest.raises(RuntimeError, match=message):
        processes._process_snapshot()


def test_snapshot_preserves_identity_and_ignores_blank_rows(monkeypatch):
    monkeypatch.setattr(processes, 'subprocess', SimpleNamespace(**(vars(subprocess) | {
        'run': lambda *args, **kwargs: SimpleNamespace(stdout='\n12 1 12 Z+\n\n13 12 12 Z\n')})))
    assert processes._process_snapshot() == ((12, 1, 12, 'Z+'), (13, 12, 12, 'Z'))


@pytest.mark.parametrize('platform,number,terminal,accepted,probed', [
    ('darwin', 3, True, True, True),
    ('darwin', 3, False, False, True),
    ('linux', 3, True, False, False),
    ('darwin', 1, True, False, False),
])
def test_missing_inspection_requires_exact_native_error_and_terminal_proof(monkeypatch, reserved_child, platform, number, terminal, accepted, probed):
    failure = OSError(number, 'controlled inspection refusal')
    calls = []
    def inspect(pid):
        assert pid == reserved_child.pid
        raise failure
    def proof(process):
        assert process is reserved_child
        calls.append(process)
        return terminal
    monkeypatch.setattr(processes, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(processes, 'observe_exit', lambda process: None)
    monkeypatch.setattr(processes, '_owned_group_terminal', proof)
    if accepted:
        assert processes.inspect_owned(reserved_child, inspect) is None
    else:
        with pytest.raises(OSError) as caught:
            processes.inspect_owned(reserved_child, inspect)
        assert caught.value is failure
    assert bool(calls) is probed
