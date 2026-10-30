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


def protected(provider):
    """Declare the credential contract on a synthetic setup-only provider."""
    provider.credential_protection_version = 1
    provider.bind_credential_authority = lambda authority: None
    return provider


def test_snapshot_private_paths_and_bounds(tmp_path, monkeypatch):
    path = snapshot(tmp_path, monkeypatch, {'STATE': tmp_path, 'SECRET': 'synthetic-secret'})
    assert path.stat().st_mode & 0o777 == 0o600
    assert p._read_snapshot()['config'] == {'STATE': str(tmp_path), 'SECRET': 'synthetic-secret'}
    with pytest.raises(ContractError, match='too large'):
        p.write_launch_snapshot(tmp_path / 'absent', SimpleNamespace(owner_root=tmp_path), {'DATA': 'x' * 1048576})
    with pytest.raises(FileExistsError):
        p.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {})


def test_large_repeated_rate_cards_round_trip_per_case(tmp_path, monkeypatch):
    card = {'models': {'model': {'rate': 'x' * 90000}}}
    backend = {'version': 1, 'host': '127.0.0.1', 'port': 43210, 'capability': 'a' * 32}
    rows = {f'case-{i}': {'config': {'_COST_RATE_CARD': card, 'choice': i},
                          'provenance': {'choice': f'source-{i}'},
                          'setup_backend': {**backend, 'port': 43210 + i}} for i in range(19)}
    rows['null'] = {'config': {'_COST_RATE_CARD': None}, 'provenance': {}}
    rows['missing'] = {'config': {}, 'provenance': {}}
    rows['different'] = {'config': {'_COST_RATE_CARD': {'other': 1}}, 'provenance': {}}
    assert len(json.dumps(rows).encode()) > p._SNAPSHOT_BYTES
    path = p.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {}, test_profiles=rows)
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    assert path.stat().st_size < p._SNAPSHOT_BYTES
    restored = p._read_snapshot()['test_profiles']
    assert restored == rows
    for i in (0, 18):
        request = SimpleNamespace(node=SimpleNamespace(nodeid=f'case-{i}'))
        effective = p._test_snapshot(request)
        assert effective['config'] == rows[f'case-{i}']['config']
        assert effective['profile_provenance'] == rows[f'case-{i}']['provenance']
        assert effective['setup_backend'] == rows[f'case-{i}']['setup_backend']
    restored['case-0']['config']['_COST_RATE_CARD']['models'].clear()
    assert restored['case-1']['config']['_COST_RATE_CARD'] == card


@pytest.mark.parametrize('index', [True, -1, 1, '0'])
def test_snapshot_rejects_invalid_rate_card_references(tmp_path, monkeypatch, index):
    path = snapshot(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data['test_profile_rate_cards'] = [None]
    data['test_profiles'] = {'case': {'config': {}, 'provenance': {}, 'rate_card_index': index}}
    path.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='start through launch.sh'):
        p._read_snapshot()


