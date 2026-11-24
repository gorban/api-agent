"""macOS lease identities survive kernel boot-clock adjustment but not reboot or PID reuse."""
import json
import os
from pathlib import Path
import subprocess

import pytest

import api_agent.runtime.prerequisites as p
from test_prerequisite_disposal import removable_engine
from test_prerequisites import setup  # noqa: F401

BOOT = 'B093631B-8658-45C5-8F92-007AE5C7CC08'
STARTED = 'Fri Nov 20 09:39:36 2026'


class MacHost:
    """Synthetic Darwin host whose derived ``kern.boottime`` moves between reads."""

    def __init__(self, monkeypatch, *, seconds=1794801641, microseconds=752487):
        self.seconds = seconds
        self.microseconds = microseconds
        self.boot = BOOT
        self.started = {}
        original_run = subprocess.run
        original_is_file = Path.is_file

        def run(command, **options):
            if command[:2] == ['/usr/sbin/sysctl', '-n']:
                if command[2] == 'kern.bootsessionuuid':
                    return subprocess.CompletedProcess(command, 0, self.boot + '\n', '')
                assert command[2] == 'kern.boottime'
                return subprocess.CompletedProcess(command, 0,
                    f'{{ sec = {self.seconds}, usec = {self.microseconds} }} Sun Nov 15 22:00:41 2026\n', '')
            if command[0] == '/bin/ps':
                pid = int(command[2])
                if pid not in self.started:
                    return subprocess.CompletedProcess(command, 1, '', '')
                return subprocess.CompletedProcess(command, 0, self.started[pid] + ' S\n', '')
            return original_run(command, **options)

        monkeypatch.setattr(subprocess, 'run', run)
        monkeypatch.setattr(Path, 'is_file', lambda path: False if str(path) == '/proc/sys/kernel/random/boot_id'
                            else original_is_file(path))
        monkeypatch.setattr(p.sys, 'platform', 'darwin')
        self.started[os.getpid()] = STARTED


def legacy(seconds, microseconds, started=STARTED):
    return f'{{ sec = {seconds}, usec = {microseconds} }} Sun Nov 15 22:00:41 2026:{started}'


def test_a_drifting_macos_boot_clock_does_not_let_a_second_consumer_dispose_of_a_live_lease(setup, monkeypatch):
    create, engine, _, scope, _ = setup
    host = MacHost(monkeypatch, microseconds=674862)
    live = create(dispose_on_release=True)
    live.identity = p.process_identity
    live.runner = removable_engine(engine)
    lease = live.acquire()
    path = live.directory / f'{scope.id}.json'
    journal = json.loads(path.read_text())
    journal[lease.invocation_id]['pid'] = 4242
    path.write_text(json.dumps(journal))
    host.started[4242] = STARTED
    host.microseconds = 752487
    second = create(dispose_on_release=True)
    second.identity = p.process_identity
    second.runner = live.runner
    second.acquire()

    second.release()

    assert set(engine.records) == set(lease.container_ids)
    assert not json.loads(path.read_text())[lease.invocation_id].get('cleanup_only', False)
    live.release()
    assert not engine.records


@pytest.mark.parametrize('recorded, active', [
    (f'darwin:{BOOT}:{STARTED}', True),
    (legacy(1794801641, 674862), True),
    (legacy(1794801641, 0), True),
    (f'darwin:{"0" * 8}-8658-45C5-8F92-007AE5C7CC08:{STARTED}', False),
    (f'darwin:{BOOT}:Fri Nov 20 09:39:37 2026', False),
    (legacy(1794801640, 674862), False),
    (legacy(1794801642, 674862), False),
    (legacy(1794801641, 674862, 'Fri Nov 20 09:39:37 2026'), False),
    ('{ sec = 1794801641, usec = 674862 }:' + STARTED, False),
    ('synthetic-boot:1', False),
])
def test_recorded_macos_identities_are_live_only_for_the_same_boot_and_process_birth(setup, monkeypatch, recorded, active):
    _, _, _, scope, tmp_path = setup
    MacHost(monkeypatch)
    directory = tmp_path / 'ownership'
    directory.mkdir()
    identifier = 'a' * 64
    (directory / f'{scope.id}.json').write_text(json.dumps(
        {'recorded': {'pid': os.getpid(), 'identity': recorded, 'ids': [identifier]}}))

    assert p.active_leased_ids(directory, scope) == ({identifier} if active else set())


def test_a_reused_macos_pid_after_reboot_is_dead(setup, monkeypatch):
    _, _, _, scope, tmp_path = setup
    host = MacHost(monkeypatch)
    recorded = p.process_identity(os.getpid())
    host.boot = '6F58D16A-E2F3-3ECC-85D8-6504CFC1339D'
    directory = tmp_path / 'ownership'
    directory.mkdir()
    (directory / f'{scope.id}.json').write_text(json.dumps(
        {'recorded': {'pid': os.getpid(), 'identity': recorded, 'ids': ['a' * 64]}}))

    assert p.active_leased_ids(directory, scope) == set()


def test_macos_identity_is_the_boot_session_and_process_birth(monkeypatch):
    host = MacHost(monkeypatch)
    first = p.process_identity(os.getpid())
    assert first == f'darwin:{BOOT}:{STARTED}'
    assert p.process_identity(os.getpid()) == first
    assert p.process_identity(123456789) is None
    host.boot = ''
    with pytest.raises(p.PrerequisiteError, match='boot identity'):
        p.process_identity(os.getpid())


@pytest.mark.parametrize('failure', ['oserror', 'timeout', 'exit', 'malformed'])
def test_an_unreadable_boot_time_refuses_to_judge_a_legacy_macos_lease(monkeypatch, failure):
    host = MacHost(monkeypatch)
    fake = subprocess.run

    def run(command, **options):
        if command[2:] == ['kern.boottime']:
            if failure == 'oserror': raise OSError('private')
            if failure == 'timeout': raise subprocess.TimeoutExpired(command, 1)
            return subprocess.CompletedProcess(command, 1 if failure == 'exit' else 0,
                                               '' if failure == 'exit' else 'unexpected', '')
        return fake(command, **options)

    monkeypatch.setattr(subprocess, 'run', run)
    current = p.process_identity(os.getpid())
    with pytest.raises(p.PrerequisiteError, match='boot identity'):
        p.same_process(current, legacy(host.seconds, 1))


@pytest.mark.parametrize('current, recorded, same', [
    (None, f'darwin:{BOOT}:{STARTED}', False),
    ('boot:start', 'boot:start', True),
    ('boot:start', 'boot:other', False),
    ('boot:start', legacy(1, 2, 'start'), False),
    (f'darwin:{BOOT}:{STARTED}', 'boot:start', False),
])
def test_identity_comparison_is_exact_outside_the_macos_formats(monkeypatch, current, recorded, same):
    MacHost(monkeypatch)
    assert p.same_process(current, recorded) is same
