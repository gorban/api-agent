"""Where each managed scope keeps its ownership journals.

A named volume carries no information about the checkout that uses it, and its
containers are removed at release, so an unused volume alone cannot say whether
its checkout still exists or when it was last used. Every acquire records the
journal directory of its scope here, one small private file per scope under the
user's cache directory, so unused storage can be judged later from any checkout.

Everything here is best-effort and bounded. A missing, oversized, malformed or
untrusted record means "unknown", and unknown always preserves storage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

LIMIT = 16
MAX_BYTES = 65536
_SCOPE = re.compile(r'[0-9a-f]{16}')


def index_directory():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'api-agent' / 'scopes'


def _path(scope_id):
    if not isinstance(scope_id, str) or not _SCOPE.fullmatch(scope_id):
        raise ValueError('Invalid scope identifier.')
    return index_directory() / f'{scope_id}.json'


def directories(scope_id):
    """The journal directories recorded for a scope, or ``None`` when they are unknown."""
    try:
        path = _path(scope_id)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_BYTES):
            return None
        document = json.loads(path.read_bytes())
        listed = document['directories']
        if (set(document) != {'directories'} or type(listed) is not list or not listed
                or len(listed) > LIMIT or len(set(listed)) != len(listed)
                or any(type(item) is not str or not os.path.isabs(item) or '..' in Path(item).parts
                       for item in listed)):
            return None
        return tuple(Path(item) for item in listed)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def modified(scope_id):
    """When the scope was last acquired (epoch seconds), or ``None`` when that is unknown."""
    try:
        info = _path(scope_id).lstat()
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None
    return info.st_mtime


def record(scope_id, directory, *, used=True):
    """Remember ``directory`` as a journal directory of the scope; never raises.

    ``used`` marks the call as an acquire, which refreshes the scope's last-use time; the reaper only
    remembers what it saw and does not.
    """
    try:
        path = _path(scope_id)
        value = str(directory)
        current = [str(item) for item in directories(scope_id) or ()]
        if value in current:
            if used:
                # Nothing to rewrite, but this acquire is the scope's latest use.
                os.utime(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.parent / f'{scope_id}.{uuid4().hex}.tmp'
        with temporary.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps({'directories': [*current, value][-LIMIT:]}))
        temporary.replace(path)
    except (OSError, ValueError):
        return


def forget(scope_id):
    """Drop a scope's record once nothing of it can remain; never raises."""
    try:
        _path(scope_id).unlink(missing_ok=True)
    except (OSError, ValueError):
        return
