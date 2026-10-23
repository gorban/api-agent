"""Managed dashboard provisioning remains identical across operation modes."""
from api_agent.config import load_shared_schema, resolve_config
from api_agent.runtime.compose import plan_compose
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.prerequisites import PrerequisiteManager
from test_prerequisites import Clock, Engine

import pytest


@pytest.mark.parametrize('test_mode', ['integration', 'e2e'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('running', [False, True])
@pytest.mark.parametrize('credentials', [{}, {
    'LANGFUSE_ADMIN_EMAIL': 'operator@example.invalid',
    'LANGFUSE_ADMIN_PASSWORD': 'synthetic-password',
}])
def test_cross_mode_retained_stack_reuses_exact_provisioning(tmp_path, test_mode, reverse, running, credentials):
    modes = (test_mode, 'run-local') if reverse else ('run-local', test_mode)
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    engine, clock = Engine(), Clock()
    managers = []
    for mode in modes:
        result = resolve_config(load_shared_schema(), environ={
            'TUI_MODE': mode, 'TRACE_MODE': 'LangFuse',
            'ALLURE_S3_BUCKET': 'synthetic-results', **credentials},
            anchors={'owner_root': tmp_path})
        assert not result.errors and not result.missing
        assert result.values['LANGFUSE_ADMIN_EMAIL'] == credentials.get('LANGFUSE_ADMIN_EMAIL', 'admin@local.dev')
        assert result.values['LANGFUSE_ADMIN_PASSWORD'] == credentials.get('LANGFUSE_ADMIN_PASSWORD', 'password')
        plan = plan_compose(result.values, scope)
        managers.append(PrerequisiteManager(plan, scope, tmp_path / 'ownership',
            command_runner=engine.run, readiness_probe=lambda *args: True,
            now=clock.now, sleep=clock.sleep, identity=lambda pid: 'boot:start'))
    first, second = managers
    assert first.fingerprints == second.fingerprints
    engine.owner = first
    initial = first.acquire()
    first.release(retain=running)
    engine.owner = second

    repeated = second.acquire()

    assert set(repeated.container_ids) == set(initial.container_ids)
    assert set(repeated.reused_ids if running else repeated.owned_ids) == set(initial.container_ids)
    assert all(record['State']['Running'] for record in engine.records.values())
    assert second.release(retain=True) == ()
    assert not any('down' in command for command, _ in engine.commands)
