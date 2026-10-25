"""Local planning and operation preparation consume selected named tools."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.config.resolve import ConfigResult
from api_agent.runtime.local_run import _plan
from test_local_run import local


@pytest.mark.parametrize('existing', [(), (SimpleNamespace(id='shared', value='same'),)])
def test_local_plan_merges_selected_tool_contributions_once(local, monkeypatch, existing):
    from api_agent.runtime import infrastructure_contract, infrastructure_plan, inventory
    from api_agent.tooling import services
    selected = SimpleNamespace(id='shared', value='same')
    extra = SimpleNamespace(id='tool-only', value='second')
    plan = SimpleNamespace(tools=('selected',))
    local.config.tool_plan = plan
    local.owner.context.infrastructure_paths = ('declared',)
    monkeypatch.setattr(infrastructure_contract, 'load_infrastructure_contributions', Mock(return_value=existing))
    capture = Mock(return_value=(selected, extra))
    monkeypatch.setattr(services, 'selected_contributions', capture)
    planner = Mock(return_value='admitted-plan')
    monkeypatch.setattr(infrastructure_plan, 'plan_infrastructure', planner)
    schema = object()
    catalog = object()
    inventory_capture = Mock(return_value=catalog)
    monkeypatch.setattr(inventory, 'capture_inventory', inventory_capture)

    assert _plan(local.config, schema, local.owner.context, 'scope') == 'admitted-plan'

    capture.assert_called_once_with(plan)
    inventory_capture.assert_called_once_with(local.owner.context, plan)
    planner.assert_called_once_with(local.config, schema, (selected, extra), 'scope',
                                    engine_environment=None, inventory=catalog)


def test_local_plan_rejects_conflicting_selected_infrastructure_before_planning(local, monkeypatch):
    from api_agent.runtime import infrastructure_contract, infrastructure_plan, inventory
    from api_agent.tooling import services
    local.config.tool_plan = SimpleNamespace(tools=('selected',))
    local.owner.context.infrastructure_paths = ('declared',)
    monkeypatch.setattr(infrastructure_contract, 'load_infrastructure_contributions',
                        Mock(return_value=(SimpleNamespace(id='shared', value='first'),)))
    monkeypatch.setattr(services, 'selected_contributions',
                        Mock(return_value=(SimpleNamespace(id='shared', value='different'),)))
    planner = Mock()
    monkeypatch.setattr(infrastructure_plan, 'plan_infrastructure', planner)
    inventory_capture = Mock()
    monkeypatch.setattr(inventory, 'capture_inventory', inventory_capture)

    with pytest.raises(ValueError, match='^Conflicting selected infrastructure identity$'):
        _plan(local.config, object(), local.owner.context, 'scope')

    planner.assert_not_called()
    inventory_capture.assert_not_called()


def test_local_start_binds_selected_tool_outputs_in_operation_callback(local, monkeypatch):
    from api_agent.tooling import services
    selected = SimpleNamespace(tools=('selected',))
    bound = SimpleNamespace(tools=('bound',))
    config = ConfigResult(dict(local.config.values), {}, (), (), (), tool_plan=selected)
    local.owner.resolve = lambda: config
    converted = replace(config, values={**config.values, 'SELECTED': 'value'})
    conversion = Mock(return_value=converted)
    monkeypatch.setattr(services, 'tool_operation_values', conversion)
    binding = Mock(return_value=bound)
    monkeypatch.setattr(services, 'bind_tool_outputs', binding)

    async def run():
        await local.owner.start()
        options = local.operation_factory.call_args.kwargs
        infrastructure = object()
        result = options['bind_infrastructure'](converted, infrastructure)
        assert result.tool_plan is bound
        assert result.values == converted.values
        assert options['required_kinds'] == {'llm'}
        conversion.assert_called_once_with(selected, config)
        binding.assert_called_once_with(selected, infrastructure)
        await local.owner.aclose()
    asyncio.run(run())


def test_local_operation_prepares_selected_tool_manifests_with_provider_manifests(local, monkeypatch):
    from api_agent.runtime.operation import local_operation
    from api_agent.runtime import dependencies
    from api_agent.providers import distribution
    from api_agent.tooling import services
    config = local.config
    selected = SimpleNamespace(tools=('selected',))
    config.tool_plan = selected
    monkeypatch.setattr(dependencies, 'active_environment', Mock(return_value=True))
    prepare = Mock(return_value=None)
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)
    monkeypatch.setattr(distribution, 'selected_manifests', Mock(return_value=('provider-manifest',)))
    manifests = Mock(return_value=('tool-manifest',))
    monkeypatch.setattr(services, 'selected_tool_manifests', manifests)
    runner = local_operation(local.owner.context, resolve=lambda: config, argv=('--selected',),
        scope='scope', ownership_directory=local.owner.context.owner_root, required_kinds={'llm'})

    runner.prepare_dependencies(config)

    manifests.assert_called_once_with(selected)
    prepare.assert_called_once_with(local.owner.context, ('--selected',), collect=False,
        groups=(), selected_provider_manifests=('provider-manifest', 'tool-manifest'))


def test_local_runtime_gives_graph_named_tools_before_studio_invocation(local, monkeypatch):
    from pathlib import Path
    from api_agent.runtime import local_run, client_tls
    from api_agent.ui import process
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    selected = SimpleNamespace(tools=('selected',))
    local.config.tool_plan = selected
    definition = local.prepared[0]
    resolver = object()
    runtime = SimpleNamespace(services={}, emissions=EmissionBoundary(ContentPolicy()), tracing=None,
                              factory_plan=(), tools_for=Mock(return_value=resolver))
    monkeypatch.setattr(local_run, 'compose_runtime', Mock(return_value=runtime))
    monkeypatch.setattr(client_tls, 'verified_client_context', lambda value: 'verified')
    monkeypatch.setattr(process, 'StudioProcesses', Mock(return_value='process-owner'))
    llm = SimpleNamespace(describe=AsyncMock(return_value=()), complete=AsyncMock(),
                          ensure_ready=AsyncMock(), aclose=AsyncMock())
    local.owner.llm_factory = Mock(return_value=llm)
    local.owner._graph = SimpleNamespace(definition=definition, complete=AsyncMock(return_value='answer'))
    local.owner._requires_api = True

    async def run():
        result = local.owner._runtime(local.config, 'https://localhost:8443',
                                      SimpleNamespace(ca_file=Path('/synthetic-ca')))
        assert result.tool_plan is selected
        assert local.owner._graph.tools is resolver
        runtime.tools_for.assert_called_once_with(definition)
        assert result.factory_plan == ()
        assert await result.services['studio_backend'].complete('request', auth=None, context='context') == 'answer'
        local.owner._graph.complete.assert_awaited_once_with('request', llm=llm, context='context')
        await result.services['studio_backend'].aclose()
    asyncio.run(run())
