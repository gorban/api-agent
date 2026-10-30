"""Load explicitly configured factories and their configuration declarations."""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
import asyncio
import os
import importlib.util
import inspect
from pathlib import Path
import sys
from types import MappingProxyType, ModuleType
from .contracts import ProviderCapabilities, OPERATION_METHODS, LLM_FEATURES, OperationalError
import uuid


class ProviderError(ValueError):
    def __init__(self, message, *, retained_owners=()):
        super().__init__(message)
        self.retained_owners = tuple(retained_owners)


def _consume(task):
    if not task.cancelled():
        task.exception()


def _task(awaitable):
    task = asyncio.create_task(awaitable)
    task.add_done_callback(_consume)
    return task


class _ReturnedAwaitable:
    """Own a rejected scheduled factory result through cancellation and disposal."""
    def __init__(self, awaitable, module_name):
        self.awaitable = awaitable
        self.module_name = module_name
        self._task = None
        self._cancel_requested = False
        self._result_owner = None

    async def aclose(self):
        if isinstance(self.awaitable, asyncio.Future) and self.awaitable.get_loop() is not asyncio.get_running_loop():
            raise ProviderError('provider_execution_context_invalid')
        if self._task is None:
            self._task = asyncio.ensure_future(self.awaitable)
            self._task.add_done_callback(_consume)
        # Opaque delegates may shield hidden work, so only direct handles are cancelled.
        if isinstance(self.awaitable, asyncio.Future) and not self._task.done() and not self._cancel_requested:
            self._cancel_requested = True
            if not isinstance(self._task, asyncio.Task) or not self._task.cancelling():
                self._task.cancel()
        try:
            result = await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
            return
        except Exception:
            return
        if self._result_owner is None:
            self._result_owner = LoadedProvider(result, self.module_name)
        await self._result_owner.aclose()


async def _validate_model(implementation, request, auth):
    maximum = request.payload.generation.max_output_tokens
    if maximum is None and not any(message.cache_boundary is not None for message in request.payload.messages):
        return None
    describe = getattr(implementation, 'adescribe', None)
    if not callable(describe):
        if maximum is None:
            return None
        raise OperationalError('unsupported')
    from .rich_inference import resolve_catalog_selection
    return await resolve_catalog_selection(lambda: describe(auth=auth), request)


