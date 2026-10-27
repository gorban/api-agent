"""Isolate exact fixture counters while ordinary service leases remain shared."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
from hashlib import sha256
import ipaddress
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from api_agent.providers.progress import component_timeout
from api_agent.telemetry.history import cache_path


def authorities(endpoints):
    """Different routes/aliases on one backend share one measurement lock."""
    result = set()
    for endpoint in endpoints:
        try:
            if type(endpoint) is not str or any(character.isspace() for character in endpoint):
                raise ValueError
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
                raise ValueError
            host = parsed.hostname.rstrip('.').lower()
            try:
                host = ipaddress.ip_address(host).compressed
            except ValueError:
                pass
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
            if not host or not port:
                raise ValueError
            result.add((parsed.scheme, host, port))
        except ValueError:
            raise ValueError('Invalid fixture measurement target.') from None
    if not result:
        raise ValueError('Fixture measurement requires a target.')
    return tuple(sorted(result))


def _open(directory, authority):
    """Use private, stable locking inodes; never unlink a live lock file."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Fixture measurement directory must be private and user-owned.')
        name = sha256(repr(authority).encode()).hexdigest() + '.lock'
        descriptor = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise ValueError('Fixture measurement lock must be private and user-owned.')
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(directory_fd)


@asynccontextmanager
async def measure_targets(endpoints, *, directory=None, timeout=960):
    """Acquire after service leases, release before their ordinary cleanup."""
    targets = authorities(endpoints)
    directory = directory if directory is not None else cache_path().parent / 'tool-measurement-locks'
    descriptors = []
    try:
        async with component_timeout('Tool fixture measurement lock wait', timeout):
            for target in targets:
                descriptor = _open(directory, target)
                descriptors.append(descriptor)
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(.05)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
