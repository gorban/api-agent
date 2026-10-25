"""Offline terminal dispatch retains compatible named-tool/provider configuration."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime import bootstrap

CASE = ('api-agent::tests/integration/test_useful_demo_graph.py::'
        'test_selected_graph_relaxes_preference_only_after_retry_and_renders_partial_evidence')


@pytest.fixture
def isolated_terminal(monkeypatch, tmp_path):
    import os
    from api_agent import config
    from api_agent.runtime import dependencies, hook_assurance, worktrees
    root = Path(__file__).resolve().parents[2]
    trace = []
    monkeypatch.setattr(os, 'environ', {'HOME': str(tmp_path), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})
    monkeypatch.setattr(config, 'read_dotenv', lambda path: {})
    monkeypatch.setattr(hook_assurance, 'assure_hook', lambda path: trace.append(('hook', path)))
    monkeypatch.setattr(worktrees, 'verify_worktrees', lambda roots, **kwargs: trace.append(('worktrees', tuple(roots))))
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda context, argv, **kwargs:
                        trace.append(('dependencies', tuple(argv), kwargs)))
    return root, trace


def arguments(root):
    provider = root / 'src/api_agent/api_providers/graphql_mcp/factory.py'
    return ('--tui-mode', 'integration', '--no-std-in', '--start',
            '--skip-allure-reporting', '--api-provider', str(provider) + ':factory', '--api-endpoint', 'https://example.invalid/graphql', CASE)


@pytest.mark.parametrize('all_tests', [False, True])
def test_terminal_selection_reaches_runner_with_compatible_provider_headers(isolated_terminal, monkeypatch, all_tests):
    from api_agent.testing import launched
    root, trace = isolated_terminal
    argv = arguments(root)[:-1] if all_tests else arguments(root)

    def run(context, tests, values, **kwargs):
        plan = kwargs['tool_plan']
        identities = tuple(test.id for test in tests)
        assert CASE in identities
        assert plan.selection.test_ids == identities
        if all_tests:
            assert len(identities) > 1
            assert {tool.id for tool in plan.tools} == {'demo/primary', 'fixture/primary', 'fixture/shared-secondary'}
            assert {tool.id for tool in plan.for_case(CASE).tools} == {'demo/primary'}
        else:
            assert identities == (CASE,)
        assert plan.tools[0].bindings == {'ENDPOINT': 'API_ENDPOINT', 'HEADERS': 'API_HEADERS'}
        assert plan.schema.fields['API_HEADERS'].help.startswith('Owner-supplied HTTP headers for the selected GraphQL endpoint.')
        assert plan.schema.fields['API_HEADERS'].default_source == 'pack_default'
        assert values['API_ENDPOINT'] == 'https://example.invalid/graphql'
        assert plan.config.fields['API_ENDPOINT'].source == 'cli'
        assert plan.config.fields['API_HEADERS'].source == 'pack_default'
        assert plan.config.fields['TUI_MODE'].source == 'cli'
        assert plan.config.fields['NO_STD_IN'].source == 'cli'
        assert plan.config.fields['START'].source == 'cli'
        assert kwargs['accepted'] is True
        trace.append(('runner', tuple(test.id for test in tests)))
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(launched, 'run_launched_integration', run)

    status = bootstrap.main(argv, base_root=root)

    assert status == 0
    assert trace[-1][0] == 'runner'
    assert CASE in trace[-1][1]
    assert ('dependencies', argv, {'collect': True, 'groups': ('dev',)}) in trace


def test_terminal_collection_preserves_exact_literal_selector_without_execution(isolated_terminal, monkeypatch, capsys):
    from api_agent.testing import launched
    root, trace = isolated_terminal
    argv = (*arguments(root), '--collect-only')
    monkeypatch.setattr(launched, 'run_launched_integration', lambda *args, **kwargs:
                        pytest.fail('Collection crossed the execution boundary'))

    status = bootstrap.main(argv, base_root=root)

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [CASE]
    assert ('dependencies', argv, {'collect': True}) in trace