class _DeferredStream:
    def __init__(self, view, request, auth):
        self.view, self.request, self.auth = view, request, auth
        self.iterator = None
        self.closed = False
        if view._owner is not None:
            view._owner._generation_streams.add(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.view._owner is not None:
            return await self.view._owner._run_generation(self._read_next)
        return await self._read_next()

    async def _read_next(self):
        if self.closed:
            raise StopAsyncIteration
        from api_agent.workflows.inference_capture import withholding_inference_content
        with withholding_inference_content(self.view._admit()):
            return await self._read_next_admitted()

    async def _read_next_admitted(self):
        if self.closed:
            raise StopAsyncIteration
        if self.iterator is None:
            from .inference import async_request_to_wire
            await async_request_to_wire(self.request)
            from .cache_policy import validate_provider_request
            validate_provider_request(self.view._implementation, self.request)
            selection = await _validate_model(self.view._implementation, self.request, self.auth)
            self.iterator = self.view._open_stream(self.request, auth=self.auth, selection=selection)
        try:
            return await self.iterator.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise

    async def aclose(self):
        self.closed = True
        if self.iterator is not None:
            await self.iterator.aclose()
        self.iterator = None
        self.request = self.auth = None
        if self.view._owner is not None:
            self.view._owner._generation_streams.discard(self)


class _ValidatedStream:
    def __init__(self, iterator, request, selection=None):
        self.iterator, self.request, self.selection = iterator, request, selection

    def __aiter__(self):
        return self

    async def __anext__(self):
        from .llm import StreamEvent
        from .rich_inference import async_validate_result
        event = await self.iterator.__anext__()
        if isinstance(event, StreamEvent) and event.final:
            await async_validate_result(self.request, event.payload, selection=self.selection)
        return event

    async def aclose(self):
        await self.iterator.aclose()


class _StreamView:
    def __init__(self, implementation, owner=None):
        self._implementation = implementation
        self._owner = owner

    def __getattr__(self, name):
        target = getattr(self._implementation, name)
        if name not in ('asetup', 'authenticate', 'adescribe', 'astart') or not callable(target):
            return target
        async def admitted(*args, **kwargs):
            from api_agent.workflows.inference_capture import withholding_inference_content
            with withholding_inference_content(self._admit()):
                return await target(*args, **kwargs)
        return admitted

    def _admit(self):
        from .credential_guard import admit_llm_provider, CredentialProtectionError
        from api_agent.telemetry.privacy import EmissionBoundary
        from api_agent.workflows.inference_capture import active_inference_boundary, bind_boundary
        owner = self._owner
        if owner is not None:
            owner._generation_check()
            if owner.implementation is not self._implementation:
                raise CredentialProtectionError('LLM provider changed after authority admission.')
        active = active_inference_boundary()
        authority = getattr(owner, '_credential_authority', None) if owner is not None else None
        if authority is None:
            services = getattr(owner, 'services', None)
            configured = services.get('emissions') if isinstance(services, Mapping) else None
            authority = configured or getattr(self._implementation, 'emissions', None) or active or EmissionBoundary()
            if owner is not None:
                owner._credential_authority = authority
        if not isinstance(authority, EmissionBoundary):
            raise CredentialProtectionError('LLM operation credential authority is unavailable.')
        if owner is not None and hasattr(owner, '_credential_withheld'):
            if (getattr(owner, '_credential_provider', None) is not self._implementation
                    or getattr(owner, '_credential_bound_authority', None) is not authority._authority):
                raise CredentialProtectionError('LLM provider or credential authority changed after admission.')
        conflict = active is not None and active._authority is not authority._authority
        # Establish an empty collector's authority, or invalidate conflicting
        # rows before any provider can publish content.
        bind_boundary(authority)
        if owner is not None and hasattr(owner, '_credential_withheld'):
            return owner._credential_withheld or conflict
        withheld = admit_llm_provider(self._implementation, authority)
        if owner is not None:
            owner._credential_provider = self._implementation
            owner._credential_bound_authority = authority._authority
            owner._credential_withheld = withheld
        return withheld or conflict

    def astream(self, request, *, auth=None):
        if self._owner is not None:
            self._owner._generation_check()
        from .inference import request_to_wire
        request_to_wire(request, _validate_instances=False)
        return _DeferredStream(self, request, auth)

    def _open_stream(self, request, *, auth=None, selection=None):
        try:
            result = self._implementation.astream(request, auth=auth)
        except OperationalError:
            raise
        except Exception:
            raise ProviderError('provider_stream_invalid') from None
        if not callable(getattr(result, '__aiter__', None)) or not callable(getattr(result, '__anext__', None)):
            if inspect.iscoroutine(result):
                result.close()
            raise ProviderError('provider_stream_invalid')
        return _ValidatedStream(result, request, selection)

    async def acomplete(self, request, *, auth=None):
        if self._owner is not None:
            return await self._owner._run_generation(lambda: self._complete(request, auth=auth))
        return await self._complete(request, auth=auth)

    async def _complete(self, request, *, auth=None):
        from api_agent.workflows.inference_capture import withholding_inference_content
        with withholding_inference_content(self._admit()):
            return await self._complete_admitted(request, auth=auth)

    async def _complete_admitted(self, request, *, auth=None):
        from .inference import async_request_to_wire
        await async_request_to_wire(request)
        from .cache_policy import validate_provider_request
        validate_provider_request(self._implementation, request)
        selection = await _validate_model(self._implementation, request, auth)
        from .rich_inference import async_validate_result
        result = await self._implementation.acomplete(request, auth=auth)
        from .generation_attempts import snapshot_transport_result
        snapshot_transport_result(result)
        return await async_validate_result(request, result, selection=selection)


@dataclass
class LoadedProvider:
    implementation: object = field(repr=False)
    module_name: str = field(repr=False)
    closed: bool = False
    services: object = field(default=None, repr=False)
    timeout: float = field(default=5, repr=False)
    _closing: object = field(default=None, init=False, repr=False)
    disposed: bool = field(default=False, init=False)
    _pid: int = field(default_factory=os.getpid, init=False, repr=False)
    _loop: object = field(default=None, init=False, repr=False)
    _generation_tasks: set = field(default_factory=set, init=False, repr=False)
    _generation_cancelled: set = field(default_factory=set, init=False, repr=False)
    _generation_streams: set = field(default_factory=set, init=False, repr=False)

    def _context(self):
        if self._pid != os.getpid():
            raise ProviderError('provider_execution_context_invalid')
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is not None and self._loop is not loop:
            raise ProviderError('provider_execution_context_invalid')
        self._loop = loop

    def require(self, kind, operation):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')
        capabilities = getattr(self.implementation, 'capabilities', None)
        if not isinstance(capabilities, ProviderCapabilities) or capabilities.kind != kind or operation not in capabilities.operations:
            raise ProviderError('provider_operation_unavailable')
        return _StreamView(self.implementation, self) if kind == 'llm' else self.implementation

    def _generation_check(self):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')

    async def _run_generation(self, operation):
        self._generation_check()
        from .sessions import _drain
        from .control import finish_cleanup
        task = _task(operation())
        self._generation_tasks.add(task)
        try:
            result = await asyncio.shield(task)
            self._generation_check()
            return result
        finally:
            try:
                await finish_cleanup(_drain([task], self.timeout, self._generation_cancelled))
            finally:
                if task.done():
                    self._generation_tasks.discard(task)
                    self._generation_cancelled.discard(task)

    async def astart(self):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')
        hook = getattr(self.implementation, 'astart', None)
        if hook is not None:
            capabilities = getattr(self.implementation, 'capabilities', None)
            if isinstance(capabilities, ProviderCapabilities) and capabilities.kind == 'llm':
                await _StreamView(self.implementation, self).astart()
            else:
                await hook()

    async def setup_routes(self, registrar, services):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')
        hook = getattr(self.implementation, 'setup_routes', None)
        if hook is not None:
            result = hook(registrar, services)
            if inspect.isawaitable(result):
                await result

    async def _dispose(self):
        from .sessions import _drain
        try:
            await _drain(self._generation_tasks, self.timeout, self._generation_cancelled)
            for stream in tuple(self._generation_streams):
                await stream.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderError('provider_cleanup_pending', retained_owners=(self,)) from None
        hook = getattr(self.implementation, 'aclose', None)
        if hook is not None:
            try:
                await hook()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProviderError('provider_cleanup_pending', retained_owners=(self,)) from None
        release_modules(self.module_name)
        self.disposed = True

    async def aclose(self):
        self._context()
        self.closed = True
        if self._closing is None or self._closing.done() and not self.disposed:
            self._closing = _task(self._dispose())
        # Waiting is shielded by asyncio.wait; cancellation leaves the owned task alive.
        done, _ = await asyncio.wait({self._closing}, timeout=self.timeout)
        if not done:
            raise ProviderError('provider_cleanup_pending', retained_owners=(self,)) from None
        await asyncio.shield(self._closing)


def provider_location(location, *, owner_root=None):
    if not isinstance(location, str) or not location.strip():
        raise ProviderError('provider_location_required')
    path_text, separator, factory = location.rpartition(':')
    if not separator or not factory.isidentifier():
        raise ProviderError('provider_location_invalid')
    path = Path(path_text).expanduser()
    if not path.is_absolute() and owner_root is not None:
        path = Path(owner_root) / path
    lexical = path.absolute()
    path = path.resolve()
    if not path.is_file() or path.suffix != '.py':
        raise ProviderError('provider_location_invalid')
    from api_agent.config.source_reads import record_link, record_probe
    record_link(lexical, path)
    record_probe(('provider-factory', lexical), path.is_file, True)
    return path, factory


def provider_owner_root(kind, values, owner_root):
    """Resolve the same relative owner used when starting a provider."""
    if kind == 'upstream' and values.get('API_PROVIDER'):
        path, _ = provider_location(values['API_PROVIDER'], owner_root=owner_root)
        return path.parent
    return owner_root


@dataclass(frozen=True)
class _PendingContribution:
    overrides: object
    source: object
    introduced: frozenset
    provider: bool
    owner_root: object


def contribution_schema(declaration, base_schema, *, deferred=None, _provider=False, _owner_root=None):
    """Expose compatible provider defaults as effective pack declarations."""
    from api_agent.config.declarations import ConfigSchema, load_declarations, compose_schema
    prior = ConfigSchema({name: value for batch in (deferred or ())
        for name, value in batch.overrides.fields.items()})
    contributed = load_declarations(declaration, base_schema=base_schema, source='pack_default',
                                    predicate_context=prior)
    contributed = ConfigSchema({name: replace(value, contribution_origin=Path(declaration).resolve())
                                for name, value in contributed.fields.items()})
    pending = ConfigSchema({})
    if deferred is not None:
        # Keep sibling overrides together without merging distinct source files.
        pending = ConfigSchema({name: value for name, value in contributed.fields.items()
                                if value.override_if_present})
        contributed = ConfigSchema({name: value for name, value in contributed.fields.items()
                                    if not value.override_if_present})
    # An already composed schema may be supplied as a fresh session baseline.
    # Identical declarations are inert, not grants to replace another owner.
    unchanged = {name for name, declaration in contributed.fields.items()
                 if base_schema.fields.get(name) == declaration}
    if unchanged:
        contributed = ConfigSchema({name: declaration for name, declaration in contributed.fields.items()
                                    if name not in unchanged})
    context = ConfigSchema({**prior.fields, **pending.fields})
    schema = compose_schema(base_schema, contributed, predicate_context=context)
    if deferred is not None:
        # Preserve raw authority and file order for final composition.
        deferred.append(_PendingContribution(pending,
            ConfigSchema({**contributed.fields, **pending.fields}),
            frozenset(contributed.fields.keys() - base_schema.fields.keys()), _provider, _owner_root))
    return schema



def _replay_contributions(base, contributions):
    """Compose original files in order after discovered targets are known."""
    from api_agent.config.declarations import ConfigSchema, _compose_fields, _validate_values
    fields = dict(base.fields)
    waiting = set()
    promoted = {}
    for index, batch in enumerate(contributions):
        for name in batch.introduced:
            if batch.provider and name in waiting and name not in fields:
                fields[name] = batch.source.fields[name]
                promoted[name] = index
        waiting.update(batch.overrides.fields.keys() - fields.keys())
    schema = ConfigSchema(fields) if promoted else base
    captured = []
    for index, batch in enumerate(contributions):
        declarations = {name: value for name, value in batch.source.fields.items()
                        if promoted.get(name) != index}
        schema = _compose_fields(schema, ConfigSchema(declarations))
        captured.extend(schema.fields[name] for name in declarations if name in schema.fields)
        captured.extend(batch.source.fields[name] for name, origin in promoted.items() if origin == index)
    # Every file's effective values remain accountable, even if a later source
    # replaces them. Predicates use the complete, final controller constraints.
    for declaration in (*schema.fields.values(), *captured):
        _validate_values(declaration, schema.fields)
    return schema


def _canonicalize_contribution(contributions, name, declaration):
    """Retain a provider selector's owned path through ordered replay."""
    batch = contributions[-1]
    source = dict(batch.source.fields)
    source[name] = replace(source[name], default=declaration.default)
    contributions[-1] = replace(batch, source=type(batch.source)(source))

def provider_schema(location, base_schema, *, owner_root=None, deferred=None):
    """Resolve contributed declarations before creating executable adapters."""
    path, _ = provider_location(location, owner_root=owner_root)
    declaration = path.parent / 'cfg/config.toml'
    present = declaration.exists()
    from api_agent.config.source_reads import record_optional
    record_optional(declaration, present)
    if not present:
        return base_schema
    if not declaration.resolve().is_relative_to(path.parent):
        raise ProviderError('provider_configuration_escape')
    return contribution_schema(declaration, base_schema, deferred=deferred, _provider=True, _owner_root=path.parent)


def release_modules(name):
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + '.'):
            sys.modules.pop(key, None)


