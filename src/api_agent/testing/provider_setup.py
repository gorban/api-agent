"""Lazy provider setup for launch-owned integration workers."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import inspect
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

_SNAPSHOT_BYTES = 1048576
_EXPANDED_PROFILE_BYTES = 16 * 1048576


def _pack_profile_rate_cards(profiles):
    """Intern repeated pricing receipts without changing case configurations."""
    from .test_profiles import validate_snapshot_profiles
    validate_snapshot_profiles(profiles)
    packed, cards, lookup = {}, [], {}
    for node, row in profiles.items():
        row = deepcopy(row)
        if '_COST_RATE_CARD' in row['config']:
            card = row['config'].pop('_COST_RATE_CARD')
            key = json.dumps(card, default=str, allow_nan=False)
            if key not in lookup:
                lookup[key] = len(cards)
                cards.append(card)
            row['rate_card_index'] = lookup[key]
        packed[node] = row
    return packed, cards


def _unpack_profile_rate_cards(profiles, cards):
    """Bound expansion before reconstructing independent per-case receipts."""
    if type(profiles) is not dict or type(cards) is not list:
        raise ValueError('Invalid integration profile rate cards')
    sizes = [len(json.dumps(card, default=str, allow_nan=False).encode()) for card in cards]
    expanded = 0
    for row in profiles.values():
        if type(row) is not dict:
            raise ValueError('Invalid integration profile entry')
        if 'rate_card_index' in row:
            index = row['rate_card_index']
            if (type(index) is not int or not 0 <= index < len(cards)
                    or type(row.get('config')) is not dict or '_COST_RATE_CARD' in row['config']):
                raise ValueError('Invalid integration profile rate card reference')
            expanded += sizes[index]
            if expanded > _EXPANDED_PROFILE_BYTES:
                raise ValueError('Integration profile rate cards exceed expansion budget')
    for row in profiles.values():
        if 'rate_card_index' in row:
            row['config']['_COST_RATE_CARD'] = deepcopy(cards[row.pop('rate_card_index')])
    return profiles


def _configured_request(request, config):
    """Freeze effective options before transport and accounting see the call."""
    from api_agent.providers.cache_policy import configured_request
    return configured_request(request, config.get('LLM_CACHE_POLICY'),
                              reasoning_effort=config.get('LLM_REASONING_EFFORT'))


def write_launch_snapshot(directory, context, config, *, unattended_index_source=None, setup_backend=None,
                          password_manager_registry=None, interaction_descriptor=None, tool_plan=None, tool_cases=None, test_profiles=None):
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
    if test_profiles is not None:
        data['test_profiles'], data['test_profile_rate_cards'] = _pack_profile_rate_cards(test_profiles)
        # Apply the reader's expansion budget before publishing a worker context.
        try:
            _unpack_profile_rate_cards(deepcopy(data['test_profiles']), data['test_profile_rate_cards'])
        except ValueError:
            raise ContractError('Integration configuration is too large.') from None
    if tool_plan is not None:
        from .tool_runtime import write_tool_packet
        from api_agent.runtime.inventory import inventory_authority
        data['inventory_authority'] = inventory_authority(context, tool_plan)
        data['tool_plan'] = write_tool_packet(directory, tool_plan, context=context)
        data['tool_cases'] = dict(tool_cases or {})
        data['base_root'], data['pack_root'] = str(context.base_root), str(context.pack_root)
    payload = json.dumps(data, default=str, allow_nan=False).encode()
    if len(payload) > _SNAPSHOT_BYTES:
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
            raw = stream.read(_SNAPSHOT_BYTES + 1)
        if len(raw) > _SNAPSHOT_BYTES:
            raise ValueError
        data = json.loads(raw)
        required = {'version', 'owner_root', 'config', 'password_manager_definitions'}
        if (not required <= set(data) or set(data) - required - {'unattended_index_source', 'setup_backend', 'password_manager_pack_root', 'interaction', 'tool_plan', 'tool_cases', 'base_root', 'pack_root', 'inventory_authority', 'test_profiles', 'test_profile_rate_cards'}
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
        if 'tool_plan' in data:
            if (type(data.get('inventory_authority')) is not list
                    or type(data.get('tool_cases')) is not dict
                    or any(type(key) is not str or type(value) is not str
                           for key, value in data['tool_cases'].items())
                    or any(type(data.get(key)) is not str or not Path(data[key]).is_absolute()
                           for key in ('base_root', 'pack_root'))):
                raise ValueError
        elif any(key in data for key in ('tool_cases', 'base_root', 'pack_root', 'inventory_authority')):
            raise ValueError
        if 'test_profile_rate_cards' in data:
            if 'test_profiles' not in data:
                raise ValueError
            data['test_profiles'] = _unpack_profile_rate_cards(data['test_profiles'], data.pop('test_profile_rate_cards'))
        if 'test_profiles' in data:
            from .test_profiles import validate_snapshot_profiles
            validate_snapshot_profiles(data['test_profiles'])
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
        from api_agent.auth.bindings import bind_role_credentials
        from api_agent.providers.sessions import ScopedLLMCredentials
        role = self.config.get('LLM_CREDENTIAL_ROLE')
        if not role:
            return None
        reader = bind_role_credentials(self.config, (role,), index,
            registry=self.password_manager_registry, emissions=self.emissions,
            pending_tasks=self._credential_tasks, interaction_owner=self.interaction,
            index_source=self.unattended_index_source)
        return ScopedLLMCredentials(role, reader)

    async def _prepare_setup(self):
        await self._release_retained()
        location = self.config.get('LLM_PROVIDER')
        if location is None or location == '':
            raise SetupPrerequisiteError('Select LLM_PROVIDER before requesting LLM setup.')
        if self.provider is None:
            self._provider_config = deepcopy(self.config)
            try:
                services = {'owner_root': self.owner_root, 'emissions': self.emissions,
                            'interaction': self.interaction}
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
                                                 captures_effective_payload=getattr(implementation, 'captures_effective_payload', False) is True,
                                                 validate_request=lambda request: self._validate_request(implementation, request),
                                                 catalog_ttl=self._provider_config.get('LLM_CATALOG_CACHE_TTL_SECONDS', 3600),
                                                 max_attempts=self._provider_config.get('LLM_MAX_ATTEMPTS', 5),
                                                 inference_timeout=getattr(implementation, 'inference_timeout', None))

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
        call_id = publish(request) or str(uuid4())
        from api_agent.workflows.inference_capture import collect_inference, capture_rows
        result = None
        collector = None
        from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger, attach_transport_attempts
        from api_agent.providers.costs import quote_cost
        ledger = TransportAttemptLedger(self._provider_config.get('LLM_MAX_ATTEMPTS', 5),
            quote=lambda result: self.emissions.content_export(quote_cost(self.provider.implementation, request, result)))
        async def captured_complete():
            nonlocal collector
            with bind_transport_attempt_ledger(ledger), collect_inference(self.emissions, call_id) as collector:
                return await self._complete(request)
        try:
            result = self._run_private(captured_complete())
            result = attach_transport_attempts(result, ledger.snapshot())
            from api_agent.providers.costs import quote_cost
            publish(request, result, call_id=call_id, quote=self.emissions.content_export(quote_cost(self.provider.implementation, request, result)))
            return result
        except SetupPrerequisiteError as error:
            attach_transport_attempts(error, ledger.snapshot())
            publish(request, result, call_id=call_id, failed=True, error=error)
            if not self.skip_unavailable:
                raise
            pytest.skip(str(error))
        except BaseException as error:
            attach_transport_attempts(error, ledger.snapshot())
            terminal_error = error
            if collector is not None:
                from api_agent.workflows.inference_capture import retain_response_failure
                try:
                    retain_response_failure(error, collector['dispatch']['response'], collector['dispatch']['usage'])
                except ContractError as conflict:
                    terminal_error = conflict
                    attach_transport_attempts(conflict, error.transport_attempts)
            publish(request, result, call_id=call_id, failed=True, error=terminal_error)
            if terminal_error is not error:
                raise terminal_error from None
            raise
        finally:
            from .inference_content import publish_content
            rows = () if collector is None else collector['rows']
            capture_rows(rows, call_id)
            publish_content(rows, call_id)

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
        from api_agent.providers.progress import component_timeout
        from inspect import CORO_CREATED, CORO_CLOSED, getcoroutinestate
        started = False

        async def bounded():
            nonlocal started
            try:
                async with component_timeout('Integration provider operation'):
                    started = True
                    return await awaitable
            finally:
                if (not started and asyncio.iscoroutine(awaitable)
                        and getcoroutinestate(awaitable) == CORO_CREATED):
                    awaitable.close()

        operation = bounded()
        try:
            return self.loop.run_until_complete(operation)
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
        finally:
            submitted = any(task.get_coro() is operation
                            for task in asyncio.all_tasks(self.loop))
            if not submitted and getcoroutinestate(operation) == CORO_CREATED:
                operation.close()
            if (not started and getcoroutinestate(operation) == CORO_CLOSED
                    and asyncio.iscoroutine(awaitable)
                    and getcoroutinestate(awaitable) == CORO_CREATED):
                awaitable.close()

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
        from api_agent.providers.setup_transport import SetupClient, _timeout
        self.timeout = _timeout((config or {}).get('LLM_SETUP_REQUEST_TIMEOUT_SECONDS', self.timeout))
        self.config = dict(config or {})
        self.client = SetupClient(descriptor, timeout=self.timeout)
        self.loop = asyncio.new_event_loop()
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1,
            thread_name_prefix='api-agent-setup-client'))

    def complete(self, request):
        request = _configured_request(request, self.config)
        from .inference_usage import publish
        call_id = publish(request) or str(uuid4())
        result = None
        packet = dict(transport_attempts=None, cost_quote=None, response_evidence=None, evidence=[])

        def invoke():
            # Snapshot all per-call facts before another caller can reset them.
            with self.client._lock:
                try:
                    return self.client.complete(request)
                finally:
                    for name in packet:
                        packet[name] = deepcopy(getattr(self.client, name, packet[name]))
        try:
            result = self._request(invoke)
            publish(request, result, call_id=call_id, quote=packet['cost_quote'])
            return result
        except BaseException as error:
            from api_agent.providers.generation_attempts import attach_transport_attempts, validate_transport_attempts
            snapshot = getattr(error, 'transport_attempts', None)
            if snapshot is None:
                snapshot = packet['transport_attempts'] or {'version': 1, 'complete': False, 'attempts': []}
                snapshot = validate_transport_attempts({**snapshot, 'complete': False})
            attach_transport_attempts(error, snapshot)
            from api_agent.workflows.inference_capture import retain_response_failure
            terminal_error = error
            try:
                retain_response_failure(error, packet['response_evidence'])
            except ContractError as conflict:
                terminal_error = conflict
                attach_transport_attempts(conflict, snapshot)
            publish(request, result, call_id=call_id, failed=True, error=terminal_error)
            if terminal_error is not error:
                raise terminal_error from None
            raise
        finally:
            from .inference_content import publish_content
            from api_agent.workflows.inference_capture import capture_rows
            rows = packet['evidence']
            capture_rows(rows, call_id)
            publish_content(rows, call_id)

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
        if self.loop.is_closed():
            raise RuntimeError('Event loop is closed')
        work = ready()
        try:
            return self.loop.run_until_complete(work)
        except SetupPrerequisiteError as error:
            pytest.skip(str(error))
        finally:
            if inspect.getcoroutinestate(work) == inspect.CORO_CREATED:
                work.close()

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



def _test_snapshot(request):
    data = _read_snapshot()
    if 'test_profiles' in data:
        node = getattr(request, 'node', request).nodeid
        row = data['test_profiles'].get(node)
        if row is None:
            raise ContractError('Integration case is outside the admitted test profile map.')
        data['config'] = deepcopy(row['config'])
        data['profile_provenance'] = deepcopy(row['provenance'])
        if 'setup_backend' in row:
            data['setup_backend'] = row['setup_backend']
    return data

@pytest.fixture
def llm_selection(llm_provider, request):
    from api_agent.providers.selection import ModelSelectionPolicy
    data = _test_snapshot(request)
    return ModelSelectionPolicy(data['config']).select_default(llm_provider.describe())


@pytest.fixture
def llm_provider(request):
    """Requesting the fixture is inert; ensure_ready is the authentication gate."""
    data = _test_snapshot(request)
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


@pytest.fixture
def ui_provider_loop():
    """The UI session's creating loop, retained only by its inspection owner."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        if not loop.is_closed() and not any(owner.loop is loop for owner in _inspection_providers):
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()


