"""Prepare an index snapshot from reviewed installed npm originals, without npm."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat


GUIDANCE = 'Frontend dependencies are missing or invalid; run npm ci from the repository root.'
TARGETS = {
    '.private_resources/viewer/' + name for name in (
        'marked.js', 'dompurify.js', 'heading-id.js', 'marked-LICENSE.txt',
        'dompurify-LICENSE.txt', 'heading-id-LICENSE.txt', 'github-slugger-LICENSE.txt')
} | {'apps/api-agent-ui/' + name for name in (
    'public/marked.js', 'public/dompurify.js', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt')}


def _read(root: Path, relative: str, limit: int = 2_000_000) -> bytes:
    parts = relative.split('/')
    if (not relative or len(relative) > 1024 or relative.startswith('/') or
            any(part in ('', '.', '..') or re.search(r'[\\\x00-\x1f\x7f]', part) for part in parts)):
        raise ValueError('Invalid frontend input path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(child, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('Frontend input must be a bounded regular file')
            data = source.read(limit + 1)
            if len(data) != info.st_size:
                raise ValueError('Frontend input changed while reading')
            return data
    finally:
        os.close(descriptor)


def prepare_snapshot(snapshot: Path, installed_root: Path) -> None:
    """Trust staged contracts only; working manifests and generated JS are irrelevant."""
    try:
        manifest = json.loads(_read(snapshot, 'tools/frontend-assets.json'))
        lock = json.loads(_read(snapshot, 'package-lock.json'))
        definition = json.loads(_read(snapshot, 'package.json'))
        entries = manifest['assets']
        if (manifest['version'] != 1 or lock['lockfileVersion'] != 3 or
                len(entries) != len(TARGETS) or {entry['target'] for entry in entries} != TARGETS):
            raise ValueError('Invalid staged frontend inventory')
        prepared = []
        for entry in entries:
            package, version = entry['package'], entry['version']
            if (not re.fullmatch(r'[a-z0-9][a-z0-9-]*', package) or
                    not re.fullmatch(r'\d+\.\d+\.\d+', version) or
                    not re.fullmatch(r'[a-f0-9]{64}', entry['sha256']) or
                    type(entry['size']) is not int or not 0 < entry['size'] <= 2_000_000 or
                    definition['dependencies'][package] != version or
                    lock['packages']['']['dependencies'][package] != version):
                raise ValueError('Invalid staged frontend dependency')
            locked = lock['packages']['node_modules/' + package]
            if (locked['version'] != version or
                    not re.fullmatch(r'https://registry\.npmjs\.org/[A-Za-z0-9@/_.%~-]+', locked['resolved']) or
                    not re.fullmatch(r'sha512-[A-Za-z0-9+/]+={0,2}', locked['integrity'])):
                raise ValueError('Invalid staged frontend lock')
            installed = json.loads(_read(installed_root, 'node_modules/' + package + '/package.json'))
            if installed['name'] != package or installed['version'] != version:
                raise ValueError('Installed frontend identity differs')
            data = _read(installed_root, 'node_modules/' + package + '/' + entry['source'])
            if len(data) != entry['size'] or sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('Installed frontend original differs from staged manifest')
            prepared.append((entry['target'], data))
        for relative, data in prepared:
            target = snapshot / relative
            # Snapshot directories and files were created solely from validated index paths.
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _read(snapshot, relative) != data:
                    raise ValueError('Staged frontend output differs from reviewed original')
            else:
                with target.open('xb') as output:
                    output.write(data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(GUIDANCE) from error
