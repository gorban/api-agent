"""Selected API tools remain borrowed from the operation-owned provider."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.config.resolve import ConfigResult
from api_agent.providers.contracts import ContractError
from api_agent.runtime import local_tools


@pytest.mark.parametrize('graph_id,useful', [('simple_chat', False), ('useful_api_demo', True)])
def test_selected_application_preserves_explicit_admission(monkeypatch, graph_id, useful):
    from api_agent.workflows import demo_application
    chat, demo = Mock(), Mock()
    monkeypatch.setattr(local_tools, 'LocalChatApplication', chat)
    monkeypatch.setattr(demo_application, 'UsefulDemoApplication', demo)
    definition = SimpleNamespace(graph_id=graph_id, tool_usages=('declared',) if useful else ())
    config = {'WORKFLOW_MAX_PASSES': 2}

    actual = local_tools.selected_application(definition, config=config)

    selected, unused = (demo, chat) if useful else (chat, demo)
    selected.assert_called_once_with(definition, config=config)
    unused.assert_not_called()
    assert actual is selected.return_value


def test_tool_view_delegates_and_does_not_dispose_provider():
    provider = SimpleNamespace(describe=AsyncMock(return_value='spec'),
        validate=AsyncMock(), invoke=AsyncMock(return_value='result'), aclose=AsyncMock())
    runtime = SimpleNamespace(require_provider=Mock(return_value=provider))
    view = local_tools.RuntimeToolOwner(runtime)
    prepared = object()

    async def scenario():
        assert await view.describe() == 'spec'
        await view.validate(prepared)
        assert await view.invoke(prepared) == 'result'
        await view.aclose()
    asyncio.run(scenario())

    assert all(call.args == ('api', 'invoke') for call in runtime.require_provider.call_args_list)
    provider.validate.assert_awaited_once_with(prepared)
    provider.invoke.assert_awaited_once_with(prepared)
    provider.aclose.assert_not_awaited()
    assert view.for_invocation() is view
    provider.for_invocation = Mock(return_value='invocation-view')
    assert view.for_invocation() == 'invocation-view'
    provider.for_invocation.assert_called_once_with()
    provider.for_invocation = 'invalid'
    with pytest.raises(ContractError, match='invocation view'):
        view.for_invocation()


@pytest.mark.parametrize('missing', ['describe', 'validate', 'invoke'])
def test_tool_view_rejects_incomplete_selected_provider(missing):
    provider = SimpleNamespace(describe=AsyncMock(), validate=AsyncMock(), invoke=AsyncMock())
    delattr(provider, missing)
    runtime = SimpleNamespace(require_provider=Mock(return_value=provider))
    with pytest.raises(ContractError, match='tool contract'):
        asyncio.run(local_tools.RuntimeToolOwner(runtime).describe())


def test_bind_tools_selects_one_factory_with_frozen_values():
    runtime = SimpleNamespace(factory_plan=(), services={'emissions': object()})
    graph = SimpleNamespace()
    config = ConfigResult({'API_PROVIDER': 'adapter.py:create', 'API_ENDPOINT': 'http://example.test/graphql'}, {}, (), (), ())

    local_tools.bind_local_tools(runtime, graph, config)
    config.values['API_ENDPOINT'] = 'http://changed.test/graphql'

    kind, location, values, services = runtime.factory_plan[0]
    assert (kind, location) == ('api', 'adapter.py:create')
    assert values['API_ENDPOINT'] == 'http://example.test/graphql'
    assert services is runtime.services
    assert graph.tool_owner.runtime is runtime


def test_bind_tools_requires_explicit_selected_api_provider():
    with pytest.raises(ContractError, match='API_PROVIDER'):
        local_tools.bind_local_tools(SimpleNamespace(), SimpleNamespace(),
            ConfigResult({}, {}, (), (), ()))