@pytest.fixture
def ui_provider_backend():
    """Optional async context factory binding an agent endpoint to this UI.

    Receives effective UI runtime values and yields only ``agent_url`` and
    ``access_token``. Its lifetime follows the complete prepared UI owner.
    """
    return None


@pytest.fixture
def ui_provider_session(request, ui_provider_loop, ui_provider_backend):
    """Prepare only the selected UI provider and retain its complete DEBUG owner."""
    from .ui_runtime import UIIntegrationOwner
    data = _test_snapshot(request)
    owner = UIIntegrationOwner(data, _worker_manager_registry(data), backend=ui_provider_backend)
    loop = ui_provider_loop
    try:
        yield loop.run_until_complete(owner.start())
    finally:
        try:
            try:
                retire = getattr(ui_provider_backend, 'retire_test', None)
                if retire is not None:
                    retire()
            finally:
                try:
                    loop.run_until_complete(owner.capture(getattr(request.node, '_api_agent_ui_outcome', 'failure')))
                except Exception:
                    pass
        finally:
            # The inspection owner retains even an interrupted browser transfer.
            # Its explicit disposal performs transfer, then inspection, then host.
            finish_ui_browser_inspection(loop, owner.inspection, owners=(owner,),
                                         transfers=(owner.browser_owner,))


