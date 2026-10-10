"""The launch-owned backend shares API-key readiness without exporting its key."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('jobs', [0, 2])
def test_recorded_workers_share_password_only_api_key_setup(tmp_path, monkeypatch, jobs):
    from api_agent.auth import credentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.testing import provider_setup
    import httpx

    key = 'synthetic-api-key-never-exported'
    calls = []
    def password(profile, **kwargs):
        calls.append((profile.vault, profile.user))
        return key
    monkeypatch.setattr(credentials, 'get_password', password)
    forbidden = Mock(side_effect=AssertionError('Unexpected subscription or network work'))
    monkeypatch.setattr(credentials, 'get_username', forbidden)
    monkeypatch.setattr(credentials, 'get_totp', forbidden)
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    monkeypatch.setattr(factory, 'ConnectionStore', forbidden)
    monkeypatch.setattr(factory, 'subscription_browser', forbidden)
    load = provider_setup.aload_provider
    loaded_providers, sessions = [], []
    async def guarded_load(*args, **kwargs):
        loaded = await load(*args, **kwargs)
        loaded_providers.append(loaded)
        globals_ = loaded.implementation.asetup.__globals__
        for name in ('ConnectionStore', 'subscription_browser', 'connect_account'):
            globals_[name] = forbidden
        setup = loaded.implementation.asetup
        async def capture_session(**options):
            session = await setup(**options)
            sessions.append(session)
            return session
        loaded.implementation.asetup = capture_session
        return loaded
    monkeypatch.setattr(provider_setup, 'aload_provider', guarded_load)
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_ready.py').write_text(
        'def test_first(llm_provider):\n    llm_provider.ensure_ready()\n'
        'def test_second(llm_provider):\n    llm_provider.ensure_ready()\n')
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    config = {
        'LLM_PROVIDER': str(Path(factory.__file__).resolve()) + ':factory',
        'LLM_CREDENTIAL_ROLE': 'LLM User', 'GPT_AUTH_MODE': 'api_key',
        'GPT_API_MODE': 'responses', 'UNATTENDED_USER_INDEX': 0,
        'UNATTENDED_AUTH': {
            'password_manager': {'manager': 'keeper', 'cli_path': 'synthetic-cli'},
            'roles': {'LLM User': [{'vault': 'synthetic-vault', 'user': 'synthetic-item'}]}},
        'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'allure', 'TEST_PARALLEL_JOBS': jobs,
    }
    selected = [test for test in collect_tests(context, 'integration', config=config) if test.repo == 'base']
    saved = []
    storage = SimpleNamespace(persist=lambda directory, manifest: saved.append(manifest))
    recorded = run_recorded_integration(context, selected, config, accepted=True, storage=storage)
    assert recorded.exit_code == 0 and recorded.persisted and len(saved) == 1
    assert [test.status for test in recorded.execution.tests] == ['passed', 'passed']
    assert calls == [('synthetic-vault', 'synthetic-item')]
    assert len(loaded_providers) == len(sessions) == 1
    assert loaded_providers[0].closed and loaded_providers[0].implementation.closed
    assert not loaded_providers[0].implementation.sessions
    assert not loaded_providers[0].implementation.browsers
    assert sessions[0]._closed and sessions[0]._key is None
    forbidden.assert_not_called()
    assert key not in recorded.execution.output
    artifacts = list(recorded.directory.glob('*-result.json'))
    assert len(artifacts) == 2
    assert all(json.loads(path.read_text())['status'] == 'passed' for path in artifacts)
    assert all(key not in path.read_text() for path in recorded.directory.rglob('*') if path.is_file())
