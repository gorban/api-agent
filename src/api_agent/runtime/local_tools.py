"""Selected local demonstration policy and borrowed API tool access."""
from api_agent.providers.contracts import ContractError
from api_agent.workflows.configuration import declared_timeouts
from api_agent.workflows.local_application import LocalChatApplication


def selected_application(definition, *, config=None):
    """Use a declared application factory; else admit the useful demo explicitly or keep one-call chat."""
    if (getattr(definition, 'schema_source', None) is not None
            or getattr(definition, 'application_binding', None) is not None):
        return _declared_application(definition, config=config)
    if getattr(definition, 'tool_usages', ()):
        from api_agent.workflows.demo_application import UsefulDemoApplication
        return UsefulDemoApplication(definition, config=config)
    return LocalChatApplication(definition, config=config)


def requires_api(definition):
    """An application needs the selected API provider exactly when its declaration consumes tools."""
    return bool(getattr(definition, 'tool_usages', ()))


def _declared_application(definition, *, config=None):
    """Load a selected application's adapter from its immutable native closure."""
    from collections.abc import Mapping
    binding = definition.application_binding
    if binding is None:
        raise ContractError('Selected stateful workflow requires an application binding.')
    registry, source = binding
    if getattr(definition, 'schema_source', None) is not None:
        factory = definition.load_catalog(config=config).namespaces[source].get(registry)
    else:
        factory = definition.application_factory()
    if not callable(factory):
        raise ContractError('Selected application factory must be callable.')
    application = factory(definition, config=dict(config or {}))
    if (getattr(application, 'definition', None) is not definition
            or not callable(getattr(application, 'complete', None))):
        raise ContractError('Selected application factory returned an invalid application.')
    # Budgets and API need come from the declaration; an application may only agree with it.
    timeouts = getattr(application, 'timeouts', None)
    if timeouts is not None and (not isinstance(timeouts, Mapping)
                                 or dict(timeouts) != declared_timeouts(definition, config)):
        raise ContractError('Selected application timeouts disagree with the declared Timeouts.')
    needs_api = getattr(application, 'requires_api', None)
    if needs_api is not None and needs_api is not requires_api(definition):
        raise ContractError('Selected application requires_api disagrees with its declared tools.')
    return application


class RuntimeToolOwner:
    """Borrow optional typed tool methods; Runtime owns start and disposal."""
    def __init__(self, runtime):
        self._runtime = runtime

    def _selected(self):
        provider = self._runtime.require_provider('api', 'invoke')
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


def bind_local_tools(runtime, config):
    """Declare exactly the selected API factory without enabling API routes; return its borrowed owner."""
    location = config.values.get('API_PROVIDER')
    if not location:
        raise ContractError('Select API_PROVIDER for the API demonstration.')
    from types import MappingProxyType
    runtime.factory_plan = (*runtime.factory_plan,
        ('api', location, MappingProxyType(dict(config.values)), runtime.services))
    return RuntimeToolOwner(runtime)


