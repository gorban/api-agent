"""Viewer operations reuse their standalone launch owners."""
import asyncio
from unittest.mock import patch

import pytest

from api_agent.runtime.bootstrap import main
from api_agent.runtime import trace_viewer
from test_tui import fixture


@pytest.mark.parametrize('mode,module,runner', [
    ('view-flow', 'view_flow', 'run_view_flow'),
    ('trace-viewer', 'trace_viewer', 'run_trace_viewer'),
])
def test_terminal_viewer_modes_bypass_unrelated_configuration(mode, module, runner):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.worktrees.verify_worktrees'), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.runtime.trace_viewer.resolve_viewer_config', return_value={}), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch(f'api_agent.runtime.{module}.{runner}', return_value=0) as run, \
            patch('api_agent.providers.loading.bootstrap_schema', side_effect=AssertionError('unrelated provider setup')):
        assert main(['--tui-mode', mode, '--no-std-in'], base_root=root) == 0
    run.assert_called_once()


@pytest.mark.parametrize('mode', ['view-flow', 'trace-viewer'])
def test_menu_viewer_hands_off_without_configuration(tmp_path, mode):
    app = fixture(tmp_path)
    app.mode = mode
    with patch.object(app, 'exit') as exit_app:
        asyncio.run(app.enter_mode())
    exit_app.assert_called_once_with(mode)


def test_trace_startup_error_never_exposes_unregistered_chained_secrets(monkeypatch, capsys):
    async def fail(*args):
        try:
            raise ValueError('external-token-unknown')
        except ValueError as error:
            raise RuntimeError('other-unregistered-secret') from error
    monkeypatch.setattr(trace_viewer, 'view_trace', fail)
    assert trace_viewer.run_trace_viewer(None, {}) == 2
    output = capsys.readouterr().out
    assert 'Trace viewer could not start' in output
    assert 'external-token-unknown' not in output
    assert 'other-unregistered-secret' not in output


@pytest.mark.parametrize('mode', ['view-flow', 'trace-viewer'])
@pytest.mark.parametrize('option,expected', [('--help', 0), ('--collect-only', 2)])
def test_viewer_help_and_collection_do_not_start_resources(mode, option, expected):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.runtime.worktrees.verify_worktrees'), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode', mode, option], base_root=root) == expected


@pytest.mark.parametrize('option', ['--collect-only=true', '--collect_only=True', '--COLLECT_ONLY=True'])
def test_viewer_collect_true_spelling_never_starts(option):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode=view-flow', option], base_root=root, _worktrees_verified=True) == 2