def validate_capabilities(adapter, expected_kind=None):
    capabilities = getattr(adapter, 'capabilities', None)
    if capabilities is None:
        return
    if not isinstance(capabilities, ProviderCapabilities) or type(capabilities.version) is not int or capabilities.version != 1:
        raise ProviderError('provider_capability_invalid')
    if type(capabilities.kind) is not str:
        raise ProviderError('provider_capability_invalid')
    methods = OPERATION_METHODS.get(capabilities.kind)
    if methods is None or expected_kind is not None and capabilities.kind != expected_kind:
        raise ProviderError('provider_capability_invalid')
    if not isinstance(capabilities.operations, tuple) or any(type(operation) is not str for operation in capabilities.operations) or len(set(capabilities.operations)) != len(capabilities.operations):
        raise ProviderError('provider_capability_invalid')
    if not isinstance(capabilities.credential_roles, tuple) or any(type(role) is not str or not role for role in capabilities.credential_roles):
        raise ProviderError('provider_capability_invalid')
    if capabilities.llm_role is not None and type(capabilities.llm_role) is not str:
        raise ProviderError('provider_capability_invalid')
    if len(set(capabilities.credential_roles)) != len(capabilities.credential_roles) or capabilities.llm_role is not None and (capabilities.kind != 'llm' or capabilities.llm_role not in capabilities.credential_roles):
        raise ProviderError('provider_capability_invalid')
    if (not isinstance(capabilities.features, tuple)
            or any(type(feature) is not str or feature not in LLM_FEATURES for feature in capabilities.features)
            or len(set(capabilities.features)) != len(capabilities.features)
            or capabilities.features and capabilities.kind != 'llm'):
        raise ProviderError('provider_capability_invalid')
    for operation in capabilities.operations:
        if operation not in methods:
            raise ProviderError('provider_capability_invalid')
        hook = getattr(adapter, methods[operation], None)
        streaming = operation == 'stream'
        if not callable(hook) or ((inspect.iscoroutinefunction(hook) or inspect.isgeneratorfunction(hook)) if streaming else not inspect.iscoroutinefunction(hook)):
            raise ProviderError('provider_capability_invalid')
        args = () if operation in ('describe', 'usage', 'launch', 'prepare_session', 'capture_evidence') else (object(),)
        kwargs = (dict(session=object(), user_index=None, credentials=None)
                  if operation == 'prepare_session' else dict(page=object(), boundary=object())
                  if operation == 'capture_evidence' else {} if operation == 'launch' else {'auth': None})
        try:
            inspect.signature(hook).bind(*args, **kwargs)
        except (TypeError, ValueError):
            raise ProviderError('provider_capability_invalid') from None


