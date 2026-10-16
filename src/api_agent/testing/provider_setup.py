"""Lazy provider setup for launch-owned integration workers."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import traceback
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError, UnattendedCredentialError, SetupPrerequisiteError
from api_agent.providers.loading import aload_provider
from api_agent.providers.sessions import InspectionOwner, LLMSessionOwner
from api_agent.providers.shared_setup import SharedSetupOwner


def _configured_request(request, config):
    """Freeze effective options before transport and accounting see the call."""
    from api_agent.providers.cache_policy import configured_request
    return configured_request(request, config.get('LLM_CACHE_POLICY'),
                              reasoning_effort=config.get('LLM_REASONING_EFFORT'))


def write_launch_snapshot(directory, context, config, *, unattended_index_source=None, setup_backend=None,
                          password_manager_registry=None, interaction_descriptor=None):
    """Write configuration to a protected, operation-owned IPC file."""
    path = Path(directory) / 'launch.json'
    from api_agent.auth.managers import registry_for_context
    registry = (registry_for_context(context) if password_manager_registry is None
                else password_manager_registry)
    data = {'version': 1, 'owner_root': str(context.owner_root), 'config': dict(config),
            'password_manager_definitions': _manager_definitions(registry)}
    if getattr(context, 'external_pack', False):
        data['password_manager_pack_root'] = str(context.pack_root)
    if unattended_index_source is not None:
        data['unattended_index_source'] = unattended_index_source
    if setup_backend is not None:
        from api_agent.providers.setup_transport import validate_descriptor
        data['setup_backend'] = validate_descriptor(setup_backend)
    if interaction_descriptor is not None:
        from api_agent.auth.interaction_transport import validate_descriptor
        data['interaction'] = validate_descriptor(interaction_descriptor)
    payload = json.dumps(data, default=str, allow_nan=False).encode()
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
        required = {'version', 'owner_root', 'config', 'password_manager_definitions'}
        if (not required <= set(data) or set(data) - required - {'unattended_index_source', 'setup_backend', 'password_manager_pack_root', 'interaction'}
                or ('unattended_index_source' in data and not isinstance(data['unattended_index_source'], str))
                or not isinstance(data['password_manager_definitions'], list)
                or type(data['version']) is not int
                or data['version'] != 1 or not isinstance(data['config'], dict)
                or not isinstance(data['owner_root'], str) or not Path(data['owner_root']).is_absolute()):
            raise ValueError
        if ('password_manager_pack_root' in data and
                (not isinstance(data['password_manager_pack_root'], str)
                 or not Path(data['password_manager_pack_root']).is_absolute())):
            raise ValueError
        if 'setup_backend' in data:
            from api_agent.providers.setup_transport import validate_descriptor
            data['setup_backend'] = validate_descriptor(data['setup_backend'])
        if 'interaction' in data:
            from api_agent.auth.interaction_transport import validate_descriptor
            data['interaction'] = validate_descriptor(data['interaction'])
        return data
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise ContractError('Integration provider context is unavailable; start through launch.py.')


def _manager_definitions(registry):
    """JSON-comparable launch-time definitions, without importing adapters."""
    return [{'id': item.id, 'label': item.label, 'implementation': str(item.implementation),
             'cli_default': item.cli_default} for item in registry.definitions]


def _worker_manager_registry(data):
    from api_agent.auth.managers import load_manager_registry, ManagerError
    try:
        registry = load_manager_registry(pack_root=data.get('password_manager_pack_root'))
        if _manager_definitions(registry) == data['password_manager_definitions']:
            return registry
    except ManagerError:
        pass
    raise ContractError('Password manager declarations changed after launch; restart through launch.py.')


_inspection_providers = []


class IntegrationLLM:
    """Creating-loop operation owner; shared consumers reach it through the backend."""
    def __init__(self, config, owner_root, *, unattended_index_source=None, loop=None,
                 password_manager_registry=None, password_manager_pack_root=None, interaction=None, quota_observer=None,
                 skip_unavailable=True):
        from api_agent.auth.interaction_transport import OwnedInteraction
        self.interaction = None if interaction is None else OwnedInteraction(interaction)
        self.config = dict(config)
        self.skip_unavailable = skip_unavailable
        from .inference_content import content_boundary
        self.emissions = content_boundary(self.config)
        self.owner_root = owner_root
        from api_agent.auth.managers import load_manager_registry, registry_for_owner
        self.password_manager_registry = (
            password_manager_registry if password_manager_registry is not None else
            load_manager_registry(pack_root=password_manager_pack_root)
            if password_manager_pack_root is not None else registry_for_owner(owner_root))
        self.unattended_index_source = unattended_index_source
        self.loop = asyncio.new_event_loop() if loop is None else loop
        self.provider = self.owner = None
        self._provider_config = deepcopy(self.config)
        self._shared_setup = None
        self._operation_id = str(uuid4())
        self._setup_lock = asyncio.Lock()
        self._quota_lock = asyncio.Lock()
        self._quota_observer = quota_observer
        self._quota_before = self._quota_after = False
        self._execution_finished = False
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
            canonical = normalize_auth(self.config.get('UNATTENDED_AUTH', {}),
                                       registry=self.password_manager_registry)
            entries = canonical['roles'].get(role, [])
            if not role:
                return None
            validate_role_entries(role, entries)
            if not entries:
                raise SetupPrerequisiteError(f'{role}: configure an account in unattended authentication setup.')
            if not canonical['password_manager']['manager']:
                raise SetupPrerequisiteError('Configure the shared password manager before unattended authentication.')
            profiles = parse_profiles({'password_manager': canonical['password_manager'],
                                       'roles': {role: entries}}, (role,),
                                      registry=self.password_manager_registry)
            if type(index) is not int or index < 0 or index >= len(profiles.for_role(role)):
                raise ValueError
            source = profiles.for_role(role)[index]
        except SetupPrerequisiteError:
            raise
        except (ValueError, TypeError):
            raise ContractError('The selected unattended login descriptor is incomplete or invalid.') from None

        async def retrieve(selected_role, selected_index, kind, **options):
            helper = {'username': helpers.get_username, 'password': helpers.get_password,
                      'totp': helpers.get_totp, 'login': helpers.get_login_credentials}[kind]
            interaction = None if self.interaction is None else self.interaction.fork()
            try:
                from api_agent.providers.progress import report_timeout
                async with report_timeout('Password-manager ' + kind, None) as budget:
                    def call_helper():
                        # A queued worker must not restart the captured budget.
                        remaining = budget.remaining
                        timeout = helpers.DEFAULT_TIMEOUT if remaining is None else min(helpers.DEFAULT_TIMEOUT, remaining)
                        return helper(source, timeout=timeout,
                                      interaction=interaction, recovery_timeout=remaining, **options)
                    task = asyncio.create_task(asyncio.to_thread(call_helper))
                    self._credential_tasks.add(task)
                    task.add_done_callback(self._credential_tasks.discard)
                    try:
                        value = await asyncio.shield(task)
                        if type(value) is str and value:
                            self.emissions.register_secret(value)
                        elif kind == 'login':
                            for secret in value.secret_values():
                                self.emissions.register_secret(secret)
                        return value
                    except asyncio.CancelledError:
                        if interaction is not None:
                            interaction.cancel()
                        from api_agent.runtime.tasks import settle
                        await settle(asyncio.gather(task, return_exceptions=True))
                        raise
            except helpers.CredentialCapabilityUnsupportedError:
                return None
            except (helpers.CredentialLoginRequiredError, helpers.CredentialRecoveryError) as error:
                raise UnattendedCredentialError(str(error),
                    index_source=self.unattended_index_source) from None
            except helpers.CredentialError:
                raise UnattendedCredentialError(
                    "Password-manager " + kind + " retrieval is unavailable. Sign in to the configured CLI and check the login entry.",
                    index_source=self.unattended_index_source) from None
            finally:
                if interaction is not None:
                    interaction.close()
        async def retrieve_login(selected_role, selected_index, **options):
            return await retrieve(selected_role, selected_index, 'login', **options)
        return ScopedLLMCredentials(role, ScopedRoleCredentials((role,), index, retrieve,
                                                               retrieve_login=retrieve_login))

    async def _prepare_setup(self):
        await self._release_retained()
        location = self.config.get('LLM_PROVIDER')
        if location is None or location == '':
            raise SetupPrerequisiteError('Select LLM_PROVIDER before requesting LLM setup.')
        if self.provider is None:
            self._provider_config = deepcopy(self.config)
            try:
                services = {'owner_root': self.owner_root, 'emissions': self.emissions}
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
            async def complete(request, *, auth):
                if type(auth) is str and auth:
                    self.emissions.register_secret(auth)
                return await implementation.acomplete(request, auth=auth)
            self._shared_setup = SharedSetupOwner(self._operation_id, identity, make_owner,
                                                 credentials=credentials, describe=getattr(implementation, 'adescribe', None),
                                                 complete=complete if callable(getattr(implementation, 'acomplete', None)) else None,
                                                 validate_request=lambda request: self._validate_request(implementation, request))

    async def _capture_quota(self, phase):
        async with self._quota_lock:
            if phase == 'before' and self._execution_finished:
                return
            if getattr(self, '_quota_' + phase):
                return
            setattr(self, '_quota_' + phase, True)
            method = getattr(self.provider.implementation, 'asnapshot', None)
            if not callable(method):
                return
            from .quota_events import validate_snapshot, publish
            from api_agent.telemetry.usage import QuotaReadError
            try:
                from api_agent.providers.progress import component_timeout
                prepare = getattr(self.provider.implementation, 'aprepare_accounting', None)
                if phase == 'before' and callable(prepare):
                    # Execution-only native preparation is distinct from a short
                    # measurement. Parent operation deadlines still cap this scope.
                    async with component_timeout('Provider accounting preparation', 330):
                        await self.owner.call(prepare)
                async with component_timeout('Provider quota snapshot', 3):
                    snapshot = await self.owner.call(method)
                if snapshot is not None:
                    validate_snapshot(snapshot)
            except Exception:
                snapshot = QuotaReadError('request')
            if snapshot is not None:
                try:
                    if self._quota_observer is None:
                        publish(phase, snapshot)
                    else:
                        self._quota_observer(phase, snapshot)
                except Exception:
                    # Reporting failure never changes authentication/test outcomes.
                    pass

    def _validate_request(self, implementation, request):
        from api_agent.providers.cache_policy import validate_provider_request
        from api_agent.providers.selection import ModelSelectionPolicy
        ModelSelectionPolicy(self.config).require_allowed(request.selected_llm)
        validate_provider_request(implementation, request)

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
        await self._capture_quota('before')

    async def _describe(self):
        async with self._setup_lock:
            await self._prepare_setup()
        from api_agent.providers.selection import ModelSelectionPolicy
        await self._ensure()
        catalog = await self._shared_setup.describe(selected_identity=self._selected_identity())
        return ModelSelectionPolicy(self.config).filter(catalog)

    async def _preflight(self, request):
        from api_agent.providers.cache_policy import configured_request
        from api_agent.providers.selection import ModelSelectionPolicy
        ModelSelectionPolicy(self.config).require_allowed(request.selected_llm)
        request = configured_request(request, self.config.get('LLM_CACHE_POLICY'),
                                     reasoning_effort=self.config.get('LLM_REASONING_EFFORT'))
        async with self._setup_lock:
            await self._prepare_setup()
        self._validate_request(self.provider.implementation, request)
        return request

    async def _complete(self, request):
        request = await self._preflight(request)
        await self._ensure()
        return await self._shared_setup.complete(request, selected_identity=self._selected_identity())

    def complete(self, request):
        if self.closed:
            raise ContractError('Integration provider setup is closed.')
        request = _configured_request(request, self.config)
        from .inference_usage import publish
        call_id = publish(request)
        result = None
        try:
            result = self._run_private(self._complete(request))
            from api_agent.providers.costs import quote_cost
            publish(request, result, call_id=call_id, quote=self.emissions.content_export(quote_cost(self.provider.implementation, request, result)))
            return result
        except SetupPrerequisiteError as error:
            publish(request, result, call_id=call_id, failed=True)
            if not self.skip_unavailable:
                raise
            pytest.skip(str(error))
        except BaseException as error:
            publish(request, result, call_id=call_id, failed=True, error=error)
            raise
        finally:
            from .inference_content import export_content, publish_content
            publish_content(export_content(self.emissions, request, result), call_id)

    def describe(self):
        if self.closed:
            raise ContractError('Integration provider setup is closed.')
        try:
            return self._run_private(self._describe())
        except SetupPrerequisiteError as error:
            if not self.skip_unavailable:
                raise
            pytest.skip(str(error))

    def ensure_ready(self):
        if self.closed:
            raise ContractError('Integration provider setup is closed.')
        try:
            self._run_private(self._ensure())
        except SetupPrerequisiteError as error:
            if not self.skip_unavailable:
                raise
            pytest.skip(str(error))

    def _run_private(self, awaitable):
        """Preserve stack locations/errors without exporting provider frame locals."""
        try:
            return self.loop.run_until_complete(awaitable)
        except BaseException as error:
            pending, seen = [error], set()
            while pending:
                item = pending.pop()
                if id(item) in seen:
                    continue
                seen.add(id(item))
                traceback.clear_frames(item.__traceback__)
                if isinstance(item, BaseExceptionGroup):
                    pending.extend(item.exceptions)
                pending.extend(value for value in (item.__cause__, item.__context__)
                               if value is not None)
            raise

    async def _finish_execution(self):
        self._execution_finished = True
        if self.interaction is not None:
            self.interaction.cancel()
        if self._shared_setup is not None:
            await self._shared_setup.settle_execution()
        if self._credential_tasks:
            # Never cancel to_thread tasks: cancellation would acknowledge the
            # wrapper while the actual CLI/helper keeps running.
            from api_agent.providers.progress import report_timeout
            async with report_timeout('Credential helper settlement', 5) as budget:
                _, pending = await asyncio.wait(self._credential_tasks, timeout=budget.remaining)
            if pending:
                raise ContractError('Credential helpers remain active; final accounting is pending.')
        if self._quota_before and not self._quota_after and self.owner is not None:
            await self._capture_quota('after')

    async def _close(self):
        try:
            await self._finish_execution()
            await self._release_retained()
            if self._shared_setup is not None:
                await self._shared_setup.aclose()
            elif self.owner is not None:
                await self.owner.aclose()
            if self.provider is not None:
                await self.provider.aclose()
        finally:
            try:
                if self._credential_tasks:
                    from api_agent.providers.control import finish_cleanup
                    async def drain():
                        await asyncio.gather(*self._credential_tasks, return_exceptions=True)
                        self._credential_tasks.clear()
                    await finish_cleanup(drain())
            finally:
                if self.interaction is not None:
                    self.interaction.close()

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


class RemoteIntegrationLLM:
    """Only readiness, bounded catalog metadata and lease disposal cross the backend boundary."""
    timeout = 300
    def __init__(self, descriptor, config=None):
        from api_agent.providers.setup_transport import SetupClient
        self.config = dict(config or {})
        self.client = SetupClient(descriptor, timeout=self.timeout)
        self.loop = asyncio.new_event_loop()
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1,
            thread_name_prefix='api-agent-setup-client'))

    def complete(self, request):
        request = _configured_request(request, self.config)
        from .inference_usage import publish
        call_id = publish(request)
        result = None
        try:
            result = self._request(lambda: self.client.complete(request))
            publish(request, result, call_id=call_id, quote=getattr(self.client, 'cost_quote', None))
            return result
        except BaseException as error:
            publish(request, result, call_id=call_id, failed=True, error=error)
            raise
        finally:
            from .inference_content import publish_content
            publish_content(getattr(self.client, 'evidence', ()), call_id)

    def describe(self):
        return self._request(self.client.describe)

    def ensure_ready(self):
        return self._request(self.client.ensure_ready)

    def _request(self, method):
        from api_agent.providers.progress import report_timeout
        from api_agent.runtime.tasks import offload
        async def ready():
            async with report_timeout('Shared provider setup', self.timeout):
                return await offload(method)
        try:
            return self.loop.run_until_complete(ready())
        except SetupPrerequisiteError as error:
            pytest.skip(str(error))

    def close(self):
        if not self.loop.is_closed():
            from api_agent.runtime.tasks import offload
            self.loop.run_until_complete(offload(self.client.close))
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()


def _worker_interaction(data):
    if 'interaction' not in data:
        return None
    from api_agent.auth.interaction_transport import InteractionClient
    return InteractionClient(data['interaction'])


@pytest.fixture(scope='session')
def llm_selection(llm_provider):
    from api_agent.providers.selection import ModelSelectionPolicy
    data = _read_snapshot()
    return ModelSelectionPolicy(data['config']).select_default(llm_provider.describe())


@pytest.fixture(scope='session')
def llm_provider():
    """Requesting the fixture is inert; ensure_ready is the authentication gate."""
    data = _read_snapshot()
    if 'setup_backend' in data:
        _worker_manager_registry(data)
        provider = RemoteIntegrationLLM(data['setup_backend'], data['config'])
    else:
        provider = IntegrationLLM(data['config'], data['owner_root'],
                                  unattended_index_source=data.get('unattended_index_source'),
                                  password_manager_registry=_worker_manager_registry(data),
                                  interaction=_worker_interaction(data))
    try:
        yield provider
    finally:
        if isinstance(provider, IntegrationLLM) and os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
            provider.finish_run()
        else:
            provider.close()


class UIBrowserInspectionRun:
    """Retain a UI browser's creating loop in the existing inspection protocol."""
    def __init__(self, loop, inspection):
        self.loop, self.inspection, self.closed = loop, inspection, False
        self._shutdown = None

    async def _shutdown_loop(self):
        await self.loop.shutdown_asyncgens()
        await self.loop.shutdown_default_executor()

    async def dispose_inspection(self):
        """Retain native/background work and loop shutdown until confirmed done."""
        from api_agent.providers.control import CleanupPendingError
        from api_agent.providers.progress import report_timeout_sync
        with report_timeout_sync('Studio inspection cleanup', 2):
            if self.inspection is not None:
                await self.inspection.aclose()
            tasks = {task for task in asyncio.all_tasks(self.loop)
                     if task is not asyncio.current_task() and task is not self._shutdown}
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=1)
                if pending:
                    raise CleanupPendingError((self,), self.dispose_inspection)
            if self._shutdown is None:
                self._shutdown = self.loop.create_task(self._shutdown_loop())
            done, _ = await asyncio.wait({self._shutdown}, timeout=1)
            if not done:
                raise CleanupPendingError((self,), self.dispose_inspection)
            self._shutdown.result()

    def close(self):
        if not self.closed:
            if self.inspection is not None:
                self.loop.run_until_complete(self.inspection.wait_closed())
            self.loop.run_until_complete(self.dispose_inspection())
            self.loop.close()
            self.closed = True


