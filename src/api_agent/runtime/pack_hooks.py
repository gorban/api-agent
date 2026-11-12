"""Install the Git hooks a resource pack declares, never replacing a user-owned hook."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from api_agent.config.process_settings import process_setting
from api_agent.runtime.hook_assurance import _git, _git_environment

MARKER = '# api-agent managed pack hook v1'
MAX_HOOK_BYTES = 4096


def _wrapper(relative):
    # The declared script stays in the checkout, so updating it needs no reinstall.
    return f'#!/bin/sh\n{MARKER}\nexec "$(git rev-parse --show-toplevel)/{relative}" "$@"\n'.encode()


def _checkout(root):
    """Return the hook directory, any configured hooksPath and whether hooks are shared."""
    if shutil.which('git') is None:
        return None
    try:
        top, hooks, git_dir, common_dir = _git(
            root, 'rev-parse', '--show-toplevel', '--git-path', 'hooks',
            '--git-dir', '--git-common-dir').decode().splitlines()
    except subprocess.CalledProcessError:
        return None
    if Path(top).resolve() != root:
        return None
    configured = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=root,
                                env=_git_environment(), capture_output=True,
                                timeout=process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS'))
    return (root / hooks,
            Path(configured.stdout.decode().strip()).expanduser() if configured.returncode == 0 else None,
            (root / git_dir).resolve() != (root / common_dir).resolve())


def assure_pack_hooks(root, hooks, *, report=None):
    """Install a small wrapper per declared (name, relative script) hook; return each outcome."""
    report = report or (lambda message: print(message, file=sys.stderr))
    root = Path(root).resolve()
    if not hooks:
        return {}
    checkout = _checkout(root)
    if checkout is None:
        return {name: 'not-applicable' for name, _ in hooks}
    return {name: _assure(root, checkout, name, relative, report) for name, relative in hooks}


def _assure(root, checkout, name, relative, report):
    directory, configured, shared = checkout
    wrapper = _wrapper(relative)
    if configured is not None:
        if (root / configured).resolve() == (root / relative).resolve().parent and Path(relative).name == name:
            return 'present'
        report(f'Pack {name} hook not installed: custom hooksPath; run {relative} from it manually.')
        return 'foreign'
    slot = directory / name
    previous = None
    if slot.exists() or slot.is_symlink():
        current = None
        if slot.is_file() and not slot.is_symlink():
            with slot.open('rb') as stream:
                current = stream.read(MAX_HOOK_BYTES)
        if current == wrapper:
            return 'present'
        if current is None or MARKER.encode() not in current.split(b'\n', 2)[:2]:
            report(f'Pack {name} hook not installed: existing hook; run {relative} from it manually.')
            return 'foreign'
        previous = current
    if shared:
        report(f'Pack {name} hook not installed: shared Git hook directory; run {relative} from it manually.')
        return 'foreign'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.api-agent-pack-hook-', dir=directory) as temporary:
            staged = Path(temporary) / name
            staged.write_bytes(wrapper)
            staged.chmod(0o755)
            # A hard link fails if another owner took the slot concurrently.
            if previous is None:
                os.link(staged, slot)
            else:
                os.replace(staged, slot)
    except OSError as error:
        report(f'Pack {name} hook unavailable: {type(error).__name__}; run {relative} from it manually.')
        return 'unavailable'
    outcome = 'installed' if previous is None else 'updated'
    report(f'Pack {name} hook {outcome}; checks run only when committing.')
    return outcome
