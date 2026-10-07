"""Load explicitly configured factories and their configuration declarations."""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
import importlib.util
import inspect
from pathlib import Path
import sys
from types import MappingProxyType
import uuid


class ProviderError(ValueError):
    pass


@dataclass
class LoadedProvider:
    implementation: object = field(repr=False)
    module_name: str = field(repr=False)
    closed: bool = False

    async def astart(self):
        if self.closed:
            raise ProviderError('provider_closed')
        hook = getattr(self.implementation, 'astart', None)
        if hook is not None:
            await hook()

    async def setup_routes(self, registrar, services):
        hook = getattr(self.implementation, 'setup_routes', None)
        if hook is not None:
            result = hook(registrar, services)
            if inspect.isawaitable(result):
                await result

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            hook = getattr(self.implementation, 'aclose', None)
            if hook is not None:
                await hook()
        finally:
            sys.modules.pop(self.module_name, None)


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


def load_provider(location, config, services):
    owner_root = services.get('owner_root') if isinstance(services, Mapping) else None
    path, factory_name = provider_location(location, owner_root=owner_root)
    name = '_api_agent_provider_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ProviderError('provider_location_invalid')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            raise ProviderError('provider_factory_invalid')
        adapter = factory(MappingProxyType(dict(config)), services)
        if inspect.isawaitable(adapter):
            if inspect.iscoroutine(adapter):
                adapter.close()
            raise ProviderError('provider_factory_must_be_synchronous')
        if adapter is None or getattr(adapter, 'version', None) != 1 or type(adapter.version) is not int:
            raise ProviderError('provider_version_invalid')
        for capability in ('authenticate', 'astart', 'aclose', 'setup_routes'):
            hook = getattr(adapter, capability, None)
            if hook is not None and not callable(hook):
                raise ProviderError('provider_capability_invalid')
        return LoadedProvider(adapter, name)
    except BaseException as error:
        sys.modules.pop(name, None)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ProviderError('provider_initialization_failed') from None


def bootstrap_schema(base_schema, argv, environ, dotenv, overrides=None, *,
                     owner_root=None, configuration_paths=()):
    """Select the provider before parsing its additional CLI/config fields."""
    from api_agent.config.options import parse_options
    from api_agent.config.declarations import UNSET
    declaration = base_schema.fields.get('LLM_PROVIDER')
    selected = declaration.default if declaration is not None and declaration.default is not UNSET else None
    selected = dotenv.get('LLM_PROVIDER', selected)
    selected = environ.get('LLM_PROVIDER', selected)
    selected = parse_options(base_schema, argv).values.get('LLM_PROVIDER', selected)
    selected = (overrides or {}).get('LLM_PROVIDER', selected)
    schema = provider_schema(selected, base_schema, owner_root=owner_root) if selected else base_schema
    for path in configuration_paths:
        schema = contribution_schema(path, schema)
    return schema