def load_provider(location, config, services, *, kind=None, _failed_owners=None):
    if _failed_owners is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ProviderError('provider_async_context_requires_aload_provider')
    owner_root = services.get('owner_root') if isinstance(services, Mapping) else None
    path, factory_name = provider_location(location, owner_root=owner_root)
    name = '_api_agent_provider_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name + '.entry', path)
    if spec is None or spec.loader is None:
        raise ProviderError('provider_location_invalid')
    module = importlib.util.module_from_spec(spec)
    package = ModuleType(name)
    package.__path__ = [str(path.parent)]
    sys.modules[name] = package
    sys.modules[name + ".entry"] = module
    adapter = None
    try:
        spec.loader.exec_module(module)
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            raise ProviderError('provider_factory_invalid')
        adapter = factory(MappingProxyType(dict(config)), services)
        if inspect.isawaitable(adapter):
            if inspect.iscoroutine(adapter):
                adapter.close()
                adapter = None
            else:
                adapter = _ReturnedAwaitable(adapter, name)
            raise ProviderError('provider_factory_must_be_synchronous')
        if adapter is None or getattr(adapter, 'version', None) != 1 or type(adapter.version) is not int:
            raise ProviderError('provider_version_invalid')
        for capability in ('authenticate', 'asetup', 'astart', 'aclose', 'setup_routes'):
            hook = getattr(adapter, capability, None)
            if hook is None:
                continue
            if not callable(hook) or inspect.isgeneratorfunction(hook) or inspect.isasyncgenfunction(hook):
                raise ProviderError('provider_capability_invalid')
            if capability != 'setup_routes' and not inspect.iscoroutinefunction(hook):
                raise ProviderError('provider_capability_invalid')
            args, kwargs = (), {}
            if capability == 'setup_routes':
                args = (object(), object())
            elif capability == 'asetup':
                kwargs = dict(interactive=True, user_index=None, credentials=None)
            elif capability == 'authenticate':
                kwargs = dict(session=object(), user_index=0, credentials=object())
                if getattr(getattr(adapter, 'capabilities', None), 'kind', kind) == 'ui':
                    kwargs['tui_mode'] = 'e2e'
            try:
                inspect.signature(hook).bind(*args, **kwargs)
            except (TypeError, ValueError):
                raise ProviderError('provider_capability_invalid') from None
        validate_capabilities(adapter, kind)
        return LoadedProvider(adapter, name, services=services)
    except BaseException as error:
        if adapter is not None and inspect.iscoroutinefunction(getattr(adapter, 'aclose', None)):
            failed_owner = LoadedProvider(adapter, name, services=services)
            if isinstance(adapter, _ReturnedAwaitable) and isinstance(adapter.awaitable, asyncio.Future):
                failed_owner._loop = adapter.awaitable.get_loop()
            if _failed_owners is not None:
                _failed_owners.append(failed_owner)
            else:
                # No disposable private loop: the caller receives an unbound owner
                # and completes its async rollback on the runtime's own loop.
                raise ProviderError('provider_cleanup_pending', retained_owners=(failed_owner,)) from None
        if _failed_owners is None or not _failed_owners:
            release_modules(name)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ProviderError('provider_initialization_failed') from None


