"""Explicit engine transport crosses the real declared-plan/manager boundary."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration
from api_agent.config.resolve import ConfigResult
from api_agent.runtime.compose import ComposeError, ENGINE_ENVIRONMENT_KEYS
from api_agent.runtime.local_run import _plan
from api_agent.runtime.operation import local_operation
from api_agent.runtime import prerequisites
from test_local_run import local

FIXTURE = Path(__file__).parent.parent / 'fixtures/infrastructure_pack/synthetic_infrastructure'
SCHEMA = ConfigSchema({'USE_LOCAL_BACKING': FieldDeclaration('USE_LOCAL_BACKING', 'bool')})


def configure(local, mode):
    values = dict(local.config.values)
    if mode is not None:
        values['USE_LOCAL_BACKING'] = mode
    result = ConfigResult(values, {}, (), (), ())
    local.owner.resolve = lambda: result
    local.owner.schema = SCHEMA
    local.owner.context.pack_root = FIXTURE
    local.owner.context.pack_id = 'synthetic'
    local.owner.context.infrastructure_paths = (FIXTURE / 'infrastructure.toml',)
    local.owner.plan_factory = _plan
    return result


@pytest.mark.parametrize('mode,expected', [
    (True, {'synthetic/backing', 'synthetic/managed'}),
    (False, {'synthetic/external'}),
    (None, set()),
])
@pytest.mark.parametrize('selection', ['explicit', 'shell'])
@pytest.mark.parametrize('engine_key', ['DOCKER_HOST', 'CONTAINER_HOST'])
def test_selected_transport_is_frozen_for_real_plan_and_manager(local, monkeypatch, mode, expected, selection, engine_key):
    configure(local, mode)
    transport = 'unix://' + str(local.owner.context.base_root / 'selected-engine.sock')
    selected = {engine_key: transport, 'PATH': '/controlled/bin'}
    normalized = {**selected, 'DOCKER_HOST': transport}
    for key in ENGINE_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('UNSELECTED_PROVIDER_SECRET', 'not-engine-configuration')
    monkeypatch.setenv(engine_key, transport)
    monkeypatch.setenv('PATH', '/controlled/bin')
    local.owner._engine_environment_source = selected if selection == 'explicit' else None
    actual_manager = prerequisites.PrerequisiteManager
    received = []
    def manager(plan, scope, directory, **options):
        received.append(options['engine_environment'])
        return actual_manager(plan, scope, directory, **options)
    monkeypatch.setattr(prerequisites, 'PrerequisiteManager', manager)
    operation = local.operation
    seen = []
    def operation_factory(context, **options):
        snapshot = options['engine_environment']
        seen.append(snapshot)
        assert options['prepared_plan'].environment['DOCKER_HOST'] == transport
        assert {item.id for item in options['prepared_plan'].conditional} == expected
        selected['DOCKER_HOST'] = 'tcp://changed.invalid:2375'
        monkeypatch.setenv('DOCKER_HOST', 'tcp://changed.invalid:2375')
        runner = local_operation(context, **options)
        owned = runner.manager(options['prepared_plan'], lambda: False)
        assert received[-1] is snapshot
        assert owned.engine_environment['DOCKER_HOST'] == transport
        assert 'UNSELECTED_PROVIDER_SECRET' not in owned.environment
        return operation
    local.owner.operation_factory = operation_factory
    async def run():
        await local.owner.start()
        assert local.owner._engine_environment is seen[0]
        assert dict(seen[0]) == normalized
        with pytest.raises(TypeError): seen[0]['DOCKER_HOST'] = 'replacement'
        await local.owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('environment', [
    {'DOCKER_HOST': 'tcp://remote.invalid:2375'},
    {'CONTAINER_HOST': 'tcp://remote.invalid:2375'},
    {'DOCKER_HOST': 'unix:///selected.sock', 'CONTAINER_HOST': 'unix:///other.sock'},
    {},
    {'DOCKER_HOST': 'unix:///selected.sock', 'PROVIDER_SECRET': 'unexpected'},
])
def test_invalid_active_transport_rejects_before_operation_effects(local, environment):
    configure(local, True)
    local.owner._engine_environment_source = environment
    async def run():
        with pytest.raises(ComposeError): await local.owner.start()
        local.operation_factory.assert_not_called()
        local.browser_factory.assert_not_called()
        assert local.calls == []
    asyncio.run(run())


def test_public_constructor_accepts_explicit_transport_without_starting_anything(local):
    from api_agent.runtime.local_run import LocalRunOwner
    selected = {'DOCKER_HOST': 'unix:///selected.sock'}
    owner = LocalRunOwner(local.owner.context, local.owner.resolve, engine_environment=selected)
    assert owner._engine_environment_source is selected
    assert owner._engine_environment is None