def test_snapshot_rejects_rate_card_expansion_over_budget(tmp_path, monkeypatch):
    path = snapshot(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data['test_profile_rate_cards'] = ['x' * 100]
    data['test_profiles'] = {str(i): {'config': {}, 'provenance': {}, 'rate_card_index': 0} for i in range(2)}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(p, '_EXPANDED_PROFILE_BYTES', 150)
    with pytest.raises(ContractError, match='start through launch.sh'):
        p._read_snapshot()
    with pytest.raises(ContractError, match='too large'):
        p.write_launch_snapshot(tmp_path / 'absent', SimpleNamespace(owner_root=tmp_path), {},
            test_profiles={str(i): {'config': {'_COST_RATE_CARD': 'x' * 100}, 'provenance': {}} for i in range(2)})


@pytest.mark.parametrize('profiles,cards', [([], []), ({}, {})])
def test_rate_card_decoder_rejects_invalid_container_types(profiles, cards):
    with pytest.raises(ValueError, match='Invalid integration profile rate cards'):
        p._unpack_profile_rate_cards(profiles, cards)


def test_rate_card_decoder_rejects_non_object_profile():
    with pytest.raises(ValueError, match='Invalid integration profile entry'):
        p._unpack_profile_rate_cards({'case': []}, [])


@pytest.mark.parametrize('problem', ['duplicate', 'orphan'])
def test_snapshot_rejects_inconsistent_rate_card_table(tmp_path, monkeypatch, problem):
    path = snapshot(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data['test_profile_rate_cards'] = [None]
    if problem == 'duplicate':
        data['test_profiles'] = {'case': {'config': {'_COST_RATE_CARD': None}, 'provenance': {}, 'rate_card_index': 0}}
    path.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='start through launch.sh'):
        p._read_snapshot()


def test_legacy_snapshot_profiles_remain_readable(tmp_path, monkeypatch):
    path = snapshot(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    profiles = {'case': {'config': {'_COST_RATE_CARD': None}, 'provenance': {}}}
    data['test_profiles'] = profiles
    path.write_text(json.dumps(data))
    assert p._read_snapshot()['test_profiles'] == profiles


@pytest.mark.parametrize('problem', ['missing', 'mode', 'owner', 'symlink', 'large', 'json', 'list', 'version', 'bool_version', 'fields', 'config', 'root', 'source', 'definitions', 'missing_definitions'])
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
        elif problem == 'source':
            data['unattended_index_source'] = 0
        elif problem == 'definitions':
            data['password_manager_definitions'] = {}
        elif problem == 'missing_definitions':
            del data['password_manager_definitions']
        else:
            data['owner_root'] = 'relative'
        path.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='start through launch.sh') as error:
        p._read_snapshot()
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('source', [None, '../base/.env', '../pack/.env', 'environment', 'cli'])
def test_source_metadata_is_separate_from_provider_settings_and_reaches_worker(tmp_path, monkeypatch, source):
    path = p.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path),
                                   {'UNATTENDED_USER_INDEX': 0}, unattended_index_source=source)
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    data = p._read_snapshot()
    assert data['config'] == {'UNATTENDED_USER_INDEX': 0}
    assert data.get('unattended_index_source') == source
    assert ('unattended_index_source' in data) is (source is not None)
    values = p.llm_provider.__wrapped__(None)
    owner = next(values)
    assert owner.unattended_index_source == source
    with pytest.raises(StopIteration):
        next(values)
    assert owner.closed and owner.loop.is_closed()


