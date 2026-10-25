"""Selected local demonstration policy and borrowed API tool access."""
from api_agent.providers.contracts import ContractError
from api_agent.workflows.local_application import LocalChatApplication


def selected_application(definition, *, config=None):
    """Admit the useful demo explicitly; preserve the one-call chat policy."""
    if getattr(definition, 'tool_usages', ()):
        from api_agent.workflows.demo_application import UsefulDemoApplication
        return UsefulDemoApplication(definition, config=config)
    return LocalChatApplication(definition, config=config)


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
