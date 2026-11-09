"""The shared chat composition admits local and production owners identically."""
import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize(('tool_plan', 'requires_api', 'timeout', 'interaction'), [
    (None, False, None, None),
    (SimpleNamespace(tools=()), True, 17, object()),
    (SimpleNamespace(tools=('primary',)), False, 17, None),
])
def test_composes_backend_runtime_tools_llm_and_observations(
        monkeypatch, tool_plan, requires_api, timeout, interaction):
    from api_agent.runtime import chat_composition

    config = SimpleNamespace(values={
        'UI_LISTENER_URL': 'https://localhost:9443',
        'LLM_REASONING_EFFORT': '',
        'LLM_MAX_OUTPUT_TOKENS': 99,
    }, tool_plan=tool_plan)
    emissions = Mock()
    runtime = SimpleNamespace(services=MappingProxyType({'base': 'service'}), emissions=emissions,
                              tracing='trace', factory_plan=(), tools_for=Mock(return_value='workflow-tools'))
    graph = SimpleNamespace(definition='definition', timeouts={} if timeout is None else {'GRAPH': timeout},
                            complete=AsyncMock(return_value='completed'))
    llm = SimpleNamespace(describe=AsyncMock(return_value=('model',)), ensure_ready=AsyncMock())
    runtime_factory = Mock(return_value=runtime)
    llm_factory = Mock(return_value=llm)
    observations = Mock(return_value='observations')
    backend = Mock()
    monkeypatch.setattr(chat_composition, 'WorkflowObservationFactory', observations)
    monkeypatch.setattr(chat_composition, 'StudioBackend', backend)
    bind_tools = Mock()
    monkeypatch.setattr(chat_composition, 'bind_local_tools', bind_tools)

    composed = chat_composition.compose_chat_runtime(
        SimpleNamespace(owner_root='owner'), config, callback_origin='https://localhost:8443',
        tls_material='tls', graph=graph, llm_factory=llm_factory, on_error='error',
        on_status='status', interaction_owner=interaction, required_kinds=('llm',),
        requires_api=requires_api, runtime_factory=runtime_factory, nonce='n' * 32,
        extra_services={'local': 'only'})

    runtime_factory.assert_called_once_with(
        composed.context, config, callback_origin='https://localhost:8443',
        tls_material='tls', required_kinds=('llm',))
    if tool_plan is not None and tool_plan.tools:
        assert runtime.tool_plan is tool_plan
        runtime.tools_for.assert_called_once_with('definition')
        bind_tools.assert_not_called()
    elif requires_api:
        bind_tools.assert_called_once_with(runtime, graph, config)
    else:
        bind_tools.assert_not_called()
    assert llm_factory.call_args.kwargs['emissions'] is emissions
    assert llm_factory.call_args.kwargs['trace'] == 'trace'
    assert llm_factory.call_args.kwargs['on_status'] == 'status'
    assert llm_factory.call_args.kwargs.get('interaction_owner') is interaction
    observations.assert_called_once_with(config, boundary=emissions, sink='status', trace='trace')
    assert graph.observations == 'observations'
    assert runtime.services == {
        'base': 'service', 'studio_backend': backend.return_value,
        'workflow_complete': composed.workflow_complete, 'workflow_application': graph, 'local': 'only',
    }
    emissions.register_secret.assert_called_once_with('n' * 32)
    assert backend.call_args.kwargs['owner'] is llm
    assert backend.call_args.kwargs['policy'].allowed.fullmatch('model') is not None
    assert backend.call_args.kwargs['timeout'] == (timeout or 120)
    assert backend.call_args.kwargs['reasoning_default'] is None
    assert backend.call_args.kwargs['max_output_tokens'] == 99

    async def verify_callbacks():
        assert await backend.call_args.kwargs['describe'](auth=None) == ('model',)
        assert await backend.call_args.kwargs['complete']('request', auth=None, context='context') == 'completed'
        assert await composed.workflow_complete('request', llm='scoped', context='scoped-context') == 'completed'
    asyncio.run(verify_callbacks())
    graph.complete.assert_awaited_with('request', llm='scoped', context='scoped-context')


@pytest.mark.parametrize('has_projection', [False, True])
def test_composition_preserves_selected_application_and_result_projection(monkeypatch, has_projection):
    from api_agent.runtime import chat_composition

    projection = (lambda result: result) if has_projection else None
    graph = SimpleNamespace(complete=AsyncMock(), project_result=projection)
    runtime = SimpleNamespace(services={}, emissions=Mock(), tracing=None)
    config = SimpleNamespace(values={}, tool_plan=None)
    monkeypatch.setattr(chat_composition, 'WorkflowObservationFactory', Mock())

    composed = chat_composition.compose_chat_runtime(
        SimpleNamespace(owner_root='owner'), config,
        callback_origin='https://localhost:8443', tls_material=None, graph=graph,
        llm_factory=Mock(return_value=object()), on_error=None,
        runtime_factory=Mock(return_value=runtime), nonce='n' * 32)

    assert composed.backend.result_projector is projection
    assert composed.runtime.services['workflow_application'] is graph