@pytest.mark.parametrize('source', ['../base/.env', 'environment'])
@pytest.mark.parametrize('jobs', [0, 2])
def test_logged_out_cli_fails_real_worker_without_browser_and_records_guidance(tmp_path, source, jobs):
    import sys
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_ready.py').write_text(
        'def test_ready(llm_provider, monkeypatch):\n'
        '    import httpx\n'
        '    from api_agent.llm_providers.gpt import factory\n'
        '    def forbidden(*args, **kwargs):\n'
        '        raise AssertionError("browser or transport must not start")\n'
        '    monkeypatch.setattr(httpx, "AsyncClient", forbidden)\n'
        '    monkeypatch.setattr(factory, "subscription_browser", forbidden)\n'
        '    llm_provider.ensure_ready()\n')
    cli = tmp_path / 'logged-out-cli'
    cli.write_text('#!' + sys.executable + '\nimport sys\nsys.stderr.write("session expired; login required\\n")\nsys.exit(1)\n')
    cli.chmod(0o700)
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    config = {'LLM_PROVIDER': str(Path(p.__file__).parents[1] / 'llm_providers/gpt/factory.py') + ':factory',
              'GPT_STATE_DIR': tmp_path / 'state', 'UNATTENDED_USER_INDEX': 0,
              'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
                  'password_manager': {'manager': '1password', 'cli_path': str(cli)},
                  'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}},
              'TUI_MODE': 'integration', 'NO_STD_IN': False, 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'allure', 'TEST_PARALLEL_JOBS': jobs}
    selected = [item for item in collect_tests(context, 'integration', config=config) if item.repo == 'base']
    result = run_recorded_integration(context, selected, config, accepted=True,
        storage=SimpleNamespace(persist=lambda *args: None), unattended_index_source=source)
    assert result.exit_code == 1 and result.persisted
    assert result.execution.tests[0].status == 'failed'
    trace = result.execution.tests[0].traceback
    assert 'UnattendedCredentialError' in trace
    assert 'Failed to get credentials for unattended access.' in trace
    assert trace.startswith('Failed to get credentials for unattended access.')
    assert 'Password-manager username retrieval is unavailable.' in trace
    assert ('Clear .env configuration' in trace) is source.endswith('/.env')
    assert ('stop passing the override' in trace) is not source.endswith('/.env')
    assert 'AssertionError: browser or transport must not start' not in trace
    rows = [json.loads(path.read_text()) for path in result.directory.glob('*-result.json')]
    assert len(rows) == 1 and rows[0]['status'] == 'failed'
    assert rows[0]['statusDetails']['trace'] == trace


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
    provider = SimpleNamespace(implementation=protected(Adapter()), aclose=AsyncMock())
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
        implementation = SimpleNamespace()
    if problem == 'interaction':
        implementation = SimpleNamespace(asetup=setup)
    loaded = SimpleNamespace(implementation=protected(implementation), aclose=AsyncMock())
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
    requirement = p.integration_config.__wrapped__(None, lambda *args: None)
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
    provider = SimpleNamespace(implementation=protected(adapter), aclose=AsyncMock())
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
def test_selected_invalid_combination_skips_shared_setup(tmp_path, jobs):
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
    # Exercise this synthetic suite independently of bundled provider additions.
    selected = collect_tests(context, 'integration', ('base::tests/integration/test_ready.py',), config=config)
    assert len(selected) == 1
    recorded = run_recorded_integration(context, selected, config, accepted=True,
        storage=SimpleNamespace(persist=lambda *args: None))
    assert recorded.exit_code == 0 and recorded.persisted
    assert [test.status for test in recorded.execution.tests] == ['skipped']
    reason = 'GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.'
    assert recorded.execution.tests[0].traceback == reason
    assert not state.exists()
    rows = [json.loads(path.read_text()) for path in recorded.directory.glob('*-result.json')]
    assert [row['status'] for row in rows] == ['skipped']
    assert next(row for row in rows if row['status'] == 'skipped')['statusDetails']['message'] == reason


def test_fixture_does_not_setup_and_always_closes(tmp_path, monkeypatch):
    snapshot(tmp_path, monkeypatch)
    values = p.llm_provider.__wrapped__(None)
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
def test_real_launch_worker_cannot_use_an_unbound_cache_for_selected_user(tmp_path, monkeypatch, cached, jobs):
    import time
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import Connection
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    import httpx
    from unittest.mock import Mock
    from api_agent.llm_providers.gpt import browser, factory
    # Authentication now runs in the launcher's backend, not the pytest child.
    # Guard that process too; no transport or browser is valid in this case.
    transport = Mock(side_effect=AssertionError('unexpected transport'))
    window = Mock(side_effect=AssertionError('unexpected browser'))
    monkeypatch.setattr(httpx, 'AsyncClient', transport)
    monkeypatch.setattr(browser, 'subscription_browser', window)
    monkeypatch.setattr(factory, 'subscription_browser', window)
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
    assert recorded.exit_code == 1
    assert recorded.execution.tests[0].status == 'failed'
    assert 'Shared setup connection failed' in recorded.execution.tests[0].traceback
    transport.assert_not_called()
    window.assert_not_called()
    assert 'synthetic-secret' not in recorded.execution.output
    results = list(recorded.directory.glob('*-result.json'))
    assert len(results) == 1 and json.loads(results[0].read_text())['status'] == 'failed'


