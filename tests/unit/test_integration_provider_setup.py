"""Lazy setup, IPC privacy and launch-owned teardown without live accounts."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, InteractionRequiredError
from api_agent.testing import provider_setup as p


def snapshot(tmp_path, monkeypatch, config=None):
    path = p.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), config or {})
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    return path


def test_snapshot_private_paths_and_bounds(tmp_path, monkeypatch):
    path = snapshot(tmp_path, monkeypatch, {'STATE': tmp_path, 'SECRET': 'synthetic-secret'})
    assert path.stat().st_mode & 0o777 == 0o600
    assert p._read_snapshot()['config'] == {'STATE': str(tmp_path), 'SECRET': 'synthetic-secret'}
    with pytest.raises(ContractError, match='too large'):
        p.write_launch_snapshot(tmp_path / 'absent', SimpleNamespace(owner_root=tmp_path), {'DATA': 'x' * 1048576})
    with pytest.raises(FileExistsError):
        p.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {})


@pytest.mark.parametrize('problem', ['missing', 'mode', 'owner', 'symlink', 'large', 'json', 'list', 'version', 'bool_version', 'fields', 'config', 'root'])
def test_snapshot_rejects_untrusted_context_without_echo(tmp_path, monkeypatch, problem):
    path = snapshot(tmp_path, monkeypatch)
    if problem == 'missing':
        monkeypatch.delenv('API_AGENT_INTEGRATION_CONTEXT')
    elif problem == 'mode':
        path.chmod(0o644)
    elif problem == 'owner':
        monkeypatch.setattr(p.os, 'getuid', lambda: -1)
    elif problem == 'symlink':
        link = tmp_path / 'alias'
        link.symlink_to(path)
        monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(link))
    elif problem == 'large':
        path.write_bytes(b'x' * 1048577)
    elif problem == 'json':
        path.write_text('synthetic-secret')
    elif problem == 'list':
        path.write_text('[]')
    else:
        data = json.loads(path.read_text())
        if problem == 'version':
            data['version'] = 2
        elif problem == 'bool_version':
            data['version'] = True
        elif problem == 'fields':
            data['extra'] = 'synthetic-secret'
        elif problem == 'config':
            data['config'] = []
        else:
            data['owner_root'] = 'relative'
        path.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='start through launch.py') as error:
        p._read_snapshot()
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('index,no_std_in', [(None, False), (0, False), (None, True)])
def test_setup_is_lazy_reusable_and_disposed_on_same_loop(tmp_path, monkeypatch, index, no_std_in):
    events = []
    class Auth:
        async def __call__(self):
            events.append('token')
            return object()
        async def aclose(self):
            events.append('auth-close')
    class Adapter:
        async def asetup(self, **kwargs):
            assert kwargs == {'interactive': index is None and not no_std_in, 'user_index': index, 'credentials': None}
            events.append('setup')
            return Auth()
    provider = SimpleNamespace(implementation=Adapter(), aclose=AsyncMock())
    load = AsyncMock(return_value=provider)
    monkeypatch.setattr(p, 'aload_provider', load)
    owner = p.IntegrationLLM({'LLM_PROVIDER': 'selected', 'UNATTENDED_USER_INDEX': index, 'NO_STD_IN': no_std_in}, tmp_path)
    assert events == [] and not load.called
    owner.ensure_ready()
    owner.ensure_ready()
    assert events == ['setup', 'token', 'token'] and load.await_count == 1
    owner.close()
    owner.close()
    assert events[-1] == 'auth-close' and provider.aclose.await_count == 1
    assert owner.loop.is_closed()
    with pytest.raises(ContractError, match='closed'):
        owner.ensure_ready()


@pytest.mark.parametrize('problem', ['missing', 'unsupported', 'none', 'interaction'])
def test_setup_failures_are_not_success(tmp_path, monkeypatch, problem):
    async def setup(**kwargs):
        if problem == 'interaction':
            raise InteractionRequiredError('Attended consent is required.')
        return SimpleNamespace(__call__=AsyncMock(), aclose=AsyncMock())
    class EmptyAuth:
        async def __call__(self): return None
        async def aclose(self): pass
    implementation = SimpleNamespace(asetup=AsyncMock(return_value=EmptyAuth()))
    if problem == 'unsupported':
        implementation = object()
    if problem == 'interaction':
        implementation = SimpleNamespace(asetup=setup)
    loaded = SimpleNamespace(implementation=implementation, aclose=AsyncMock())
    monkeypatch.setattr(p, 'aload_provider', AsyncMock(return_value=loaded))
    owner = p.IntegrationLLM({} if problem == 'missing' else {'LLM_PROVIDER': 'selected'}, tmp_path)
    try:
        with pytest.raises(pytest.skip.Exception if problem in {'missing', 'unsupported'} else ContractError):
            owner.ensure_ready()
    finally:
        owner.close()
    assert owner.loop.is_closed()


@pytest.mark.parametrize('values', [{}, {'FIRST': None, 'SECOND': ''}])
def test_required_configuration_lists_missing_names_without_values(values):
    requirement = p.IntegrationRequirements(values)
    with pytest.raises(pytest.skip.Exception, match='Missing required configuration: FIRST, SECOND'):
        requirement.require('FIRST', 'SECOND')
    requirement.require()
    assert values == requirement.values


def test_requirement_fixture_is_inert_and_false_zero_are_provided(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch, {'BOOL': False, 'NUMBER': 0, 'VALUE': 'configured'})
    requirement = p.integration_config.__wrapped__()
    requirement.require('BOOL', 'NUMBER', 'VALUE')
    requirement.require_combination(True, 'not relevant')
    with pytest.raises(pytest.skip.Exception, match='This operation requires the supported combination'):
        requirement.require_combination(False, 'This operation requires the supported combination')


@pytest.mark.parametrize('problem', ['configuration', 'authentication', 'transport'])
def test_only_explicit_prerequisites_become_skips(tmp_path, monkeypatch, problem):
    from api_agent.providers.contracts import SetupPrerequisiteError
    from api_agent.providers.control import OperationalError
    errors = {'configuration': SetupPrerequisiteError('Unsupported selected combination'),
              'authentication': InteractionRequiredError('Sign-in is required'),
              'transport': OSError('transport failed')}
    adapter = SimpleNamespace(asetup=AsyncMock(side_effect=errors[problem]))
    provider = SimpleNamespace(implementation=adapter, aclose=AsyncMock())
    monkeypatch.setattr(p, 'aload_provider', AsyncMock(return_value=provider))
    owner = p.IntegrationLLM({'LLM_PROVIDER': 'selected'}, tmp_path)
    try:
        expected = pytest.skip.Exception if problem == 'configuration' else OperationalError if problem == 'transport' else InteractionRequiredError
        try:
            owner.ensure_ready()
        except BaseException as error:
            observed = error
        else:
            pytest.fail('Expected setup rejection')
        assert isinstance(observed, expected)
        assert ('unknown' if problem == 'transport' else str(errors[problem])) in str(observed)
    finally:
        owner.close()
    provider.aclose.assert_awaited_once()


@pytest.mark.parametrize('value', [False, 0, [], 'malformed'])
def test_malformed_provider_selection_is_not_a_skip(tmp_path, value):
    from api_agent.providers.loading import ProviderError
    owner = p.IntegrationLLM({'LLM_PROVIDER': value}, tmp_path)
    try:
        try:
            owner.ensure_ready()
        except BaseException as error:
            observed = error
        else:
            pytest.fail('Expected invalid provider rejection')
        assert isinstance(observed, ProviderError)
        assert not isinstance(observed, pytest.skip.Exception)
    finally:
        owner.close()


@pytest.mark.parametrize('jobs', [0, 2])
def test_selected_invalid_combination_skips_shared_setup_but_runs_isolated_checks(tmp_path, jobs):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_ready.py').write_text('def test_ready(llm_provider):\n    llm_provider.ensure_ready()\n')
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    state = tmp_path / 'never-created'
    config = {'LLM_PROVIDER': str(Path(p.__file__).parents[1] / 'llm_providers/gpt/factory.py') + ':factory',
              'GPT_AUTH_MODE': 'subscription', 'GPT_API_MODE': 'completions', 'GPT_STATE_DIR': state,
              'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'allure', 'TEST_PARALLEL_JOBS': jobs}
    selected = collect_tests(context, 'integration', config=config)
    assert len(selected) == 3
    recorded = run_recorded_integration(context, selected, config, accepted=True,
        storage=SimpleNamespace(persist=lambda *args: None))
    assert recorded.exit_code == 0 and recorded.persisted
    assert [test.status for test in recorded.execution.tests] == ['skipped', 'passed', 'passed']
    reason = 'GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.'
    assert recorded.execution.tests[0].traceback == reason
    assert not state.exists()
    rows = [json.loads(path.read_text()) for path in recorded.directory.glob('*-result.json')]
    assert sorted(row['status'] for row in rows) == ['passed', 'passed', 'skipped']
    assert next(row for row in rows if row['status'] == 'skipped')['statusDetails']['message'] == reason


def test_fixture_does_not_setup_and_always_closes(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch)
    values = p.llm_provider.__wrapped__()
    owner = next(values)
    assert owner.provider is None
    with pytest.raises(StopIteration):
        next(values)
    assert owner.loop.is_closed()


def test_cleanup_failure_keeps_loop_for_retry(tmp_path, monkeypatch):
    owner = p.IntegrationLLM({}, tmp_path)
    cleanup = AsyncMock(side_effect=[RuntimeError('cleanup'), None])
    owner.provider = SimpleNamespace(aclose=cleanup)
    with pytest.raises(RuntimeError):
        owner.close()
    assert not owner.closed and not owner.loop.is_closed()
    owner.close()
    assert owner.loop.is_closed() and cleanup.await_count == 2


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('jobs', [0, 2])
def test_real_launch_worker_setup_and_allure_without_inference(tmp_path, cached, jobs):
    import time
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import Connection
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_ready.py').write_text('def test_ready(llm_provider, monkeypatch):\n'
        '    import httpx\n'
        '    monkeypatch.setattr(httpx, "AsyncClient", lambda: (_ for _ in ()).throw(AssertionError("unexpected transport")))\n'
        '    llm_provider.ensure_ready()\n')
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    state = tmp_path / 'private'
    if cached:
        store = ConnectionStore(state)
        store.save(Connection('subject', 'client', store.host_id(), '', 'synthetic-secret',
            'refresh', 'id', ('chatgpt.tokens.use.direct',), time.time() + 3600))
    config = {'LLM_PROVIDER': str(Path(p.__file__).parents[1] / 'llm_providers/gpt/factory.py') + ':factory',
              'GPT_STATE_DIR': state, 'UNATTENDED_USER_INDEX': 0, 'TUI_MODE': 'integration',
              'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': tmp_path / 'allure', 'TEST_PARALLEL_JOBS': jobs}
    selected = [item for item in collect_tests(context, 'integration', config=config) if item.repo == 'base']
    saved = []
    storage = SimpleNamespace(persist=lambda directory, manifest: saved.append((directory, manifest)))
    recorded = run_recorded_integration(context, selected, config, accepted=True, storage=storage)
    assert recorded.persisted and len(saved) == 1
    assert recorded.exit_code == (0 if cached else 1)
    assert recorded.execution.tests[0].status == ('passed' if cached else 'failed')
    if not cached:
        assert 'attended sign-in' in recorded.execution.tests[0].traceback
    assert 'synthetic-secret' not in recorded.execution.output
    results = list(recorded.directory.glob('*-result.json'))
    assert len(results) == 1 and json.loads(results[0].read_text())['status'] == ('passed' if cached else 'failed')
