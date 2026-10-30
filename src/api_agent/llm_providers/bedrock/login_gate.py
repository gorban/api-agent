"""Cancellable cross-process admission for one persisted native AWS session."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import stat

from api_agent.providers.contracts import ContractError, SetupPrerequisiteError
from api_agent.llm_providers.bedrock.persistence import directory_fd


@asynccontextmanager
async def profile_login_gate(paths, profile):
    """Hold a distinct lock from worker publication; never wait in a thread.

    The caller's component deadline covers admission and the entire acquisition.
    The descriptor closes on cancellation/process exit, releasing native flock.
    No credential content enters this coordination file.
    """
    if os.name != 'posix':
        raise SetupPrerequisiteError('AWS sign-in currently requires POSIX process isolation.')
    import fcntl
    credentials = Path(paths['credentials'])
    if not credentials.is_absolute() or profile is not None and type(profile) is not str:
        raise ContractError('Invalid AWS sign-in coordination binding')
    name = '.api-agent-session-' + hashlib.sha256(
        (credentials.name + '\0' + (profile or '')).encode()).hexdigest() + '.lock'
    with directory_fd(credentials.parent, create=True) as directory:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ContractError('Unsafe AWS sign-in coordination file')
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.05)
        yield
    finally:
        os.close(fd)