def test_pack_requirement_marker_registered_for_worker():
    from unittest.mock import Mock
    config = Mock()
    p.pytest_configure(config)
    from unittest.mock import call
    assert config.addinivalue_line.call_args_list == [
        call("markers", "requires_pack_api: selected integration requires pack API/identity preflight"),
        call("markers", "requires_ui_session: selected integration consumes the UI provider"),
        call("markers", "requires_llm: UI integration also consumes the LLM provider"),
        call("markers", "credential_authorities(*owners): explicitly admit every worker credential producer"),
    ]


@pytest.mark.parametrize('endpoint', [None, 'http://127.0.0.1:12345'])
def test_debug_worker_retains_browser_and_drives_loop_after_auth_close(tmp_path, monkeypatch, capsys, endpoint):
    from api_agent.providers.contracts import BrowserSession
    events = []
    provider = p.IntegrationLLM({'LLM_PROVIDER': 'selected', 'DEBUG': True, 'NO_STD_IN': True}, tmp_path)
    class Auth:
        async def __call__(self): return object()
        async def aclose(self): events.append('auth-close')
    class Adapter:
        async def asetup(self, **kwargs):
            assert kwargs['interactive'] is True
            session = BrowserSession(SimpleNamespace(is_connected=lambda: True), SimpleNamespace(pages=[object()]), object(), endpoint)
            provider.inspection.observe(session)
            async def close_window():
                await asyncio.sleep(.02)
                session.context.pages.clear()
            asyncio.create_task(close_window())
            return Auth()
    loaded = SimpleNamespace(implementation=protected(Adapter()), aclose=AsyncMock(side_effect=lambda: events.append('provider-close')))
    load = AsyncMock(return_value=loaded)
    monkeypatch.setattr(p, 'aload_provider', load)
    provider.ensure_ready()
    assert load.await_args.args[2]['browser_inspection'] is provider.inspection
    provider.close()
    assert events == ['auth-close', 'provider-close'] and provider.loop.is_closed()
    output = capsys.readouterr().out
    assert 'Debug browser retained' in output
    assert ('Provider browser inspection:' in output) == (endpoint is not None)


def test_debug_empty_inspector_and_interrupted_inspection_cleanup(tmp_path, monkeypatch):
    provider = p.IntegrationLLM({'DEBUG': True}, tmp_path)
    provider.close()
    assert provider.loop.is_closed()
    provider = p.IntegrationLLM({'DEBUG': True}, tmp_path)
    monkeypatch.setattr(provider.inspection, 'wait_closed', AsyncMock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        provider.close()
    assert provider.inspection._closed
    provider.close()
    assert provider.loop.is_closed()


def test_finish_run_disposes_auth_before_retaining_creating_loop(tmp_path):
    provider = p.IntegrationLLM({'DEBUG': True}, tmp_path)
    events = []
    provider.owner = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: events.append('auth')))
    provider.provider = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: events.append('provider')))
    async def setup():
        provider.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
        task = asyncio.create_task(asyncio.sleep(.01, result=None))
        provider._credential_tasks.add(task)
        task.add_done_callback(lambda _: events.append('credential'))
    provider.loop.run_until_complete(setup())
    try:
        provider.finish_run()
        assert events == ['credential', 'auth', 'provider']
        assert not provider.loop.is_closed() and not provider.closed
        assert provider in p._inspection_providers
        assert not provider._credential_tasks
    finally:
        p._inspection_providers.remove(provider)
        provider.loop.run_until_complete(provider.inspection.aclose())
        provider.loop.close()


