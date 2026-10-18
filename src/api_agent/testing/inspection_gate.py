"""Local readiness handoff after immutable suite accounting has finalized."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from api_agent.providers.progress import report_timeout_sync

MARKER = 'inspection-accounting-ready.json'
FINALIZED = {'version': 1, 'status': 'finalized'}
LIMIT = 256


@contextmanager
def _channel(path):
    # The exact private IPC leaf is the authority. Its temporary parent may be
    # a sticky directory mapped to another UID by a rootless container.
    descriptor = os.open(Path(path).absolute(), os.O_RDONLY | os.O_DIRECTORY
                         | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError('Inspection channel is not a private owned directory.')
        yield descriptor
    finally:
        os.close(descriptor)


def _read(directory):
    descriptor = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > LIMIT):
            raise ValueError('Inspection marker is not private bounded data.')
        body = os.read(descriptor, LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError('Inspection marker exceeds its bounded size.')
        return json.loads(body)
    finally:
        os.close(descriptor)


def _write(directory):
    temporary = MARKER + '.' + uuid4().hex
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        created = True
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(json.dumps(FINALIZED, sort_keys=True).encode())
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, MARKER, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def manual_accounting_ready():
    """Fail closed without authentication, network work or blocking FIFO reads."""
    event_path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    if not event_path:
        return False
    try:
        with _channel(Path(event_path).parent) as directory:
            row = _read(directory)
        return (type(row) is dict and type(row.get('version')) is int
                and row == FINALIZED)
    except (OSError, ValueError):
        return False


def publish_manual_accounting_ready(inspections):
    """Only the parent publisher can open retained worker interaction here."""
    from .inspection import IntegrationInspection
    with report_timeout_sync('Retained interaction readiness', 2):
        for inspection in inspections:
            if isinstance(inspection, IntegrationInspection) and not inspection.closed:
                with _channel(inspection.directory.temporary.name) as directory:
                    _write(directory)
