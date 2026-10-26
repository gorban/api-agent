"""Prepare locked source-checkout frontend dependencies at operation boundaries."""
import hashlib
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from api_agent.providers.progress import report_timeout_sync


_GUIDANCE = ('Install Node.js 24 or newer and npm using your operating system or Node.js installer, '
             'then retry. You can also run npm ci from the source repository root.')


def source_root():
    """Only the executing source tree owns preparation, never the caller's roots."""
    root = Path(__file__).resolve().parents[3]
    required = ('launch.py', 'pyproject.toml', 'package.json', 'package-lock.json',
                'tools/prepare_frontend_assets.mjs', 'tools/frontend-assets.json')
    source = root / 'src/api_agent/runtime/frontend.py'
    return root if Path(__file__).resolve() == source and all((root / name).is_file() for name in required) else None


def _check_budget(budget, cancellation):
    if cancellation is not None and cancellation.is_set():
        raise InterruptedError('Frontend preparation cancelled')
    if budget.remaining is not None and budget.remaining <= 0:
        raise TimeoutError('Frontend preparation timed out; retry npm ci from the repository root.')


def _command(argv, root, budget, cancellation):
    """Discard potentially credential-bearing installer output and reap the group."""
    _check_budget(budget, cancellation)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(argv, cwd=root, stdout=output if argv[1] == '--version' else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            while True:
                _check_budget(budget, cancellation)
                try:
                    process.wait(timeout=min(.1, budget.remaining or .1))
                    output.seek(0)
                    return process.returncode, output.read(4096)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            from api_agent.testing.processes import finalize
            finalize(process)




def _installed(root):
    try:
        expected = json.loads((root / 'package-lock.json').read_text())['packages']
        actual = json.loads((root / 'node_modules/.package-lock.json').read_text())['packages']
        required = {key: value for key, value in expected.items() if key and not value.get('optional')}
        if (any(actual.get(key) != value for key, value in required.items())
                or any(expected.get(key) != value for key, value in actual.items())):
            return False
        definition_path = root / 'package.json'
        if definition_path.is_file():
            definition = json.loads(definition_path.read_text())
            if any(definition.get(field, {}) != expected[''].get(field, {})
                   for field in ('dependencies', 'devDependencies', 'optionalDependencies')):
                return False
        for relative, details in actual.items():
            path = root / relative
            if not path.resolve().is_relative_to(root / 'node_modules'):
                return False
            package = json.loads((path / 'package.json').read_text())
            if package.get('version') != details['version']:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _preserve_outputs(root):
    """Reject changed existing generated assets before running an installer."""
    manifest = json.loads((root / 'tools/frontend-assets.json').read_text())
    owners = {root}
    contexts = root / 'cfg/workflow-view-roots.json'
    if contexts.is_file():
        for context in json.loads(contexts.read_text())['entries'].values():
            for field in ('base_root', 'pack_root'):
                owner = (root / context[field]).resolve()
                if not owner.is_relative_to(root):
                    raise ValueError('Frontend owner is outside the source repository')
                owners.add(owner)
    for entry in manifest['assets']:
        target = entry['target']
        for owner in owners if target.startswith('.private_resources/viewer/') else (root,):
            path = owner / target
            if not path.resolve().is_relative_to(root):
                raise ValueError('Frontend output is outside the source repository')
            if path.exists() or path.is_symlink():
                if (path.is_symlink() or not path.is_file() or path.stat().st_size != entry['size']
                        or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
                    raise ValueError('Existing frontend output differs from reviewed bytes; '
                                     'preserve/remove that generated file explicitly before npm ci.')


def ensure_source_frontend(*, cancellation=None, timeout=300):
    root = source_root()
    if root is None:
        return
    import fcntl
    with report_timeout_sync('Source frontend preparation', timeout) as budget:
        node = shutil.which('node')
        if node is None:
            raise OSError(_GUIDANCE)
        status, version = _command([node, '--version'], root, budget, cancellation)
        match = re.fullmatch(rb'v(\d+)\.\d+\.\d+(?:[-+][^\s]+)?\s*', version)
        if status or match is None or int(match[1]) < 24:
            raise OSError(_GUIDANCE)
        # One lock per canonical checkout, outside tracked/package resources.
        key = hashlib.sha256(os.fsencode(root)).hexdigest()
        lock = Path(tempfile.gettempdir()) / f'api-agent-frontend-{os.getuid()}-{key}.lock'
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            while True:
                _check_budget(budget, cancellation)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.05)
            helper = [node, 'tools/prepare_frontend_assets.mjs', '--check']
            if _installed(root) and _command(helper, root, budget, cancellation)[0] == 0:
                return
            _preserve_outputs(root)
            npm = shutil.which('npm')
            if npm is None:
                raise OSError(_GUIDANCE)
            print('Preparing locked source frontend dependencies with npm ci...', file=sys.stderr, flush=True)
            lock_bytes = (root / 'package-lock.json').read_bytes()
            status, _ = _command([npm, 'ci', '--include=dev', '--no-audit', '--no-fund'], root, budget, cancellation)
            if (root / 'package-lock.json').read_bytes() != lock_bytes:
                raise ValueError('Frontend preparation changed package-lock.json; review it explicitly.')
            if status or not _installed(root) or _command(helper, root, budget, cancellation)[0]:
                raise OSError('Frontend preparation failed. Run npm ci from the repository root '
                              'to inspect the failure; existing generated output must be preserved or removed explicitly.')


async def prepare_source_frontend():
    from api_agent.runtime.tasks import offload
    cancellation = threading.Event()
    await offload(partial(ensure_source_frontend, cancellation=cancellation), cancellation=cancellation)
