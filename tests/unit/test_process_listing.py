"""Every system process listing goes through one helper in api_agent.runtime.processes."""
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.auth import owned_process
from api_agent.runtime import processes, prerequisites


def report(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.fixture
def ps(monkeypatch):
    calls, outcomes = [], []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(processes.subprocess, 'run', run)
    return calls, outcomes


def test_all_processes_are_listed_with_one_fixed_invocation(ps):
    calls, outcomes = ps
    outcomes.append(report(' 12 1 12 S+\n\n 13 12 12 Z\n'))
    assert processes.list_processes('pid', 'ppid', 'pgid', 'state', timeout=3) == (
        (12, 1, 12, 'S+'), (13, 12, 12, 'Z'))
    assert calls == [(['/bin/ps', '-A', '-o', 'pid=', '-o', 'ppid=', '-o', 'pgid=', '-o', 'state='],
                      {'capture_output': True, 'text': True, 'check': False, 'timeout': 3,
                       'env': {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}})]


def test_one_process_is_listed_with_its_multiword_start_time(ps):
    calls, outcomes = ps
    outcomes.append(report('Mon Oct  5 12:00:00 2026 S\n'))
    assert processes.list_processes('started', 'state', pid=123) == (('Mon Oct 5 12:00:00 2026', 'S'),)
    assert calls[0][0] == ['/bin/ps', '-p', '123', '-o', 'lstart=', '-o', 'state=']


def test_a_listing_without_rows_is_empty(ps):
    ps[1].append(report(''))
    assert processes.list_processes('pid') == ()


def test_a_process_that_no_longer_exists_lists_no_rows(ps):
    ps[1].append(report('', returncode=1))
    assert processes.list_processes('pid', pid=123) == ()


@pytest.mark.parametrize('pid,outcome', [
    (123, report('', returncode=1, stderr='permission denied')),
    (123, report('', returncode=0)),
    (123, report('1 S', returncode=2)),
    (None, report('', returncode=1)),
    (None, report('1 S', returncode=2)),
    (123, OSError('missing ps')),
    (None, subprocess.TimeoutExpired('ps', 1)),
])
def test_any_other_ps_failure_is_an_error(ps, pid, outcome):
    ps[1].append(outcome)
    with pytest.raises(RuntimeError, match='ps failed'):
        processes.list_processes('pid', 'state', pid=pid)


@pytest.mark.parametrize('listing', ['1', '1 S extra', '-1 S', '+1 S', 'x S', '\u0661 S'])
def test_unreadable_output_is_an_error(ps, listing):
    ps[1].append(report(listing))
    with pytest.raises(RuntimeError, match='invalid ps output'):
        processes.list_processes('pid', 'state')


def test_the_real_system_listing_describes_this_process():
    (row,) = processes.list_processes('pid', 'ppid', 'pgid', 'state', pid=os.getpid())
    assert row[:3] == (os.getpid(), os.getppid(), os.getpgrp())
    started, state = processes.list_processes('started', 'state', pid=os.getpid())[0]
    assert len(started.split()) == 5 and not state.startswith('Z')


def test_the_group_probe_of_an_owned_helper_uses_the_shared_listing(monkeypatch):
    monkeypatch.setattr(owned_process.os, 'killpg', Mock(side_effect=PermissionError('refused')))
    listing = Mock(return_value=((123, 123, 'Z'), (124, 123, 'Z+'), (999, 1, 'S')))
    monkeypatch.setattr(processes, 'list_processes', listing)

    owned_process.kill_owned_group(123)

    listing.assert_called_once_with('pid', 'pgid', 'state', timeout=5)


@pytest.mark.parametrize('members', [
    ((123, 123, 'S'),), ((124, 123, 'Z'),), ((123, 123, 'Z'), (124, 123, 'S')), (),
])
def test_a_refused_signal_is_tolerated_only_for_a_zombie_only_group_with_its_leader(monkeypatch, members):
    monkeypatch.setattr(owned_process.os, 'killpg', Mock(side_effect=PermissionError('refused')))
    monkeypatch.setattr(processes, 'list_processes', Mock(return_value=members))
    with pytest.raises(PermissionError):
        owned_process.kill_owned_group(123)


def test_an_absent_group_needs_no_listing(monkeypatch):
    monkeypatch.setattr(owned_process.os, 'killpg', Mock(side_effect=ProcessLookupError()))
    monkeypatch.setattr(processes, 'list_processes', Mock(side_effect=AssertionError('listed')))
    owned_process.kill_owned_group(123)


def test_lease_process_identity_failure_from_the_shared_listing_is_a_prerequisite_error(monkeypatch):
    monkeypatch.setattr(prerequisites.Path, 'is_file', lambda self: False)
    monkeypatch.setattr(prerequisites.sys, 'platform', 'darwin')
    monkeypatch.setattr(prerequisites.subprocess, 'run', lambda *a, **k: report('{ sec = 1, usec = 2 }'))
    monkeypatch.setattr(prerequisites, 'list_processes', Mock(side_effect=RuntimeError('ps failed')))
    with pytest.raises(prerequisites.PrerequisiteError, match='lease process identity'):
        prerequisites.process_identity(123)


def test_no_module_other_than_the_shared_helper_runs_ps():
    from pathlib import Path
    source = Path(prerequisites.__file__).parents[1]
    offenders = sorted(str(path.relative_to(source)) for path in source.rglob('*.py')
                       if '/bin/ps' in path.read_text() and path.name != 'processes.py')
    assert offenders == []
