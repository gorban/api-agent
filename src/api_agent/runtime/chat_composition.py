"""Shared composition for the existing Studio chat API."""
from dataclasses import dataclass
from types import MappingProxyType

from api_agent.api.composition import compose_runtime
from api_agent.api.studio import StudioBackend
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.contracts import ContractError
from api_agent.runtime.local_tools import bind_local_tools, requires_api
from api_agent.tooling.runtime import OwnerTools, bind_workflow_tools
from api_agent.workflows.configuration import declared_timeouts
from api_agent.workflows.observations import WorkflowObservationFactory


@dataclass(frozen=True)
class ChatRuntime:
    """The runtime owners required by the existing chat routes."""
    context: object
    runtime: object
    llm: object
    backend: object
    workflow_complete: object


def compose_chat_runtime(context, config, *, callback_origin, tls_material, graph, llm_factory,
                         on_error, on_status=None, interaction_owner=None, required_kinds=(),
                         runtime_factory=compose_runtime, nonce,
                         chat_origin=None, extra_services=None, permitted_browser_origins=None,
                         client_address_policy='loopback'):
    """Compose one admitted graph behind ``StudioBackend`` without local orchestration."""
    timeout = declared_timeouts(graph.definition, config.values).get('GRAPH')
    if timeout is None:
        raise ContractError('Chat composition requires a declared GRAPH timeout value.')
    runtime = runtime_factory(context, config, callback_origin=callback_origin,
                              tls_material=tls_material, required_kinds=required_kinds)
    # The application never holds tool handles: the resolver is installed around its
    # completions and reaches only the declared ports of the node that is running.
    tool_plan = getattr(config, 'tool_plan', None)
    tools = None
    if tool_plan is not None and tool_plan.tools:
        runtime.tool_plan = tool_plan
        tools = runtime.tools_for(graph.definition)
    elif requires_api(graph.definition):
        tools = OwnerTools(graph.definition, bind_local_tools(runtime, config))
    options = {'interaction_owner': interaction_owner} if interaction_owner is not None else {}
    llm = llm_factory(config.values, context.owner_root, emissions=runtime.emissions,
                      on_status=on_status, trace=runtime.tracing, **options)
    graph.observations = WorkflowObservationFactory(config, boundary=runtime.emissions,
                                                    sink=on_status, trace=runtime.tracing)

    async def describe(*, auth):
        return await llm.describe()

    async def workflow_complete(request, *, llm, context):
        with bind_workflow_tools(tools):
            return await graph.complete(request, llm=llm, context=context)

    async def complete(request, *, auth, context):
        return await workflow_complete(request, llm=llm, context=context)

    backend = StudioBackend(
        owner=llm, describe=describe, complete=complete,
        policy=ModelSelectionPolicy(config.values), origin=chat_origin or callback_origin, nonce=nonce,
        scrub=lambda message: 'Selected provider rejected the request.',
        result_projector=getattr(graph, 'project_result', None),
        timeout=timeout,
        on_error=on_error,
        reasoning_default=config.values.get('LLM_REASONING_EFFORT') or None,
        max_output_tokens=config.values.get('LLM_MAX_OUTPUT_TOKENS', 200000),
        permitted_origins=permitted_browser_origins,
        client_address_policy=client_address_policy,
    )
    extra = extra_services(runtime) if callable(extra_services) else extra_services
    services = {'studio_backend': backend, 'workflow_complete': workflow_complete,
                'workflow_application': graph,
                **dict(extra or {})}
    runtime.emissions.register_secret(nonce)
    runtime.services = MappingProxyType({**runtime.services, **services})
    return ChatRuntime(context, runtime, llm, backend, workflow_complete)