async def aload_provider(location, config, services, *, kind=None):
    """Load within an awaited lifecycle transaction, including invalid returns."""
    failed = []
    try:
        return load_provider(location, config, services, kind=kind, _failed_owners=failed)
    except BaseException as error:
        for owner in failed:
            try:
                await owner.aclose()
            except asyncio.CancelledError as cancelled:
                cancelled.retained_owners = tuple(failed)
                raise
            except Exception:
                raise ProviderError('provider_cleanup_pending', retained_owners=failed) from None
        raise error


def bootstrap_schema(base_schema, argv, environ, dotenv, overrides=None, *,
                     owner_root=None, configuration_paths=()):
    """Select the provider before parsing its additional CLI/config fields."""
    from api_agent.config.options import parse_options
    from api_agent.config.declarations import UNSET
    schema = base_schema
    deferred = []
    selectors = ('LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER', 'ACCESS_PROVIDER', 'UPSTREAM_API_PROVIDER')
    owners = {name: owner_root for name in selectors}
    for path in configuration_paths:
        previous = set(schema.fields)
        schema = contribution_schema(path, schema, deferred=deferred, _owner_root=owner_root)
        for name in set(schema.fields) - previous:
            if name.endswith('_PROVIDER'):
                owners[name] = owner_root
    processed = {}
    discovered = {}
    discovery_owners = {}
    paths = set()
    while True:
        pending = False
        options = parse_options(schema, argv).values
        for name in selectors:
            declaration = schema.fields.get(name)
            if declaration is None:
                continue
            selected = declaration.default if declaration.default is not UNSET else None
            selected = dotenv.get(name, selected)
            selected = environ.get(name, selected)
            selected = options.get(name, selected)
            selected = (overrides or {}).get(name, selected)
            if name in processed:
                if selected != processed[name]:
                    raise ProviderError('provider_selection_cycle')
                continue
            if not selected:
                continue
            selection_owner = owners.get(name, owner_root)
            path, factory = provider_location(selected, owner_root=selection_owner)
            if (path, factory) in paths:
                raise ProviderError('provider_selection_cycle')
            paths.add((path, factory))
            processed[name] = selected
            discovered[name] = (path, factory)
            discovery_owners[name] = selection_owner
            previous = dict(schema.fields)
            schema = provider_schema(str(path) + ':' + factory, schema, deferred=deferred)
            for added, field in schema.fields.items():
                if previous.get(added) == field:
                    continue
                if added.endswith('_PROVIDER'):
                    owners[added] = path.parent
                    if field.default is not UNSET and field.default:
                        default_path, separator, default_factory = str(field.default).rpartition(':')
                        fields = dict(schema.fields)
                        if separator and not Path(default_path).is_absolute():
                            fields[added] = replace(field, default=str((path.parent / default_path).resolve()) + ':' + default_factory)
                        schema = type(schema)(fields)
                        _canonicalize_contribution(deferred, added, schema.fields[added])
            options = parse_options(schema, argv).values
            pending = True
        if not pending:
            from api_agent.config.declarations import ConfigSchema
            schema = _replay_contributions(base_schema, deferred)
            # Discovery is intentionally fixed before optional late overrides.
            # Compare *resolved* selections, including conditional defaults, and
            # do not let HELP/applicability hide a changed graph from this guard.
            from api_agent.config.resolve import _controllers, resolve_config
            # Execution-only conditions must not defeat help. Follow only the
            # selectors' transitive conditional-default dependencies, including
            # nested predicates, while parsing CLI against the complete schema.
            needed = set(selectors) & schema.fields.keys()
            remaining = list(needed)
            while remaining:
                for conditional in schema.fields[remaining.pop()].conditional_defaults:
                    for controller in _controllers(conditional['when']):
                        if controller not in needed:
                            needed.add(controller)
                            remaining.append(controller)
            selector_schema = ConfigSchema({name: replace(field, applies_when={}, pack_only=False)
                if name in selectors else field for name, field in schema.fields.items() if name in needed})
            options = parse_options(schema, argv).values
            resolved = resolve_config(selector_schema, environ=environ, dotenv=dotenv,
                overrides={**options, **(overrides or {}), 'HELP': 'False'},
                anchors={'owner_root': owner_root}, interactive=True)
            for name in selectors:
                effective = resolved.values.get(name)
                if bool(effective) != (name in processed):
                    raise ProviderError('provider_selection_cycle')
                if effective:
                    origin = schema.fields[name].contribution_origin
                    final_owner = next((batch.owner_root for batch in reversed(deferred)
                        if name in batch.source.fields and
                        batch.source.fields[name].contribution_origin == origin), owner_root)
                    # Explicit input was resolved in the discovered selector's
                    # scope; a new default retains its contributing owner's root.
                    if resolved.fields[name].source not in {'shared_default', 'pack_default'}:
                        final_owner = discovery_owners[name]
                    try:
                        final_location = provider_location(effective, owner_root=final_owner)
                    except ProviderError:
                        raise ProviderError('provider_selection_cycle') from None
                    if final_location != discovered[name]:
                        raise ProviderError('provider_selection_cycle')
            return schema