class UIBrowserInspectionRun:
    """Retain a UI browser's creating loop in the existing inspection protocol."""
    def __init__(self, loop, inspection, owners=(), transfers=()):
        self.loop, self.inspection, self.closed = loop, inspection, False
        self._owners = list(owners)
        self._transfers = list(transfers)
        self._shutdown = None

    async def _shutdown_loop(self):
        await self.loop.shutdown_asyncgens()
        await self.loop.shutdown_default_executor()

    async def dispose_inspection(self):
        """Retain native/background work and loop shutdown until confirmed done."""
        from api_agent.providers.control import CleanupPendingError
        from api_agent.providers.progress import report_timeout_sync
        with report_timeout_sync('Studio inspection cleanup', 2):
            while self._transfers:
                await self._transfers[-1].aclose()
                self._transfers.pop()
            if self.inspection is not None:
                await self.inspection.aclose()
            while self._owners:
                await self._owners[-1].aclose()
                self._owners.pop()
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


def finish_ui_browser_inspection(loop, inspection, *, owners=(), transfers=()):
    """Transfer allocated DEBUG UI browsers to launch's end_inspection owner.

    Empty and non-debug runs close their loop immediately. Without launch's
    inspection channel, attended debug windows retain the same manual-close
    behavior as provider inspection. Callers either transfer BrowserOwner first
    or supply it in transfers; those transfers finish before inspection and
    complete runtime ownership is released.
    """
    owner = UIBrowserInspectionRun(loop, inspection, owners, transfers)
    if inspection is not None and inspection.sessions and os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
        _inspection_providers.append(owner)
    else:
        try:
            owner.close()
        except BaseException:
            if os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
                _inspection_providers.append(owner)
            raise
    return owner