def test_viewer_collect_cli_false_overrides_environment():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {'COLLECT_ONLY': 'True'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.runtime.view_flow.run_view_flow', return_value=0) as run:
        assert main(['--tui-mode=view-flow', '--collect-only=false'], base_root=root, _worktrees_verified=True) == 0
    run.assert_called_once()


def test_viewer_collect_environment_overrides_dotenv():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {'COLLECT_ONLY': 'True'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={'COLLECT_ONLY': 'False'}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode=view-flow'], base_root=root, _worktrees_verified=True) == 2


def test_menu_handoff_discards_prior_operation_arguments():
    from api_agent.runtime.bootstrap import _viewer_handoff_arguments
    from api_agent.config import load_shared_schema
    schema = load_shared_schema()
    argv = ['--skip-unattended-setup=True', '--start=False', '--debug=True', '--', '--trace-viewer']
    assert _viewer_handoff_arguments(schema, argv, 'view-flow') == ['--debug=True']
    assert '--start=False' not in _viewer_handoff_arguments(schema, argv, 'trace-viewer')
    assert '--trace-viewer' not in _viewer_handoff_arguments(schema, argv, 'trace-viewer')


def test_viewer_collect_dotenv_blocks_and_help_does_not_start_resources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={'COLLECT_ONLY': 'True'}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode=view-flow'], base_root=root, _worktrees_verified=True) == 2
        assert main(['--tui-mode=view-flow', '--help'], base_root=root, _worktrees_verified=True) == 0


def test_trace_menu_handoff_preserves_tracing_options():
    from api_agent.runtime.bootstrap import _viewer_handoff_arguments
    from api_agent.config import load_shared_schema
    schema = load_shared_schema()
    argv = ['--trace-provider=example.toml', '--start=False', 'old_test', '--', '--trace-viewer']
    assert _viewer_handoff_arguments(schema, argv, 'trace-viewer') == ['--trace-provider=example.toml']


@pytest.mark.parametrize('mode', ['view-flow', 'trace-viewer'])
def test_menu_handoff_preserves_explicit_runtime_override(mode, tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    stale = str(root / 'missing-runtime')
    (tmp_path / 'pack.toml').write_text('version = 1\nid = "viewer_test"\nconfig = "config.toml"\n')
    (tmp_path / 'config.toml').write_text('version = 1\n')
    (tmp_path / '.env').write_text(f'API_AGENT_DIR={stale}\n')
    with patch.dict('os.environ', {'API_AGENT_DIR': stale}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={'API_AGENT_DIR': stale}), \
            patch('sys.stdin.isatty', return_value=True), \
            patch('sys.stdout.isatty', return_value=True), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.tui.app.run_tui', return_value=mode), \
            patch('api_agent.runtime.trace_viewer.resolve_viewer_config', return_value={}), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch('api_agent.runtime.view_flow.run_view_flow', return_value=0) as flow, \
            patch('api_agent.runtime.trace_viewer.run_trace_viewer', return_value=0) as trace:
        assert main(['--api-agent-dir', str(root)], base_root=root, resource_pack_dir=tmp_path,
                    launcher_root=tmp_path, _worktrees_verified=True) == 0
    (flow if mode == 'view-flow' else trace).assert_called_once()


@pytest.mark.parametrize('mode,runner', [('view-flow', 'view_flow'), ('trace-viewer', 'trace_viewer')])
@pytest.mark.parametrize('source', ['environment', 'dotenv'])
def test_configured_noninteractive_viewer_mode_bypasses_application_setup(mode, runner, source):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    configured = {'TUI_MODE': mode, 'NO_STD_IN': 'True'}
    environ = configured if source == 'environment' else {}
    dotenv = configured if source == 'dotenv' else {}
    with patch.dict('os.environ', environ, clear=True), \
            patch('api_agent.config.read_dotenv', return_value=dotenv), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.runtime.trace_viewer.resolve_viewer_config', return_value={}), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch(f'api_agent.runtime.{runner}.run_{runner}', return_value=0) as run, \
            patch('api_agent.providers.loading.bootstrap_schema', side_effect=AssertionError('application setup')):
        assert main([], base_root=root, _worktrees_verified=True) == 0
    run.assert_called_once()


@pytest.mark.parametrize('alias', ['--view-flow', '--trace-viewer'])
@pytest.mark.parametrize('source', ['cli', 'environment', 'dotenv'])
def test_viewer_alias_collection_never_starts_resources(alias, source, capsys):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    options = ['--collect-only=True'] if source == 'cli' else []
    environ = {'COLLECT_ONLY': 'True'} if source == 'environment' else {}
    dotenv = {'COLLECT_ONLY': 'True'} if source == 'dotenv' else {}
    with patch.dict('os.environ', environ, clear=True), \
            patch('api_agent.config.read_dotenv', return_value=dotenv), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main([alias, *options], base_root=root, _worktrees_verified=True) == 2
        assert 'COLLECT_ONLY requires' in capsys.readouterr().err
        assert main([alias, *options, '--help'], base_root=root, _worktrees_verified=True) == 0


@pytest.mark.parametrize('field', ['LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER'])
def test_trace_resolver_ignores_unrelated_invalid_provider(field):
    from pathlib import Path
    from api_agent.config import load_shared_schema
    root = Path(__file__).resolve().parents[2]
    result = trace_viewer.resolve_viewer_config(load_shared_schema(root), argv=(),
        environ={field: '/missing/provider.py:factory', 'TRACE_MODE': 'OTEL'}, dotenv={},
        anchors={'owner_root': root, 'base_root': root, 'pack_root': root})
    assert result['TRACE_MODE'] == 'OTEL'


def test_trace_resolver_rejects_invalid_selected_provider():
    from pathlib import Path
    from api_agent.config import load_shared_schema
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError):
        trace_viewer.resolve_viewer_config(load_shared_schema(root), argv=(),
            environ={'TRACE_PROVIDER': '/missing/provider.py:factory'}, dotenv={},
            anchors={'owner_root': root, 'base_root': root, 'pack_root': root})


@pytest.mark.parametrize('mode', ['view-flow', 'trace-viewer'])
@pytest.mark.parametrize('source', ['environment', 'dotenv'])
@pytest.mark.parametrize('help_option', ['--help', '-h', '--HELP=True', '--help=True'])
def test_configured_viewer_help_precedes_collection(mode, source, help_option):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    configured = {'TUI_MODE': mode, 'NO_STD_IN': 'True', 'COLLECT_ONLY': 'True'}
    environ = configured if source == 'environment' else {}
    dotenv = configured if source == 'dotenv' else {}
    with patch.dict('os.environ', environ, clear=True), \
            patch('api_agent.config.read_dotenv', return_value=dotenv), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main([help_option], base_root=root, _worktrees_verified=True) == 0


def test_trace_menu_handoff_filters_other_provider_options_and_keeps_trace_options():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('sys.stdin.isatty', return_value=True), \
            patch('sys.stdout.isatty', return_value=True), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.tui.app.run_tui', return_value='trace-viewer'), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch('api_agent.runtime.trace_viewer.run_trace_viewer', return_value=0) as run:
        assert main(['--gpt-auth-mode=api_key', '--trace-mode=OTEL', '--jaeger-viewer-port=16687'],
                    base_root=root, _worktrees_verified=True) == 0
    assert run.call_args.args[1]['TRACE_MODE'] == 'OTEL'
    assert run.call_args.args[1]['JAEGER_VIEWER_PORT'] == 16687


@pytest.mark.parametrize('mode,runner', [('view-flow', 'view_flow'), ('trace-viewer', 'trace_viewer')])
@pytest.mark.parametrize('entry', ['mode', 'alias'])
@pytest.mark.parametrize('help_args', [['--help', 'False'], ['--help=False']])
def test_viewer_cli_help_false_overrides_inherited_true(mode, runner, entry, help_args):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    select = ['--tui-mode=' + mode] if entry == 'mode' else ['--' + mode]
    with patch.dict('os.environ', {'HELP': 'True'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.runtime.trace_viewer.resolve_viewer_config', return_value={}), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch(f'api_agent.runtime.{runner}.run_{runner}', return_value=0) as run:
        assert main([*select, *help_args], base_root=root, _worktrees_verified=True) == 0
    run.assert_called_once()


@pytest.mark.parametrize('mode', ['view-flow', 'trace-viewer'])
@pytest.mark.parametrize('entry', ['mode', 'alias'])
@pytest.mark.parametrize('help_args', [['--help', 'True'], ['--help=True']])
def test_viewer_cli_help_true_is_lazy(mode, entry, help_args):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    select = ['--tui-mode=' + mode] if entry == 'mode' else ['--' + mode]
    with patch.dict('os.environ', {'HELP': 'False', 'LLM_PROVIDER': '/invalid/provider.py:factory'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')), \
            patch('api_agent.providers.loading.bootstrap_schema', side_effect=AssertionError('application setup')):
        assert main([*select, *help_args], base_root=root, _worktrees_verified=True) == 0


@pytest.mark.parametrize('mode,runner', [('view-flow', 'view_flow'), ('trace-viewer', 'trace_viewer')])
@pytest.mark.parametrize('name', ['--TUI_MODE', '--Tui-Mode', '--tui_mode'])
@pytest.mark.parametrize('joined', [True, False])
def test_viewer_mode_uses_canonical_option_spelling(mode, runner, name, joined):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    arguments = [name + '=' + mode] if joined else [name, mode]
    with patch.dict('os.environ', {'LLM_PROVIDER': '/invalid/provider.py:factory', 'TRACE_MODE': 'OTEL'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.providers.distribution.selected_manifests', return_value=[]), \
            patch(f'api_agent.runtime.{runner}.run_{runner}', return_value=0) as run:
        assert main([*arguments, '--no-std-in'], base_root=root, _worktrees_verified=True) == 0
    run.assert_called_once()
    if mode == 'trace-viewer':
        assert run.call_args.args[1]['TRACE_MODE'] == 'OTEL'


def test_viewer_mode_duplicate_normalized_spellings_are_rejected():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--TUI_MODE=view-flow', '--tui-mode=trace-viewer'],
                    base_root=root, _worktrees_verified=True) == 2


def test_viewer_mode_option_after_separator_is_not_selected():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    observed = []
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('sys.stdin.isatty', return_value=True), \
            patch('sys.stdout.isatty', return_value=True), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=lambda context, argv, **kw: observed.append(argv) or 73), \
            patch('api_agent.runtime.view_flow.run_view_flow', side_effect=AssertionError('viewer selected')):
        assert main(['--tui-mode=unit', '--', '--TUI_MODE=view-flow'],
                    base_root=root, _worktrees_verified=True) == 73
    assert observed == [['--tui-mode=unit', '--', '--TUI_MODE=view-flow']]


def test_viewer_mode_rejects_missing_value():
    from api_agent.runtime.bootstrap import _extract_tui_mode
    with pytest.raises(ValueError, match='requires a value'):
        _extract_tui_mode(['--TUI_MODE'])


@pytest.mark.parametrize('alias', ['--view-flow', '--trace-viewer'])
def test_viewer_mode_conflicts_with_standalone_flag(alias):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode=view-flow', alias], base_root=root, _worktrees_verified=True) == 2


def test_viewer_mode_selector_after_separator_is_not_help(capsys):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.runtime.dependencies.ensure_environment', side_effect=AssertionError('started resources')):
        assert main(['--tui-mode=trace-viewer', '--trace-mode=OTEL', '--', '--help'], base_root=root, _worktrees_verified=True) == 2
    assert 'Usage:' not in capsys.readouterr().out


@pytest.mark.parametrize('mode_option', [[], ['--tui-mode=unit']])
def test_configured_viewer_mode_can_be_overridden_or_remain_interactive(mode_option):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {'TUI_MODE': 'view-flow', 'NO_STD_IN': 'False'}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('sys.stdin.isatty', return_value=True), \
            patch('sys.stdout.isatty', return_value=True), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=73), \
            patch('api_agent.runtime.view_flow.run_view_flow', side_effect=AssertionError('viewer selected')):
        assert main(mode_option, base_root=root, _worktrees_verified=True) == 73


@pytest.mark.parametrize('error,hint', [
    (trace_viewer.ConfigError('unregistered-secret'), 'selected tracing provider configuration'),
    (trace_viewer.DashboardError('unregistered-secret'), 'dashboard URL and managed collector readiness'),
])
def test_trace_viewer_typed_failure_hints_are_safe(monkeypatch, capsys, error, hint):
    async def fail(*args):
        raise error
    monkeypatch.setattr(trace_viewer, 'view_trace', fail)
    assert trace_viewer.run_trace_viewer(None, {}) == 2
    output = capsys.readouterr().out
    assert hint in output
    assert 'unregistered-secret' not in output
