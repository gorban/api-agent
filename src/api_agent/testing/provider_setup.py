"""Lazy provider setup for launch-owned integration workers."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError, InteractionRequiredError, SetupPrerequisiteError
from api_agent.providers.loading import aload_provider
from api_agent.providers.sessions import InspectionOwner, LLMSessionOwner
from api_agent.providers.shared_setup import SharedSetupOwner


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


_inspection_providers = []


class IntegrationLLM:
    """One worker-local operation; this does not implement backend-wide IPC."""
    def __init__(self, config, owner_root):
        self.config = dict(config)
        self.owner_root = owner_root
        self.loop = asyncio.new_event_loop()
        self.provider = self.owner = None
        self._provider_config = deepcopy(self.config)
        self._shared_setup = None
        self._operation_id = str(uuid4())
        self._setup_lock = asyncio.Lock()
        self._retained_owners = []
        self._credential_tasks = set()
        self.closed = False
        self.inspection = InspectionOwner(on_session=self._show_browser) if self.config.get('DEBUG', False) else None

    @staticmethod
    def _show_browser(session):
        if session.inspection_endpoint:
            print('Provider browser inspection: ' + session.inspection_endpoint, flush=True)

    async def _release_retained(self):
        while self._retained_owners:
            await self._retained_owners[-1].aclose()
            self._retained_owners.pop()

    def _credentials(self, index):
        from api_agent.auth.profiles import normalize_auth, parse_profiles, validate_role_entries
        from api_agent.auth import credentials as helpers
        from api_agent.providers.sessions import ScopedRoleCredentials, ScopedLLMCredentials
        role = self.config.get('LLM_CREDENTIAL_ROLE')
        try:
            canonical = normalize_auth(self.config.get('UNATTENDED_AUTH', {}))
            entries = canonical['roles'].get(role, [])
            if not role:
                return None
            validate_role_entries(role, entries)
            if not entries:
                raise SetupPrerequisiteError(f'{role}: configure an account in unattended authentication setup.')
            if not canonical['password_manager']['manager']:
                raise SetupPrerequisiteError('Configure the shared password manager before unattended authentication.')
            profiles = parse_profiles({'password_manager': canonical['password_manager'],
                                       'roles': {role: entries}}, (role,))
            if type(index) is not int or index < 0 or index >= len(profiles.for_role(role)):
                raise ValueError
            source = profiles.for_role(role)[index]
        except SetupPrerequisiteError:
            raise
        except (ValueError, TypeError):
            raise ContractError('The selected unattended login descriptor is incomplete or invalid.') from None

        async def retrieve(selected_role, selected_index, kind):
            helper = {'username': helpers.get_username, 'password': helpers.get_password,
                      'totp': helpers.get_totp}[kind]
            task = asyncio.create_task(asyncio.to_thread(helper, source))
            self._credential_tasks.add(task)
            try:
                return await asyncio.shield(task)
            except helpers.CredentialError:
                raise InteractionRequiredError("Password-manager retrieval is unavailable. Sign in to the configured CLI and check the login entry.") from None
        return ScopedLLMCredentials(role, ScopedRoleCredentials((role,), index, retrieve))

    async def _prepare_setup(self):
        await self._release_retained()
        location = self.config.get('LLM_PROVIDER')
        if location is None or location == '':
            raise SetupPrerequisiteError('Select LLM_PROVIDER before requesting LLM setup.')
        if self.provider is None:
            self._provider_config = deepcopy(self.config)
            try:
                services = {'owner_root': self.owner_root}
                if self.inspection is not None:
                    services['browser_inspection'] = self.inspection
                self.provider = await aload_provider(location, deepcopy(self._provider_config), services, kind='llm')
            except BaseException as error:
                self._retained_owners.extend(getattr(error, 'retained_owners', ()))
                raise
        if (self._shared_setup is None
                and self._setup_identity(self.config) != self._setup_identity(self._provider_config)):
            provider, self.provider = self.provider, None
            self._provider_config = None
            primary = ContractError('Integration provider selection changed during setup.')
            try:
                await provider.aclose()
            except BaseException:
                self._retained_owners.append(provider)
                primary.retained_owners = (provider,)
            raise primary
        implementation = self.provider.implementation
        if not callable(getattr(implementation, 'asetup', None)):
            raise SetupPrerequisiteError('The selected LLM provider has no setup capability.')
        if self._shared_setup is None:
            index = self._provider_config.get('UNATTENDED_USER_INDEX')
            credentials = self._credentials(index) if index is not None else None
            identity = self._selected_identity(self._provider_config)
            def make_owner(scoped_credentials):
                self.owner = LLMSessionOwner(implementation, user_index=index,
                                             credentials=scoped_credentials,
                                             interactive=index is None and (self.inspection is not None or not self._provider_config.get('NO_STD_IN', False)))
                return self.owner
            self._shared_setup = SharedSetupOwner(self._operation_id, identity, make_owner,
                                                 credentials=credentials)

    def _selected_identity(self, config=None):
        config = self.config if config is None else config
        return json.dumps([config.get('LLM_PROVIDER'), config.get('LLM_CREDENTIAL_ROLE'),
                           config.get('UNATTENDED_USER_INDEX')], separators=(',', ':'))

    def _setup_identity(self, config):
        return self._selected_identity(config), json.dumps(config.get('UNATTENDED_AUTH'),
                                                         sort_keys=True, separators=(',', ':'))

    async def _ensure(self):
        async with self._setup_lock:
            await self._prepare_setup()
        await self._shared_setup.ensure_ready(selected_identity=self._selected_identity())

    def ensure_ready(self):
        if self.closed:
            raise ContractError('Integration provider setup is closed.')
        try:
            self.loop.run_until_complete(self._ensure())
        except SetupPrerequisiteError as error:
            pytest.skip(str(error))

    async def _close(self):
        await self._release_retained()
        if self._shared_setup is not None:
            await self._shared_setup.aclose()
        elif self.owner is not None:
            await self.owner.aclose()
        if self.provider is not None:
            await self.provider.aclose()
        if self._credential_tasks:
            await asyncio.shield(asyncio.gather(*self._credential_tasks, return_exceptions=True))
            self._credential_tasks.clear()

    def finish_run(self):
        """Dispose authentication while preserving the creating inspection loop."""
        self.loop.run_until_complete(self._close())
        if self.inspection is not None and self.inspection.sessions:
            _inspection_providers.append(self)
        else:
            self.close()

    def close(self):
        if not self.closed:
            self.loop.run_until_complete(self._close())
            if self.inspection is not None and not self.inspection._closed:
                try:
                    if self.inspection.sessions:
                        print('Debug browser retained. Close its windows to finish inspection; Ctrl+C stops the run.', flush=True)
                    self.loop.run_until_complete(self.inspection.wait_closed())
                finally:
                    self.loop.run_until_complete(self.inspection.aclose())
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
        if os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
            provider.finish_run()
        else:
            provider.close()


def serve_inspections(send):
    """Called after pytest has finalized all reports and session teardown."""
    providers = list(_inspection_providers)
    _inspection_providers.clear()
    if not providers:
        return
    command = Path(os.environ['API_AGENT_INTEGRATION_EVENTS']).parent / 'inspection-command.json'
    for provider in providers:
        for session in provider.inspection.sessions:
            send(kind='inspection_open', endpoint=session.inspection_endpoint or '')
    seen = 0
    pending = set()
    while providers:
        attempt = None
        try:
            row = json.loads(command.read_text())
            if (isinstance(row, dict) and type(row.get('version')) is int and row['version'] == 1
                    and row.get('command') == 'end_inspection'
                    and type(row.get('attempt')) is int and row['attempt'] > seen):
                attempt = seen = row['attempt']
        except (OSError, ValueError):
            pass
        for provider in tuple(providers):
            inspector = provider.inspection
            windows = any(session.browser.is_connected() and session.context.pages
                          for session in inspector.sessions)
            if attempt is not None or (not windows and provider not in pending):
                try:
                    provider.loop.run_until_complete(inspector.aclose())
                except Exception:
                    pending.add(provider)
                    send(kind='inspection_cleanup_pending', attempt=attempt)
                else:
                    provider.closed = True
                    provider.loop.close()
                    providers.remove(provider)
            else:
                provider.loop.run_until_complete(asyncio.sleep(.03))
    send(kind='inspection_closed', attempt=seen)


class IntegrationRequirements:
    """Test-local configuration prerequisites, checked before side effects.

    Tests with isolated configuration should pass that resolved mapping instead
    of the launch mapping. False and zero are provided values, not missing ones.
    Reasons name settings/requirements, never their potentially secret values.
    """
    def __init__(self, values):
        self.values = dict(values)

    def require(self, *names):
        missing = [name for name in names if self.values.get(name) is None or self.values.get(name) == '']
        if missing:
            pytest.skip('Missing required configuration: ' + ', '.join(missing))

    def require_combination(self, valid, reason):
        if not valid:
            pytest.skip(reason)


@pytest.fixture(scope='session')
def integration_config():
    """Require only settings used by this test, not unrelated suite settings."""
    return IntegrationRequirements(_read_snapshot()['config'])


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_pack_api: selected integration requires pack API/identity preflight")
