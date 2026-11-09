"""Selected local demonstration policy and borrowed API tool access."""
from api_agent.providers.contracts import ContractError
from api_agent.workflows.local_application import LocalChatApplication


def selected_application(definition, *, config=None):
    """Admit the useful demo explicitly; preserve the one-call chat policy."""
    if getattr(definition, 'schema_source', None) is not None:
        return _typed_application(definition, config=config)
    if getattr(definition, 'tool_usages', ()):
        from api_agent.workflows.demo_application import UsefulDemoApplication
        return UsefulDemoApplication(definition, config=config)
    return LocalChatApplication(definition, config=config)


def _typed_application(definition, *, config=None):
    """Load a selected application's adapter from its immutable native closure."""
    from collections.abc import Mapping
    binding = definition.application_binding
    if binding is None:
        raise ContractError('Selected stateful workflow requires an application binding.')
    registry, source = binding
    catalog = definition.load_catalog(config=config)
    factory = catalog.namespaces[source].get(registry)
    if not callable(factory):
        raise ContractError('Selected application factory must be callable.')
    application = factory(definition, config=dict(config or {}))
    if (getattr(application, 'definition', None) is not definition
            or not callable(getattr(application, 'complete', None))
            or not isinstance(getattr(application, 'timeouts', None), Mapping)
            or type(getattr(application, 'requires_api', None)) is not bool):
        raise ContractError('Selected application factory returned an invalid application.')
    return application


class RuntimeToolOwner:
    """Borrow optional typed tool methods; Runtime owns start and disposal."""
    def __init__(self, runtime):
        self.runtime = runtime

    def _selected(self):
        provider = self.runtime.require_provider('api', 'invoke')
        if any(not callable(getattr(provider, name, None))
               for name in ('describe', 'validate', 'invoke')):
            raise ContractError('Selected API provider does not implement the tool contract.')
        return provider

    async def describe(self):
        return await self._selected().describe()

    async def validate(self, prepared):
        return await self._selected().validate(prepared)

    async def invoke(self, prepared):
        return await self._selected().invoke(prepared)

    async def aclose(self):
        # Releasing a borrowed view cannot close the operation-owned provider.
        return None

    def for_invocation(self):
        provider = self._selected()
        factory = getattr(provider, 'for_invocation', None)
        if factory is None:
            return self
        if not callable(factory):
            raise ContractError('Selected API provider has an invalid invocation view.')
        return factory()


def bind_local_tools(runtime, graph, config):
    """Declare exactly the selected API factory without enabling API routes."""
    location = config.values.get('API_PROVIDER')
    if not location:
        raise ContractError('Select API_PROVIDER for the API demonstration.')
    from types import MappingProxyType
    runtime.factory_plan = (*runtime.factory_plan,
        ('api', location, MappingProxyType(dict(config.values)), runtime.services))
    graph.tool_owner = RuntimeToolOwner(runtime)
