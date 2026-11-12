"""A pack's declared Git hooks are installed without replacing user-owned hooks."""
import os
from pathlib import Path
import subprocess

import pytest

from api_agent.runtime import pack_hooks
from api_agent.runtime.pack_hooks import MARKER, assure_pack_hooks

DECLARED = (('pre-commit', 'git/hooks/pre-commit'),)


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'sample'
    (root / 'git/hooks').mkdir(parents=True)
    hook = root / 'git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\nprintf ran > "$(git rev-parse --show-toplevel)/proof.txt"\n')
    hook.chmod(0o755)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'a@example.test')
    git(root, 'config', 'user.name', 'A')
    git(root, 'config', 'commit.gpgsign', 'false')
    return root


def install(root, messages=None, declared=DECLARED):
    return assure_pack_hooks(root, declared, report=(messages if messages is not None else []).append)


def test_declared_hook_installs_once_and_runs_the_checkout_script_on_commit(checkout):
    messages = []
    assert install(checkout, messages) == {'pre-commit': 'installed'}
    slot = checkout / '.git/hooks/pre-commit'
    installed = slot.read_bytes()
    assert MARKER.encode() in installed and os.access(slot, os.X_OK)
    assert install(checkout, messages) == {'pre-commit': 'present'}
    assert slot.read_bytes() == installed
    assert len(messages) == 1
    (checkout / 'file.txt').write_text('x\n')
    git(checkout, 'add', 'file.txt')
    git(checkout, 'commit', '-q', '-m', 'first')
    assert (checkout / 'proof.txt').read_text() == 'ran'


def test_no_declared_hooks_do_not_touch_git(checkout, monkeypatch):
    monkeypatch.setattr(pack_hooks.shutil, 'which', lambda name: pytest.fail('git was consulted'))
    assert install(checkout, declared=()) == {}


def test_existing_user_hook_is_never_replaced(checkout):
    slot = checkout / '.git/hooks/pre-commit'
    slot.write_text('#!/bin/sh\nexit 0\n')
    messages = []
    assert install(checkout, messages) == {'pre-commit': 'foreign'}
    assert slot.read_text() == '#!/bin/sh\nexit 0\n'
    assert 'existing hook' in messages[0]


def test_symlinked_hook_slot_is_foreign(checkout):
    slot = checkout / '.git/hooks/pre-commit'
    slot.symlink_to(checkout / 'git/hooks/pre-commit')
    assert install(checkout) == {'pre-commit': 'foreign'}
    assert slot.is_symlink()


def test_stale_managed_wrapper_is_replaced_in_place(checkout):
    slot = checkout / '.git/hooks/pre-commit'
    slot.write_text(f'#!/bin/sh\n{MARKER}\nexec true\n')
    slot.chmod(0o755)
    messages = []
    assert install(checkout, messages) == {'pre-commit': 'updated'}
    assert 'git/hooks/pre-commit' in slot.read_text()
    assert os.access(slot, os.X_OK)
    assert 'updated' in messages[0]


def test_custom_hooks_path_elsewhere_is_left_to_the_user(checkout):
    git(checkout, 'config', 'core.hooksPath', 'custom-hooks')
    messages = []
    assert install(checkout, messages) == {'pre-commit': 'foreign'}
    assert not (checkout / '.git/hooks/pre-commit').exists()
    assert 'custom hooksPath' in messages[0]


@pytest.mark.parametrize('configured', ['git/hooks', './git/hooks'])
def test_hooks_path_already_naming_the_declared_directory_needs_no_wrapper(checkout, configured):
    git(checkout, 'config', 'core.hooksPath', configured)
    assert install(checkout) == {'pre-commit': 'present'}
    assert not (checkout / '.git/hooks/pre-commit').exists()


def test_hooks_path_naming_the_directory_does_not_cover_a_differently_named_script(checkout):
    git(checkout, 'config', 'core.hooksPath', 'git/hooks')
    (checkout / 'git/hooks/check').write_text('#!/bin/sh\n')
    assert install(checkout, declared=(('pre-commit', 'git/hooks/check'),)) == {'pre-commit': 'foreign'}


def test_not_a_checkout_root_is_not_applicable(tmp_path, checkout):
    inner = checkout / 'git'
    assert install(inner) == {'pre-commit': 'not-applicable'}
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert install(plain) == {'pre-commit': 'not-applicable'}


def test_missing_git_executable_is_not_applicable(checkout, monkeypatch):
    monkeypatch.setattr(pack_hooks.shutil, 'which', lambda name: None)
    assert install(checkout) == {'pre-commit': 'not-applicable'}


def test_linked_worktree_shares_hooks_and_is_never_written(checkout, tmp_path):
    git(checkout, 'add', '-A')
    git(checkout, 'commit', '-q', '-m', 'base')
    linked = tmp_path / 'linked'
    git(checkout, 'worktree', 'add', '-q', str(linked))
    messages = []
    assert install(linked, messages) == {'pre-commit': 'foreign'}
    assert not (checkout / '.git/hooks/pre-commit').exists()
    assert 'shared Git hook directory' in messages[0]
    assert install(checkout) == {'pre-commit': 'installed'}
    assert install(linked) == {'pre-commit': 'present'}


def test_unwritable_hook_directory_is_reported_not_raised(checkout, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError('read-only')
    monkeypatch.setattr(pack_hooks.tempfile, 'TemporaryDirectory', refuse)
    messages = []
    assert install(checkout, messages) == {'pre-commit': 'unavailable'}
    assert 'PermissionError' in messages[0]


def test_default_reporter_writes_to_standard_error(checkout, capsys):
    (checkout / '.git/hooks/pre-commit').write_text('#!/bin/sh\n')
    assert assure_pack_hooks(checkout, DECLARED) == {'pre-commit': 'foreign'}
    assert 'existing hook' in capsys.readouterr().err
