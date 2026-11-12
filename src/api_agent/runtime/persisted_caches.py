"""Caches a pack persists in the user's cache directory, declared so they can be reviewed and purged."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys

_IDENTIFIER = re.compile(r'[a-z][a-z0-9-]{0,63}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')


def user_cache_directory(name):
    """The per-user cache directory an application named ``name`` persists into."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / name
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / name


@dataclass(frozen=True)
class PersistedCache:
    id: str
    label: str
    user_cache_dir: str

    @property
    def key(self):
        return f'cache:{self.id}'

    @property
    def directory(self):
        return user_cache_directory(self.user_cache_dir)

    def present(self):
        return self.directory.is_symlink() or self.directory.exists()

    def summary(self):
        size = 0 if self.directory.is_symlink() else sum(
            path.stat().st_size for path in self.directory.rglob('*') if path.is_file() and not path.is_symlink())
        return f'{self.label} ({self.directory}, {size} bytes)'

    def purge(self):
        """Remove the whole cache; a symlink is removed itself, never followed."""
        if self.directory.is_symlink():
            self.directory.unlink()
        elif self.directory.exists():
            shutil.rmtree(self.directory)


def parse_persisted_caches(declared):
    """Validate a pack's ``persisted_caches`` table array."""
    from .resource_pack import ResourcePackError
    if type(declared) is not list:
        raise ResourcePackError('persisted_caches must be an array of tables')
    caches = []
    for entry in declared:
        if (type(entry) is not dict or set(entry) != {'id', 'label', 'user_cache_dir'}
                or any(type(value) is not str for value in entry.values())
                or not _IDENTIFIER.fullmatch(entry['id'])
                or not entry['label'].isprintable() or not entry['label'].strip()
                or not _NAME.fullmatch(entry['user_cache_dir'])):
            raise ResourcePackError('persisted_caches entries require an id, a printable label and a '
                                    'user_cache_dir naming one directory')
        caches.append(PersistedCache(entry['id'], entry['label'], entry['user_cache_dir']))
    if len({cache.id for cache in caches}) != len(caches):
        raise ResourcePackError('persisted_caches ids must be unique')
    return tuple(caches)
