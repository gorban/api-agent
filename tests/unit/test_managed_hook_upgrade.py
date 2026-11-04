"""Exact managed-hook upgrades preserve edits and concurrent installations."""
from hashlib import sha256
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from api_agent.runtime import hook_assurance as hooks


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / 'sample'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'tools').mkdir()
    template = Path(__file__).resolve().parents[2] / 'tools/pre_commit_dispatcher.py'
    shutil.copyfile(template, root / 'tools/pre_commit_dispatcher.py')
    subprocess.run(['git', '-C', str(root), 'add', 'tools'], check=True)
    body = b'"""Synthetic older managed dispatcher."""\n'
    monkeypatch.setattr(hooks, 'PREVIOUS_DISPATCHER_SHA256', frozenset({sha256(body).hexdigest()}))
    path = root / '.git/hooks/pre-commit'
    previous = b'#!/usr/bin/env python3\n' + hooks.MARKER.encode() + b'\n' + body
    path.write_bytes(previous)
    path.chmod(0o755)
    return root, path, previous


def test_known_managed_hook_updates_once_without_running_checks(checkout):
    root, path, previous = checkout
    messages = []

    assert hooks.assure_hook(root, report=messages.append) == 'updated'
    current = path.read_bytes()
    assert current != previous
    assert current.endswith((root / 'tools/pre_commit_dispatcher.py').read_bytes())
    assert os.access(path, os.X_OK)
    assert hooks.assure_hook(root, report=messages.append) == 'present'
    assert len(messages) == 1 and 'updated' in messages[0]
    assert not list((root / '.git').glob('api-agent-hook-recovery-*'))
    assert not list(path.parent.glob('.api-agent-pre-commit-*'))


@pytest.mark.parametrize('mutation', ['edited', 'nonexecutable', 'oversized'])
def test_modified_managed_hook_is_preserved(checkout, mutation):
    root, path, previous = checkout
    if mutation == 'edited':
        path.write_bytes(previous + b'# user edit\n')
    elif mutation == 'oversized':
        path.write_bytes(b'x' * (hooks.MAX_DISPATCHER_BYTES + 1))
    else:
        path.chmod(0o644)
    expected = path.read_bytes(), path.stat().st_mode

    assert hooks.assure_hook(root, report=lambda _: None) == 'foreign'
    assert (path.read_bytes(), path.stat().st_mode) == expected


def test_invalid_new_template_cannot_displace_old_hook(checkout):
    root, path, previous = checkout
    (root / 'tools/pre_commit_dispatcher.py').write_text('untrusted\n')
    subprocess.run(['git', '-C', str(root), 'add', 'tools'], check=True)

    assert hooks.assure_hook(root, report=lambda _: None) == 'unavailable'
    assert path.read_bytes() == previous
    assert not list((root / '.git').glob('api-agent-hook-recovery-*'))


@pytest.mark.parametrize('replacement', ['edited', 'replaced', 'symlink', 'nonexecutable'])
def test_change_during_move_is_restored_without_new_publication(checkout, monkeypatch, replacement):
    root, path, previous = checkout
    original = hooks.os.rename
    foreign = root / 'foreign-hook'
    foreign.write_bytes(b'foreign target\n')
    def race(source, target):
        if source == path:
            if replacement == 'edited':
                path.write_bytes(previous + b'# concurrent edit\n')
            elif replacement == 'replaced':
                original(path, root / 'displaced-managed-hook')
                path.write_bytes(previous)
                path.chmod(0o755)
            elif replacement == 'symlink':
                path.unlink()
                path.symlink_to(foreign)
            else:
                path.chmod(0o644)
        return original(source, target)
    monkeypatch.setattr(hooks.os, 'rename', race)

    assert hooks.assure_hook(root, report=lambda _: None) == 'unavailable'
    if replacement == 'symlink':
        assert path.is_symlink() and path.read_bytes() == b'foreign target\n'
    else:
        assert path.read_bytes() == previous + (b'# concurrent edit\n' if replacement == 'edited' else b'')
        assert bool(path.stat().st_mode & 0o100) == (replacement != 'nonexecutable')
    assert not list((root / '.git').glob('api-agent-hook-recovery-*'))


def test_concurrent_installation_wins_and_original_is_retained(checkout, monkeypatch):
    root, path, previous = checkout
    original = hooks.os.link
    def race(source, target, **kwargs):
        if Path(source).parent.name.startswith('.api-agent-pre-commit-'):
            path.write_bytes(b'#!/bin/sh\nexit 7\n')
        return original(source, target, **kwargs)
    monkeypatch.setattr(hooks.os, 'link', race)
    messages = []

    assert hooks.assure_hook(root, report=messages.append) == 'unavailable'
    assert path.read_bytes() == b'#!/bin/sh\nexit 7\n'
    backups = list((root / '.git').glob('api-agent-hook-recovery-*/pre-commit'))
    assert len(backups) == 1 and backups[0].read_bytes() == previous
    assert any(str(backups[0]) in message for message in messages)


@pytest.mark.parametrize('stage', ['move', 'publish'])
def test_failed_upgrade_restores_original_and_cleans_empty_recovery(checkout, monkeypatch, stage):
    root, path, previous = checkout
    if stage == 'move':
        monkeypatch.setattr(hooks.os, 'rename', lambda *args: (_ for _ in ()).throw(PermissionError('controlled')))
    else:
        original = hooks.os.link
        def fail(source, target, **kwargs):
            if Path(source).parent.name.startswith('.api-agent-pre-commit-'):
                raise PermissionError('controlled')
            return original(source, target, **kwargs)
        monkeypatch.setattr(hooks.os, 'link', fail)

    assert hooks.assure_hook(root, report=lambda _: None) == 'unavailable'
    assert path.read_bytes() == previous
    assert not list((root / '.git').glob('api-agent-hook-recovery-*'))


def test_concurrent_replacement_after_publication_retains_old_backup(checkout, monkeypatch):
    root, path, previous = checkout
    original = hooks.os.link
    def race(source, target, **kwargs):
        result = original(source, target, **kwargs)
        if Path(source).parent.name.startswith('.api-agent-pre-commit-'):
            path.unlink()
            path.write_bytes(b'#!/bin/sh\nexit 9\n')
        return result
    monkeypatch.setattr(hooks.os, 'link', race)
    messages = []

    assert hooks.assure_hook(root, report=messages.append) == 'unavailable'
    assert path.read_bytes() == b'#!/bin/sh\nexit 9\n'
    backups = list((root / '.git').glob('api-agent-hook-recovery-*/pre-commit'))
    assert len(backups) == 1 and backups[0].read_bytes() == previous
    assert any(str(backups[0]) in message for message in messages)
