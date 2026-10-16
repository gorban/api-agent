"""Bounded user-owned metadata for local reporting and invocation leases."""
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .prerequisites import PrerequisiteError

LIMIT = 65536


def _directory(path, *, create=False):
    path = Path(path).absolute()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for item in (path, *path.parents):
        info = item.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise PrerequisiteError('Reporting ownership directory is not trusted.')
    if path.stat().st_uid != os.getuid():
        raise PrerequisiteError('Reporting ownership directory must belong to this user.')
    return path


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PrerequisiteError('Cannot open reporting ownership metadata.') from error
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > LIMIT):
            raise ValueError()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            body = stream.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError()
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError) as error:
        raise PrerequisiteError('Invalid reporting ownership metadata.') from error
    finally:
        os.close(descriptor)


def _write(path, value):
    payload = json.dumps(value, sort_keys=True)
    if len(payload.encode()) > LIMIT:
        raise PrerequisiteError('Reporting ownership metadata exceeds its bounded size.')
    temporary = path.with_name(path.name + '.' + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