def finish_ui_browser_inspection(loop, inspection):
    """Transfer allocated DEBUG UI browsers to launch's end_inspection owner.

    Empty and non-debug runs close their loop immediately. Without launch's
    inspection channel, attended debug windows retain the same manual-close
    behavior as provider inspection. The caller must already close BrowserOwner
    so its resources have transferred to the supplied InspectionOwner.
    """
    owner = UIBrowserInspectionRun(loop, inspection)
    if inspection is not None and inspection.sessions and os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
        _inspection_providers.append(owner)
    else:
        owner.close()
    return owner


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
                    dispose = getattr(provider, 'dispose_inspection', inspector.aclose)
                    provider.loop.run_until_complete(dispose())
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
    def __init__(self, values, owner_root=None):
        self.values = dict(values)
        self.owner_root = owner_root

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
    snapshot = _read_snapshot()
    return IntegrationRequirements(snapshot['config'], snapshot['owner_root'])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Put recovery guidance before the internal stack in every evidence sink."""
    report = yield
    if call.excinfo is not None and isinstance(call.excinfo.value, UnattendedCredentialError):
        guidance = str(call.excinfo.value).partition('\n\n')[0]
        report.longrepr = guidance + '\n\n' + str(report.longrepr)
    return report


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_pack_api: selected integration requires pack API/identity preflight")
