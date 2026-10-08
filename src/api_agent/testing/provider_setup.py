"""Lazy provider setup for launch-owned integration workers."""
import asyncio
import json
import os
from pathlib import Path
import stat

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.loading import aload_provider
from api_agent.providers.sessions import LLMSessionOwner


def write_launch_snapshot(directory, context, config):
    """Write configuration to a protected, operation-owned IPC file."""
    path = Path(directory) / 'launch.json'
    payload = json.dumps({'version': 1, 'owner_root': str(context.owner_root),
                          'config': dict(config)}, default=str, allow_nan=False).encode()
    if len(payload) > 1048576:
        raise ContractError('Integration configuration is too large.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(payload)
    return path


def _read_snapshot():
    try:
        path = os.environ['API_AGENT_INTEGRATION_CONTEXT']
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError
        data = json.loads(raw)
        if (set(data) != {'version', 'owner_root', 'config'} or type(data['version']) is not int
                or data['version'] != 1 or not isinstance(data['config'], dict)
                or not isinstance(data['owner_root'], str) or not Path(data['owner_root']).is_absolute()):
            raise ValueError
        return data
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise ContractError('Integration provider context is unavailable; start through launch.py.')


class IntegrationLLM:
    """One worker-owned loop; provider setup starts only on ensure_ready."""
    def __init__(self, config, owner_root):
        self.config = dict(config)
        self.owner_root = owner_root
        self.loop = asyncio.new_event_loop()
        self.provider = self.owner = None
        self._retained_owners = []
        self.closed = False

    async def _release_retained(self):
        while self._retained_owners:
            await self._retained_owners[-1].aclose()
            self._retained_owners.pop()

    async def _ensure(self):
        await self._release_retained()
        location = self.config.get('LLM_PROVIDER')
        if not location:
            raise ContractError('Select LLM_PROVIDER before requesting LLM setup.')
        if self.provider is None:
            try:
                self.provider = await aload_provider(location, self.config,
                                                    {'owner_root': self.owner_root}, kind='llm')
            except BaseException as error:
                self._retained_owners.extend(getattr(error, 'retained_owners', ()))
                raise
        implementation = self.provider.implementation
        if not callable(getattr(implementation, 'asetup', None)):
            raise ContractError('The selected LLM provider has no setup capability.')
        if self.owner is None:
            index = self.config.get('UNATTENDED_USER_INDEX')
            self.owner = LLMSessionOwner(implementation, user_index=index,
                                         interactive=index is None and not self.config.get('NO_STD_IN', False))

        async def checkpoint(*, auth):
            if auth is None:
                raise ContractError('The selected LLM provider did not provide authentication.')
        await self.owner.call(checkpoint)

    def ensure_ready(self):
        if self.closed:
            raise ContractError('Integration provider setup is closed.')
        self.loop.run_until_complete(self._ensure())

    async def _close(self):
        await self._release_retained()
        if self.owner is not None:
            await self.owner.aclose()
        if self.provider is not None:
            await self.provider.aclose()

    def close(self):
        if not self.closed:
            self.loop.run_until_complete(self._close())
            self.closed = True
            self.loop.close()


@pytest.fixture(scope='session')
def llm_provider():
    """Requesting the fixture is inert; ensure_ready is the authentication gate."""
    data = _read_snapshot()
    provider = IntegrationLLM(data['config'], data['owner_root'])
    try:
        yield provider
    finally:
        provider.close()
