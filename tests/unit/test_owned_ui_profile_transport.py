"""Owned UI scenarios receive profiles without acquiring framework providers."""
import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from api_agent.testing.setup_pool import SetupPool
from test_integration_runner import context, fixture_repo


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_owned_ui_worker_receives_admitted_model_profile_without_setup(tmp_path, monkeypatch, mode):
    base = fixture_repo(tmp_path / 'base', '''import pytest
from api_agent.testing.provider_setup import _test_snapshot

@pytest.mark.requires_owned_ui_session
def test_ok(request):
    snapshot = _test_snapshot(request)
    assert snapshot['config']['LLM_DEFAULT_MODEL'] == 'gpt-6'
    assert snapshot['config']['LLM_E2E_TEST_MODEL'] == 'gpt-5.6-luna'
    assert snapshot['profile_provenance']['LLM_E2E_TEST_MODEL'] == 'pack_test_profile:test-profiles.toml:converted_shell'
    assert 'setup_backend' not in snapshot
''')
    ctx = context(base)
    ctx.suite_paths = lambda selected_mode: (base / 'tests/integration',)
    selected = collect_tests(ctx, mode)
    assert selected[0].tags == frozenset({'requires_owned_ui_session'})
    config = {'TUI_MODE': mode, 'LLM_DEFAULT_MODEL': 'gpt-6'}
    profiles = {test.id: {'node_id': test.node_id, 'repo': test.repo,
        'config': {**config, 'LLM_E2E_TEST_MODEL': 'gpt-5.6-luna'},
        'provenance': {'LLM_E2E_TEST_MODEL': 'pack_test_profile:test-profiles.toml:converted_shell'}}
        for test in selected}
    def forbidden_setup(*args, **kwargs):
        raise AssertionError('Owned UI scenarios must not acquire framework providers')
    monkeypatch.setattr(SetupPool, '__init__', forbidden_setup)

    result = run_integration_tests(ctx, selected, accepted=True, config=config,
                                   resolved_test_profiles=profiles)

    assert result.exit_code == 0, result.output
    assert result.tests[0].status == 'passed', result.tests[0].traceback
    assert config == {'TUI_MODE': mode, 'LLM_DEFAULT_MODEL': 'gpt-6'}
