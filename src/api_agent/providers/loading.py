"""Load explicitly configured factories and their configuration declarations."""
from dataclasses import dataclass, field
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


def provider_location(location):
    if not isinstance(location, str) or not location.strip():
        raise ProviderError('provider_location_required')
    path_text, separator, factory = location.rpartition(':')
    if not separator or not factory.isidentifier():
        raise ProviderError('provider_location_invalid')
    path = Path(path_text).expanduser().resolve()
    if not path.is_file() or path.suffix != '.py':
        raise ProviderError('provider_location_invalid')
    return path, factory


def provider_schema(location, base_schema):
    """Resolve contributed declarations before creating executable adapters."""
    from api_agent.config.declarations import load_declarations, compose_schema
    path, _ = provider_location(location)
    declaration = path.parent / 'cfg/config.toml'
    if not declaration.exists():
        return base_schema
    if not declaration.resolve().is_relative_to(path.parent):
        raise ProviderError('provider_configuration_escape')
    return compose_schema(base_schema, load_declarations(
        declaration, base_schema=base_schema, source='provider_default'))


def load_provider(location, config, services):
    path, factory_name = provider_location(location)
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


def bootstrap_schema(base_schema, argv, environ, dotenv, overrides=None):
    """Select the provider before parsing its additional CLI/config fields."""
    from api_agent.config.options import parse_options
    selected = dotenv.get('LLM_PROVIDER')
    selected = environ.get('LLM_PROVIDER', selected)
    selected = parse_options(base_schema, argv).values.get('LLM_PROVIDER', selected)
    selected = (overrides or {}).get('LLM_PROVIDER', selected)
    return provider_schema(selected, base_schema) if selected else base_schema