def retained_inspection_manifest():
    """Describe retained sessions without transferring or clearing ownership."""
    return tuple(endpoint for provider in _inspection_providers
                 for endpoint in (tuple(session.inspection_endpoint or ''
                     for session in provider.inspection.sessions) if provider.inspection is not None
                     and provider.inspection.sessions else ('',)))


def serve_inspections(send, *, command_path=None):
    """Called after pytest has finalized all reports and session teardown."""
    providers = list(_inspection_providers)
    _inspection_providers.clear()
    if not providers:
        return
    command = (Path(command_path) if command_path is not None else
               Path(os.environ['API_AGENT_INTEGRATION_EVENTS']).parent / 'inspection-command.json')
    for provider in providers:
        sessions = () if provider.inspection is None else provider.inspection.sessions
        for session in sessions:
            send(kind='inspection_open', endpoint=session.inspection_endpoint or '')
        if not sessions:
            send(kind='inspection_open', endpoint='')
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
                          for session in (() if inspector is None else inspector.sessions))
            if attempt is not None or (not windows and provider not in pending):
                try:
                    dispose = getattr(provider, 'dispose_inspection', None) or inspector.aclose
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


@pytest.fixture
def workflow_tools(request, ui_provider_loop):
    """Use only the parent's exact-case captured tooling on its retained loop."""
    from .tool_runtime import PreparedTestTools
    owner = PreparedTestTools(_read_snapshot(), request.node.nodeid, ui_provider_loop)
    try:
        yield owner
    finally:
        finish_ui_browser_inspection(ui_provider_loop, None, owners=(owner,))


