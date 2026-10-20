"""Assure a missing local hook without replacing user-owned Git hooks."""
from pathlib import Path
from hashlib import sha256
import os
import stat
import subprocess
import sys
import tempfile

MARKER = '# api-agent managed pre-commit dispatcher v1'
DISPATCHER_SHA256 = '4a02d434823228190ccaaa3c18de0bb716ed53cf2792012e5a9879a741bc87d5'
MAX_DISPATCHER_BYTES = 1024 * 1024


def _git_environment():
    """Ignore inherited Git redirection while locating this checkout's hook slot."""
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def _git(root, *arguments):
    return subprocess.run(['git', *arguments], cwd=root, env=_git_environment(),
                          capture_output=True, timeout=5, check=True).stdout


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


def assure_hook(root: Path, *, report=None) -> str:
    report = report or (lambda message: print(message, file=sys.stderr))
    try:
        root = Path(root).resolve()
        lines = _git(root, 'rev-parse', '--show-toplevel', '--git-path', 'hooks').decode().splitlines()
        if len(lines) != 2 or Path(lines[0]).resolve() != root:
            return 'not-applicable'
        locations = _git(root, 'rev-parse', '--git-dir', '--git-common-dir').decode().splitlines()
        if len(locations) != 2 or (root / locations[0]).resolve() != (root / locations[1]).resolve():
            report('Managed pre-commit hook not installed: shared Git hook directory; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        hooks = Path(lines[1])
        if not hooks.is_absolute():
            hooks = root / hooks
        if not hooks.resolve().is_relative_to((root / locations[0]).resolve()):
            report('Managed pre-commit hook not installed: hook path leaves this Git directory; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        configured = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=root,
                                    env=_git_environment(), capture_output=True, timeout=5)
        if configured.returncode == 0:
            report('Managed pre-commit hook not installed: custom hooksPath; integrate tools/pre_commit_dispatcher.py manually.')
            return 'foreign'
        hook = hooks / 'pre-commit'
        if hook.exists() or hook.is_symlink():
            header = b'#!/usr/bin/env python3\n' + MARKER.encode() + b'\n'
            if hook.is_file() and not hook.is_symlink():
                content = hook.read_bytes()
                mode = hook.stat().st_mode
                if (content.startswith(header) and sha256(content[len(header):]).hexdigest() == DISPATCHER_SHA256
                        and stat.S_ISREG(mode) and mode & stat.S_IXUSR):
                    return 'present'
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
            os.link(staged, hook)
        report('Managed pre-commit dispatcher installed; checks run only when committing.')
        return 'installed'
    except subprocess.CalledProcessError:
        return 'not-applicable'
    except (FileExistsError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        report(f'Managed pre-commit assurance unavailable: {type(error).__name__}; integrate tools/pre_commit_dispatcher.py manually.')
        return 'unavailable'
