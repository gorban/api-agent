"""Shared composition for the existing Studio chat API."""
from dataclasses import dataclass
from types import MappingProxyType

from api_agent.api.composition import compose_runtime
from api_agent.api.studio import StudioBackend
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.runtime.local_tools import bind_local_tools
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
                         requires_api=False, runtime_factory=compose_runtime, nonce,
                         chat_origin=None, extra_services=None, permitted_browser_origins=None,
                         client_address_policy='loopback'):
    """Compose one admitted graph behind ``StudioBackend`` without local orchestration."""
    runtime = runtime_factory(context, config, callback_origin=callback_origin,
                              tls_material=tls_material, required_kinds=required_kinds)
    tool_plan = getattr(config, 'tool_plan', None)
    if tool_plan is not None and tool_plan.tools:
        runtime.tool_plan = tool_plan
        graph.tools = runtime.tools_for(graph.definition)
    elif requires_api:
        bind_local_tools(runtime, graph, config)
    options = {'interaction_owner': interaction_owner} if interaction_owner is not None else {}
    llm = llm_factory(config.values, context.owner_root, emissions=runtime.emissions,
                      on_status=on_status, trace=runtime.tracing, **options)
    graph.observations = WorkflowObservationFactory(config, boundary=runtime.emissions,
                                                    sink=on_status, trace=runtime.tracing)

    async def describe(*, auth):
        return await llm.describe()

    async def workflow_complete(request, *, llm, context):
        return await graph.complete(request, llm=llm, context=context)

    async def complete(request, *, auth, context):
        return await workflow_complete(request, llm=llm, context=context)

    backend = StudioBackend(
        owner=llm, describe=describe, complete=complete,
        policy=ModelSelectionPolicy(config.values), origin=chat_origin or callback_origin, nonce=nonce,
        scrub=lambda message: 'Selected provider rejected the request.',
        result_projector=getattr(graph, 'project_result', None),
        timeout=getattr(graph, 'timeouts', {}).get('GRAPH') or 120,
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
