"""Inert, bounded provider-owned test declarations."""
import os
from pathlib import Path
import stat
import tomllib

from .collection import CollectionError


def load_test_declaration(root):
    root = Path(root)
    manifest = root / 'cfg/tests.toml'
    if not manifest.exists():
        return None
    if not manifest.resolve().is_relative_to(root):
        raise CollectionError(f'Provider test declaration escapes its root: {manifest}')
    try:
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not a regular file')
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError('declaration too large')
        data = tomllib.loads(raw.decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise CollectionError(f'Cannot read provider test declaration: {manifest}') from exc
    tests = data.get('tests')
    if (set(data) != {'version', 'tests'} or type(data.get('version')) is not int
            or data['version'] != 1 or not isinstance(tests, dict)
            or set(tests) != {'integration'}
            or not isinstance(tests.get('integration'), list)):
        raise CollectionError(f'Invalid provider test declaration: {manifest}')
    return tests
