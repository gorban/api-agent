"""Managed hook assurance and index snapshot regression checks."""
from pathlib import Path
import os
import shutil
import subprocess
from types import SimpleNamespace

from api_agent.runtime.hook_assurance import assure_hook, MARKER
from api_agent.runtime import hook_assurance


ROOT = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path):
    root = tmp_path / 'sample'
    root.mkdir(parents=True)
    git(root, 'init', '-q')
    (root / 'tools').mkdir()
    shutil.copy2(ROOT / 'tools/pre_commit_dispatcher.py', root / 'tools/pre_commit_dispatcher.py')
    shutil.copy2(ROOT / 'tools/checks.py', root / 'tools/checks.py')
    (root / 'tools/workflow_views.py').write_text('')
    package = root / 'src/api_agent/workflows'
    package.mkdir(parents=True)
    (root / 'src/api_agent/__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    (package / 'viewer_build.py').write_text('def check_views(root):\n    assert (root / "proof.txt").read_text() == "ok\\n"\n\ndef check_staged_workflow_matches_worktree(snapshot, worktree):\n    pass\n')
    (root / 'proof.txt').write_text('ok\n')
    git(root, 'add', '.')
    return root


def test_missing_hook_installs_once_without_running_checks(tmp_path):
    root = repository(tmp_path)
    messages = []
    assert assure_hook(root, report=messages.append) == 'installed'
    hook = root / '.git/hooks/pre-commit'
    before = hook.read_bytes()
    assert MARKER.encode() in before
    assert assure_hook(root, report=messages.append) == 'present'
    assert len(messages) == 1
    assert hook.read_bytes() == before


def test_foreign_and_custom_hooks_remain_owned_by_user(tmp_path):
    root = repository(tmp_path)
    hook = root / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\nexit 0\n')
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert hook.read_text() == '#!/bin/sh\nexit 0\n'
    hook.unlink()
    git(root, 'config', 'core.hooksPath', 'custom-hooks')
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert not hook.exists()


def test_marker_only_hook_cannot_claim_managed_assurance(tmp_path):
    root = repository(tmp_path)
    hook = root / '.git/hooks/pre-commit'
    fake = '#!/usr/bin/env python3\n' + MARKER + '\npass\n'
    hook.write_text(fake)
    hook.chmod(0o755)
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert hook.read_text() == fake


def test_modified_or_nonexecutable_managed_hook_is_foreign(tmp_path):
    root = repository(tmp_path)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    hook = root / '.git/hooks/pre-commit'
    hook.chmod(0o644)
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    hook.chmod(0o755)
    hook.write_bytes(hook.read_bytes() + b'\n# modified\n')
    assert assure_hook(root, report=lambda _: None) == 'foreign'


def test_no_git_is_not_applicable(tmp_path):
    assert assure_hook(tmp_path, report=lambda _: None) == 'not-applicable'


def test_missing_git_skips_assurance_without_warning_or_git_call(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_assurance, 'shutil', SimpleNamespace(which=lambda name: None))
    def unexpected_git(*args):
        raise AssertionError('git must not be invoked')
    monkeypatch.setattr(hook_assurance, '_git', unexpected_git)
    messages = []
    assert assure_hook(tmp_path, report=messages.append) == 'not-applicable'
    assert messages == []


def test_nested_directory_does_not_install_repo_hook(tmp_path):
    root = repository(tmp_path)
    nested = root / 'nested'
    nested.mkdir()
    assert assure_hook(nested, report=lambda _: None) == 'not-applicable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_shared_symlink_hook_directory_is_preserved(tmp_path):
    root = repository(tmp_path)
    hooks = root / '.git/hooks'
    shutil.rmtree(hooks)
    shared = tmp_path / 'shared-hooks'
    shared.mkdir()
    hooks.symlink_to(shared, target_is_directory=True)
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert not (shared / 'pre-commit').exists()


def test_linked_worktree_does_not_install_into_shared_hook_directory(tmp_path):
    root = repository(tmp_path)
    git(root, 'config', 'user.name', 'Viewer Test')
    git(root, 'config', 'user.email', 'viewer@example.invalid')
    git(root, 'commit', '-q', '-m', 'baseline')
    linked = tmp_path / 'linked'
    git(root, 'worktree', 'add', '-q', '-b', 'linked', str(linked))
    assert assure_hook(linked, report=lambda _: None) == 'foreign'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_symlinked_hook_file_is_foreign(tmp_path):
    root = repository(tmp_path)
    foreign = tmp_path / 'foreign-hook'
    foreign.write_text('#!/bin/sh\nexit 0\n')
    hook = root / '.git/hooks/pre-commit'
    hook.symlink_to(foreign)
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert foreign.read_text() == '#!/bin/sh\nexit 0\n'


def test_unwritable_hook_slot_leaves_no_partial_hook(tmp_path, monkeypatch):
    root = repository(tmp_path)

    def fail_create(**kwargs):
        raise PermissionError('hook directory is not writable')

    monkeypatch.setattr(hook_assurance.tempfile, 'TemporaryDirectory', fail_create)
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_absolute_git_hook_path_is_confined_to_same_repository(tmp_path, monkeypatch):
    root = repository(tmp_path)
    original = hook_assurance._git

    def absolute(root_arg, *arguments):
        result = original(root_arg, *arguments)
        if arguments == ('rev-parse', '--show-toplevel', '--git-path', 'hooks'):
            lines = result.decode().splitlines()
            return f'{lines[0]}\n{root / ".git/hooks"}\n'.encode()
        return result

    monkeypatch.setattr(hook_assurance, '_git', absolute)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    assert (root / '.git/hooks/pre-commit').is_file()


def test_concurrent_foreign_hook_wins_without_replacement_or_temp_leak(tmp_path, monkeypatch):
    root = repository(tmp_path)
    hook = root / '.git/hooks/pre-commit'
    def concurrent_install(source, target):
        hook.write_text('#!/bin/sh\nexit 7\n')
        raise FileExistsError('hook appeared concurrently')

    monkeypatch.setattr(hook_assurance.os, 'link', concurrent_install)
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert hook.read_text() == '#!/bin/sh\nexit 7\n'
    assert not list(hook.parent.glob('.api-agent-pre-commit-*'))


def test_exact_checkout_template_installs_when_staged_template_is_missing(tmp_path):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    messages = []
    assert assure_hook(root, report=messages.append) == 'installed'
    assert (root / '.git/hooks/pre-commit').is_file()
    assert len(messages) == 1
    assert assure_hook(root, report=messages.append) == 'present'
    assert len(messages) == 1


def test_modified_checkout_or_staged_template_cannot_be_installed(tmp_path):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    template = root / 'tools/pre_commit_dispatcher.py'
    template.write_text('raise SystemExit(0)\n')
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_invalid_staged_template_never_falls_back_to_valid_checkout(tmp_path):
    root = repository(tmp_path)
    template = root / 'tools/pre_commit_dispatcher.py'
    original = template.read_bytes()
    template.write_text('raise SystemExit(0)\n')
    git(root, 'add', 'tools/pre_commit_dispatcher.py')
    template.write_bytes(original)
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_unreadable_staged_blob_never_falls_back_to_valid_checkout(tmp_path):
    root = repository(tmp_path)
    staged = git(root, 'ls-files', '--stage', '--', 'tools/pre_commit_dispatcher.py')
    blob = staged.split()[1]
    object_path = root / '.git/objects' / blob[:2] / blob[2:]
    object_bytes = object_path.read_bytes()
    object_path.unlink()

    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()

    object_path.write_bytes(object_bytes)
    assert assure_hook(root, report=lambda _: None) == 'installed'


def test_unreadable_index_cannot_authorize_checkout_fallback(tmp_path, monkeypatch):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    original = hook_assurance._git

    def unreadable_index(selected_root, *arguments):
        if arguments[:2] == ('ls-files', '--stage'):
            raise subprocess.CalledProcessError(1, ['git', *arguments])
        return original(selected_root, *arguments)

    monkeypatch.setattr(hook_assurance, '_git', unreadable_index)
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_missing_and_special_checkout_templates_are_unavailable(tmp_path):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    template = root / 'tools/pre_commit_dispatcher.py'
    template.unlink()
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    template.symlink_to(ROOT / 'tools/pre_commit_dispatcher.py')
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    template.unlink()
    os.mkfifo(template)
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_oversized_checkout_template_is_unavailable(tmp_path):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    (root / 'tools/pre_commit_dispatcher.py').write_bytes(b'x' * (hook_assurance.MAX_DISPATCHER_BYTES + 1))
    assert assure_hook(root, report=lambda _: None) == 'unavailable'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_checkout_template_uses_opened_bytes_if_path_is_replaced(tmp_path, monkeypatch):
    root = repository(tmp_path)
    git(root, 'rm', '--cached', 'tools/pre_commit_dispatcher.py')
    template = root / 'tools/pre_commit_dispatcher.py'
    trusted = template.read_bytes()
    original_open = hook_assurance.os.open

    def replace_after_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == 'pre_commit_dispatcher.py':
            template.unlink()
            template.write_text('raise SystemExit(0)\n')
        return descriptor

    monkeypatch.setattr(hook_assurance.os, 'open', replace_after_open)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    assert (root / '.git/hooks/pre-commit').read_bytes().endswith(trusted)


def test_explicit_local_hook_path_is_not_modified(tmp_path):
    root = repository(tmp_path)
    git(root, 'config', 'core.hooksPath', '.git/hooks')
    assert assure_hook(root, report=lambda _: None) == 'foreign'
    assert not (root / '.git/hooks/pre-commit').exists()


def test_dispatcher_checks_index_not_unstaged_runner(tmp_path):
    root = repository(tmp_path)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    hook = root / '.git/hooks/pre-commit'
    assert subprocess.run([str(hook)], cwd=root, check=False).returncode == 0
    (root / 'tools/checks.py').write_text('raise SystemExit(0)\n')
    (root / 'src/api_agent/workflows/viewer_build.py').write_text('raise SystemExit(0)\n')
    (root / 'proof.txt').write_text('wrong\n')
    assert subprocess.run([str(hook)], cwd=root, check=False).returncode == 0
    git(root, 'add', 'proof.txt')
    assert subprocess.run([str(hook)], cwd=root, check=False).returncode != 0


def test_inherited_git_redirection_cannot_relocate_assurance(tmp_path, monkeypatch):
    root = repository(tmp_path)
    other = repository(tmp_path / 'other-parent')
    monkeypatch.setenv('GIT_DIR', str(other / '.git'))
    assert assure_hook(root, report=lambda _: None) == 'installed'
    assert (root / '.git/hooks/pre-commit').exists()
    assert not (other / '.git/hooks/pre-commit').exists()


def test_dispatcher_rejects_unowned_temporary_index(tmp_path):
    root = repository(tmp_path)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    outside = tmp_path / 'outside-index'
    outside.write_bytes((root / '.git/index').read_bytes())
    environment = os.environ.copy()
    environment['GIT_INDEX_FILE'] = str(outside)
    hook = root / '.git/hooks/pre-commit'
    assert subprocess.run([str(hook)], cwd=root, env=environment, check=False,
                          capture_output=True).returncode != 0


def test_dispatcher_uses_verified_git_temporary_index(tmp_path):
    root = repository(tmp_path)
    assert assure_hook(root, report=lambda _: None) == 'installed'
    alternate = root / '.git/index.viewer-temporary'
    alternate.write_bytes((root / '.git/index').read_bytes())
    environment = os.environ.copy()
    environment['GIT_INDEX_FILE'] = str(alternate)
    (root / 'proof.txt').write_text('wrong\n')
    subprocess.run(['git', 'add', 'proof.txt'], cwd=root, env=environment, check=True)
    hook = root / '.git/hooks/pre-commit'
    assert subprocess.run([str(hook)], cwd=root, env=environment, check=False,
                          capture_output=True).returncode != 0
    assert subprocess.run([str(hook)], cwd=root, check=False, capture_output=True).returncode == 0
