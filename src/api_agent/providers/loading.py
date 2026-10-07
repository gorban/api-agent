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


class _StreamView:
    def __init__(self, implementation):
        self._implementation = implementation

    def __getattr__(self, name):
        return getattr(self._implementation, name)

    def astream(self, request, *, auth=None):
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
        return result


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
        return _StreamView(self.implementation) if operation == 'stream' else self.implementation

    async def astart(self):
        self._context()
        if self.closed:
            raise ProviderError('provider_closed')
        hook = getattr(self.implementation, 'astart', None)
        if hook is not None:
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
    path = path.resolve()
    if not path.is_file() or path.suffix != '.py':
        raise ProviderError('provider_location_invalid')
    return path, factory


def contribution_schema(declaration, base_schema):
    """Expose compatible provider defaults as effective pack declarations."""
    from api_agent.config.declarations import ConfigSchema, load_declarations, compose_schema
    contributed = load_declarations(declaration, base_schema=base_schema, source='pack_default')
    fields = {name: replace(value, override=True) if name in base_schema.fields else value
              for name, value in contributed.fields.items()}
    return compose_schema(base_schema, ConfigSchema(fields))


def provider_schema(location, base_schema, *, owner_root=None):
    """Resolve contributed declarations before creating executable adapters."""
    path, _ = provider_location(location, owner_root=owner_root)
    declaration = path.parent / 'cfg/config.toml'
    if not declaration.exists():
        return base_schema
    if not declaration.resolve().is_relative_to(path.parent):
        raise ProviderError('provider_configuration_escape')
    return contribution_schema(declaration, base_schema)


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
        args = () if operation in ('describe', 'usage', 'launch') else (object(),)
        kwargs = {} if operation == 'launch' else {'auth': None}
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
        for capability in ('authenticate', 'astart', 'aclose', 'setup_routes'):
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
    owners = {name: owner_root for name in ('LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER', 'ACCESS_PROVIDER')}
    for path in configuration_paths:
        previous = set(schema.fields)
        schema = contribution_schema(path, schema)
        for name in set(schema.fields) - previous:
            if name.endswith('_PROVIDER'):
                owners[name] = owner_root
    processed = {}
    paths = set()
    while True:
        pending = False
        options = parse_options(schema, argv).values
        for name in ('LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER', 'ACCESS_PROVIDER', 'UPSTREAM_API_PROVIDER'):
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
            path, factory = provider_location(selected, owner_root=owners.get(name, owner_root))
            if (path, factory) in paths:
                raise ProviderError('provider_selection_cycle')
            paths.add((path, factory))
            processed[name] = selected
            previous = dict(schema.fields)
            schema = provider_schema(str(path) + ':' + factory, schema)
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
            options = parse_options(schema, argv).values
            pending = True
        if not pending:
            return schema