class _AdmittedToolFinder:
    """Load one captured package without exposing a source directory on sys.path."""
    def __init__(self, namespace, admission):
        self.namespace, self.admission = namespace, admission

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.namespace and not fullname.startswith(self.namespace + '.'):
            from api_agent.tooling.admission import admitted_dependency_spec
            return admitted_dependency_spec(self.admission, fullname, path)
        relative = fullname[len(self.namespace):].lstrip('.').replace('.', '/')
        sources = self.admission.sources
        module = relative + '.py'
        package = (relative + '/' if relative else '') + '__init__.py'
        if package in sources:
            selected, is_package = package, True
        elif module in sources:
            selected, is_package = module, False
        elif not relative or any(name.startswith(relative + '/') for name in sources):
            selected, is_package = None, True
        else:
            raise ImportError('Module is absent from the admitted tool closure')
        spec = importlib.util.spec_from_loader(fullname, self, is_package=is_package)
        spec.loader_state = selected
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        selected = module.__spec__.loader_state
        if selected is not None:
            module.__file__ = '<admitted-tool>/' + selected
            exec(compile(self.admission.sources[selected], module.__file__, 'exec'), module.__dict__)


class LoadedTool(LoadedProvider):
    """Reuse provider disposal ownership while retaining captured import state."""
    def __init__(self, implementation, module_name, finder, *, services):
        super().__init__(implementation, module_name, services=services)
        self.finder = finder

    async def _dispose(self):
        await super()._dispose()
        if self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)

    def require_tool(self):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')
        return self.implementation