def test_worker_shared_setup_coalesces_and_freezes_selected_identity(tmp_path, monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()
    class Auth:
        def __init__(self):
            self.reads = 0
            self.aclose = AsyncMock()
        async def __call__(self):
            self.reads += 1
            entered.set()
            await finish.wait()
            return object()
    auth = Auth()
    adapter = SimpleNamespace(asetup=AsyncMock(return_value=auth))
    loaded = SimpleNamespace(implementation=protected(adapter), aclose=AsyncMock())
    load = AsyncMock(return_value=loaded)
    monkeypatch.setattr(p, 'aload_provider', load)
    worker = p.IntegrationLLM({'LLM_PROVIDER': 'selected'}, tmp_path)
    other = p.IntegrationLLM({'LLM_PROVIDER': 'selected'}, tmp_path)
    assert worker._operation_id != other._operation_id
    other.close()
    async def scenario():
        waiters = [asyncio.create_task(worker._ensure()) for _ in range(5)]
        await entered.wait()
        assert adapter.asetup.await_count == 1 and auth.reads == 1
        finish.set()
        await asyncio.gather(*waiters)
        assert worker.owner is worker._shared_setup._owner
        assert load.await_count == 1
        worker.config['UNATTENDED_USER_INDEX'] = 1
        with pytest.raises(ContractError, match='outside shared binding'):
            await worker._ensure()
        assert auth.reads == 1
    try:
        worker.loop.run_until_complete(scenario())
    finally:
        worker.close()
    auth.aclose.assert_awaited_once()


def test_worker_revokes_before_provider_disposal_and_rejects_late_reply(tmp_path, monkeypatch):
    from api_agent.providers.contracts import SessionClosedError
    entered = asyncio.Event()
    reply = asyncio.Event()
    captured = []
    async def reader(index, kind):
        entered.set()
        await reply.wait()
        return 'private'
    class Auth:
        async def __call__(self):
            return object()
        async def aclose(self):
            reply.set()
    async def setup(**kwargs):
        captured.append(kwargs['credentials'])
        return Auth()
    async def provider_close():
        with pytest.raises(SessionClosedError):
            await captured[0](0, 'password')
    loaded = SimpleNamespace(implementation=protected(SimpleNamespace(asetup=setup)), aclose=provider_close)
    monkeypatch.setattr(p, 'aload_provider', AsyncMock(return_value=loaded))
    worker = p.IntegrationLLM({'LLM_PROVIDER': 'selected', 'UNATTENDED_USER_INDEX': 0}, tmp_path)
    monkeypatch.setattr(worker, '_credentials', lambda index: reader)
    worker.ensure_ready()
    async def scenario():
        request = asyncio.create_task(captured[0](0, 'password'))
        await entered.wait()
        await worker._close()
        with pytest.raises(SessionClosedError):
            await request
    try:
        worker.loop.run_until_complete(scenario())
    finally:
        worker.close()


def test_cancelled_worker_helper_drain_waits_for_true_completion(tmp_path):
    worker = p.IntegrationLLM({}, tmp_path)
    entered = asyncio.Event()
    finish = asyncio.Event()
    async def helper():
        entered.set()
        await finish.wait()
    async def scenario():
        task = asyncio.create_task(helper())
        worker._credential_tasks.add(task)
        await entered.wait()
        close = asyncio.create_task(worker._close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        assert not task.done() and task in worker._credential_tasks
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        await worker._close()
        assert task.done() and not task.cancelled()
        assert not worker._credential_tasks
    try:
        worker.loop.run_until_complete(scenario())
    finally:
        worker.close()


def test_unsettled_helper_is_not_cancelled_or_acknowledged(tmp_path, monkeypatch):
    worker = p.IntegrationLLM({}, tmp_path)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def helper():
            entered.set()
            await release.wait()
        task = asyncio.create_task(helper())
        worker._credential_tasks.add(task)
        task.add_done_callback(worker._credential_tasks.discard)
        await entered.wait()
        async def unresolved(tasks, *, timeout):
            assert timeout <= 5
            return set(), set(tasks)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(p.asyncio, 'wait', unresolved)
                with pytest.raises(ContractError, match='Credential helpers remain active'):
                    await worker._finish_execution()
            assert not task.done() and not task.cancelled()
            assert task in worker._credential_tasks
        finally:
            release.set()
            await task
        await worker._finish_execution()
        assert not worker._credential_tasks
    try:
        worker.loop.run_until_complete(scenario())
    finally:
        worker.close()