@pytest.fixture
def integration_config(request, record_property):
    """Require only settings used by this test, not unrelated suite settings."""
    snapshot = _test_snapshot(request)
    requirements = IntegrationRequirements(snapshot['config'], snapshot['owner_root'])
    requirements.provenance = snapshot.get('profile_provenance', {})
    if requirements.provenance:
        evidence = {key: {'value': requirements.values.get(key), 'source': source}
                    for key, source in requirements.provenance.items()}
        record_property('integration_test_profile', json.dumps(evidence, sort_keys=True, default=str))
        print('Integration test profile: ' + json.dumps(evidence, sort_keys=True, default=str), flush=True)
    return requirements


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Put recovery guidance before the internal stack in every evidence sink."""
    report = yield
    if report.when == 'call':
        item._api_agent_ui_outcome = 'success' if report.passed else 'failure'
    if call.excinfo is not None and isinstance(call.excinfo.value, UnattendedCredentialError):
        guidance = str(call.excinfo.value).partition('\n\n')[0]
        report.longrepr = guidance + '\n\n' + str(report.longrepr)
    return report


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_pack_api: selected integration requires pack API/identity preflight")
    config.addinivalue_line("markers", "requires_ui_session: selected integration consumes the UI provider")
    config.addinivalue_line("markers", "requires_llm: UI integration also consumes the LLM provider")


def pytest_runtest_setup(item):
    """Expose only approved effective profile controls before test setup/auth."""
    if 'API_AGENT_INTEGRATION_CONTEXT' not in os.environ:
        return
    snapshot = _test_snapshot(item)
    provenance = snapshot.get('profile_provenance', {})
    if provenance:
        evidence = {key: {'value': snapshot['config'].get(key), 'source': source}
                    for key, source in provenance.items()}
        rendered = json.dumps(evidence, sort_keys=True, default=str)
        item.user_properties.append(('integration_test_profile', rendered))
        print('Integration test profile: ' + rendered, flush=True)