def _validate_tool(adapter):
    if adapter is None or type(getattr(adapter, 'version', None)) is not int or adapter.version != 1:
        raise ProviderError('tool_version_invalid')
    for name, args in (('describe', ()), ('validate', (object(),)),
                       ('invoke', (object(),)), ('aclose', ())):
        hook = getattr(adapter, name, None)
        if not inspect.iscoroutinefunction(hook):
            raise ProviderError('tool_callable_invalid')
        try:
            inspect.signature(hook).bind(*args)
        except (TypeError, ValueError):
            raise ProviderError('tool_callable_invalid') from None
    start = getattr(adapter, 'astart', None)
    if start is not None:
        if not inspect.iscoroutinefunction(start):
            raise ProviderError('tool_callable_invalid')
        try:
            inspect.signature(start).bind()
        except (TypeError, ValueError):
            raise ProviderError('tool_callable_invalid') from None


async def aload_tool(admission, config, services):
    """Create a captured tool with the same retained rollback receipt as providers."""
    from api_agent.tooling.admission import verify_tool_dependencies
    import importlib
    verify_tool_dependencies(admission)
    namespace = '_api_agent_tool_' + uuid.uuid4().hex
    finder = _AdmittedToolFinder(namespace, admission)
    sys.meta_path.insert(0, finder)
    owner = None
    try:
        module_name = admission.entry.removesuffix('.py').replace('/', '.')
        if module_name == '__init__':
            module_name = ''
        elif module_name.endswith('.__init__'):
            module_name = module_name.removesuffix('.__init__')
        module = importlib.import_module(namespace + ('.' + module_name if module_name else ''))
        factory = getattr(module, admission.factory, None)
        if not callable(factory):
            raise ProviderError('tool_factory_invalid')
        try:
            inspect.signature(factory).bind(MappingProxyType(dict(config)), services)
        except (TypeError, ValueError):
            raise ProviderError('tool_factory_invalid') from None
        adapter = factory(MappingProxyType(dict(config)), services)
        if inspect.isawaitable(adapter):
            if inspect.iscoroutine(adapter):
                adapter.close()
                raise ProviderError('tool_factory_must_be_synchronous')
            adapter = _ReturnedAwaitable(adapter, namespace)
            owner = LoadedTool(adapter, namespace, finder, services=services)
            raise ProviderError('tool_factory_must_be_synchronous')
        owner = LoadedTool(adapter, namespace, finder, services=services)
        _validate_tool(adapter)
        owner.admission = admission
        return owner
    except BaseException as primary:
        if owner is not None:
            try:
                await owner.aclose()
            except BaseException as cleanup:
                primary.retained_owners = (owner,)
                primary.cleanup_error = cleanup
                raise primary
        else:
            release_modules(namespace)
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
        raise
