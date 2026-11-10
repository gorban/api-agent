"""Install a declared Node workspace's locked dependencies at an explicit boundary.

The workspace belongs to its caller and is only populated when it has no
dependency tree at all. Registry credentials reach the installer's environment
alone, never this process's environment, and installer output is discarded.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import time

from api_agent.providers.progress import report_timeout_sync
from api_agent.runtime.frontend import _GUIDANCE, _command
from api_agent.runtime.mise_tools import ensure_tools


LOCK_DIRECTORY = Path(tempfile.gettempdir())
_UNBOUNDED = SimpleNamespace(remaining=None)
_TIMEOUT = 'Node workspace preparation timed out; run npm ci in the workspace to inspect the failure.'
_CANCELLED = 'Node workspace preparation cancelled'


def _check(budget, cancellation):
    if cancellation is not None and cancellation.is_set():
        raise InterruptedError(_CANCELLED)
    if budget.remaining is not None and budget.remaining <= 0:
        raise TimeoutError(_TIMEOUT)


@contextmanager
def _locked(root, budget, cancellation):
    """One lock per canonical workspace, outside the workspace itself."""
    budget = _UNBOUNDED if budget is None else budget
    key = hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()
    path = LOCK_DIRECTORY / f'api-agent-node-workspace-{os.getuid()}-{key}.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        while True:
            _check(budget, cancellation)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.05)
        yield


def _ready(root, ready):
    return all((root / relative).exists() for relative in ready)


def ensure_node_workspace(root, *, ready, credentials=None, environment=None, cancellation=None, timeout=900):
    """Run a locked ``npm ci`` when the workspace has no dependency tree.

    ``ready`` lists workspace-relative paths proving a usable installation. An
    existing tree that satisfies them is trusted; one that does not is never
    replaced, because ``npm ci`` deletes it first. Missing credentials stop the
    run before anything is touched and are reported by name only.
    """
    root = Path(root).expanduser().resolve()
    if not (root / 'package.json').is_file() or not (root / 'package-lock.json').is_file():
        raise ValueError(f'Node workspace {root} needs package.json and package-lock.json.')
    if _ready(root, ready):
        return
    missing = [name for name, value in (credentials or {}).items() if not value]
    if missing:
        raise ValueError(f'Node workspace {root} needs its locked dependencies installed; set '
                         f'{", ".join(missing)} in configuration and retry, or run npm ci there yourself.')
    with report_timeout_sync('Node workspace preparation', timeout) as budget:
        ensure_tools('node', 'npm', timeout=budget.remaining, cancellation=cancellation)
        npm = shutil.which('npm')
        if npm is None:
            raise OSError(_GUIDANCE)
        with _locked(root, budget, cancellation):
            if _ready(root, ready):
                return
            if (root / 'node_modules').exists():
                raise ValueError(f'Node workspace {root} has dependencies that do not match its declared '
                                 'readiness; refusing to replace them. Run npm ci there yourself.')
            lock_bytes = (root / 'package-lock.json').read_bytes()
            child = {**os.environ, **(environment or {}), **(credentials or {})}
            try:
                status, _ = _command([npm, 'ci', '--include=dev', '--no-audit', '--no-fund'], root, budget,
                                     cancellation, env=child)
            except TimeoutError:
                raise TimeoutError(_TIMEOUT) from None
            except InterruptedError:
                raise InterruptedError(_CANCELLED) from None
            if (root / 'package-lock.json').read_bytes() != lock_bytes:
                raise OSError(f'Node workspace preparation changed package-lock.json in {root}; review it explicitly.')
            if status or not _ready(root, ready):
                raise OSError(f'Node workspace preparation failed in {root}; run npm ci there to inspect the failure.')
