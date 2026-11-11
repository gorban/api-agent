"""Assure and upgrade exact managed hooks without replacing user-owned hooks."""
from pathlib import Path
from hashlib import sha256
import os
import shutil
import stat
import subprocess
import sys
import tempfile

from api_agent.config.process_settings import process_setting

MARKER = '# api-agent managed pre-commit dispatcher v1'
DISPATCHER_SHA256 = 'dcb111cedd4ac411b00d829385423130b7d80d9dd3198e56cb39534feb106054'
PREVIOUS_DISPATCHER_SHA256 = frozenset({
    'ee8a6311873df766ed3091e548c8b6aa7c12b4ab8109514fa99bebf5738623e8',
    '4a02d434823228190ccaaa3c18de0bb716ed53cf2792012e5a9879a741bc87d5',
    'cededf3fd6d38156a5a868987591f7f24154be3dc6c2144e81ce2186b39f9f2b',
})
MAX_DISPATCHER_BYTES = 1024 * 1024


def _git_environment():
    """Ignore inherited Git redirection while locating this checkout's hook slot."""
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def _git(root, *arguments):
    return subprocess.run(['git', *arguments], cwd=root, env=_git_environment(),
                          capture_output=True, timeout=process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS'),
                          check=True).stdout


def _trusted_checkout_template(root):
    """Read a bounded regular template without following either path component."""
    directory = os.open(root / 'tools', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open('pre_commit_dispatcher.py', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_DISPATCHER_BYTES:
                return None
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                script = stream.read(MAX_DISPATCHER_BYTES + 1)
            return script if sha256(script).hexdigest() == DISPATCHER_SHA256 else None
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _hook_snapshot(path):
    """Read bounded executable hook bytes from one regular, non-followed inode."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or not details.st_mode & stat.S_IXUSR
                or details.st_size > MAX_DISPATCHER_BYTES):
            return None
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            content = stream.read(MAX_DISPATCHER_BYTES + 1)
        return content, stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino
    finally:
        os.close(descriptor)


def _upgrade_hook(staged, hook, previous, report):
    """Move and verify the old inode, then publish only into a vacant slot."""
    recovery = Path(tempfile.mkdtemp(prefix='api-agent-hook-recovery-', dir=hook.parent.parent))
    saved = recovery / 'pre-commit'
    try:
        os.rename(hook, saved)
        if _hook_snapshot(saved) != previous:
            raise OSError('Managed hook changed during upgrade')
        expected = _hook_snapshot(staged)
        os.link(staged, hook)
        if _hook_snapshot(hook) != expected:
            raise OSError('Managed hook changed during publication')
    except BaseException:
        if saved.exists() or saved.is_symlink():
            try:
                os.link(saved, hook, follow_symlinks=False)
                saved.unlink()
            except OSError:
                report(f'Managed hook upgrade interrupted; preserved previous hook at {saved}.')
        raise
    else:
        saved.unlink()
    finally:
        if not (saved.exists() or saved.is_symlink()):
            recovery.rmdir()


def _current_managed(hook):
    """Whether a hook file is exactly the current managed dispatcher."""
    if not hook.is_file() or hook.is_symlink():
        return False
    header = b'#!/usr/bin/env python3\n' + MARKER.encode() + b'\n'
    snapshot = _hook_snapshot(hook)
    return (snapshot is not None and snapshot[0].startswith(header)
            and sha256(snapshot[0][len(header):]).hexdigest() == DISPATCHER_SHA256)


def assure_hook(root: Path, *, report=None) -> str:
    report = report or (lambda message: print(message, file=sys.stderr))
    if shutil.which('git') is None:
        return 'not-applicable'
    try:
        root = Path(root).resolve()
        lines = _git(root, 'rev-parse', '--show-toplevel', '--git-path', 'hooks').decode().splitlines()
        if len(lines) != 2 or Path(lines[0]).resolve() != root:
            return 'not-applicable'
        locations = _git(root, 'rev-parse', '--git-dir', '--git-common-dir').decode().splitlines()
        hooks = Path(lines[1])
        if not hooks.is_absolute():
            hooks = root / hooks
        if len(locations) != 2 or (root / locations[0]).resolve() != (root / locations[1]).resolve():
            # A linked worktree shares its main checkout's hooks; never write there.
            if _current_managed(hooks / 'pre-commit'):
                return 'present'
            report('Managed pre-commit hook not installed: shared Git hook directory; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        if not hooks.resolve().is_relative_to((root / locations[0]).resolve()):
            report('Managed pre-commit hook not installed: hook path leaves this Git directory; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        configured = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=root,
                                    env=_git_environment(), capture_output=True,
                                    timeout=process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS'))
        if configured.returncode == 0:
            report('Managed pre-commit hook not installed: custom hooksPath; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        hook = hooks / 'pre-commit'
        previous = None
        if hook.exists() or hook.is_symlink():
            header = b'#!/usr/bin/env python3\n' + MARKER.encode() + b'\n'
            if hook.is_file() and not hook.is_symlink():
                snapshot = _hook_snapshot(hook)
                if snapshot is not None and snapshot[0].startswith(header):
                    digest = sha256(snapshot[0][len(header):]).hexdigest()
                    if digest == DISPATCHER_SHA256:
                        return 'present'
                    if digest in PREVIOUS_DISPATCHER_SHA256:
                        previous = snapshot
            if previous is None:
                report('Managed pre-commit hook not installed: existing hook; integrate tools/pre_commit_dispatcher.py manually.')
                return 'foreign'
        # Prefer the proposed commit snapshot. Before first staging, the local
        # template is acceptable only when it matches this fixed trusted digest.
        try:
            script = _git(root, 'show', ':tools/pre_commit_dispatcher.py')
        except subprocess.CalledProcessError:
            try:
                entries = _git(root, 'ls-files', '--stage', '-z', '--', 'tools/pre_commit_dispatcher.py')
            except subprocess.CalledProcessError:
                entries = b'index-error'
            if entries:
                report('Managed pre-commit hook unavailable: staged dispatcher template cannot be read.')
                return 'unavailable'
            try:
                script = _trusted_checkout_template(root)
            except OSError:
                script = None
            if script is None:
                report('Managed pre-commit hook unavailable: trusted dispatcher template is missing.')
                return 'unavailable'
        if sha256(script).hexdigest() != DISPATCHER_SHA256:
            report('Managed pre-commit hook unavailable: staged dispatcher template is invalid.')
            return 'unavailable'
        hooks.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.api-agent-pre-commit-', dir=hooks) as temporary_dir:
            staged = Path(temporary_dir) / 'pre-commit'
            with staged.open('xb') as temporary:
                temporary.write(b'#!/usr/bin/env python3\n' + MARKER.encode() + b'\n' + script)
                temporary.flush()
                os.fsync(temporary.fileno())
            staged.chmod(0o755)
            # Hard-link publication fails if another owner installed a hook
            # concurrently; never replace or unlink that hook on failure.
            if previous is None:
                os.link(staged, hook)
            else:
                _upgrade_hook(staged, hook, previous, report)
        if previous is not None:
            report('Managed pre-commit dispatcher updated; checks run only when committing.')
            return 'updated'
        report('Managed pre-commit dispatcher installed; checks run only when committing.')
        return 'installed'
    except subprocess.CalledProcessError:
        return 'not-applicable'
    except (FileExistsError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        report(f'Managed pre-commit assurance unavailable: {type(error).__name__}; integrate tools/pre_commit_dispatcher.py manually.')
        return 'unavailable'
