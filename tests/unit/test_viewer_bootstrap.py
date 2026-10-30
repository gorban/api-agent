"""Standalone viewer switch is selected before execution configuration."""
import pytest
from pathlib import Path

from api_agent.runtime.bootstrap import _extract_view_flow, _validate_view_flow_arguments, main


ROOT = Path(__file__).resolve().parents[2]


def test_extracts_one_flag_without_changing_pack_arguments():
    remaining, selected = _extract_view_flow(['--resource-pack-dir', '/pack', '--view-flow', '--debug'])
    assert selected is True
    assert remaining == ['--resource-pack-dir', '/pack', '--debug']


@pytest.mark.parametrize('arguments', [
    ['--view-flow', '--view-flow'], ['--view-flow=yes'],
])
def test_rejects_duplicate_or_valued_flag(arguments):
    with pytest.raises(ValueError):
        _extract_view_flow(arguments)


@pytest.mark.parametrize('arguments', [
    ['--debug'], ['--no-std-in'], ['--debug=False', '--no-std-in=True'], ['--help'],
])
def test_accepts_harmless_lifecycle_flags(arguments):
    _validate_view_flow_arguments(arguments)


def test_accepts_one_flow_path_relative_to_callers_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _validate_view_flow_arguments(['--debug', 'nested/flow.md.html']) == tmp_path / 'nested/flow.md.html'
    with pytest.raises(ValueError, match='one optional'):
        _validate_view_flow_arguments(['one.md.html', 'two.md.html'])


def test_explicit_flow_survives_dependency_handoff_and_reaches_viewer(monkeypatch, tmp_path):
    from api_agent.runtime import dependencies, hook_assurance, view_flow
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hook_assurance, 'assure_hook', lambda root: 'present')
    calls = []
    monkeypatch.setattr(dependencies, 'ensure_environment', lambda context, arguments, **kwargs: calls.append(arguments))
    viewed = []
    monkeypatch.setattr(view_flow, 'run_view_flow', lambda context, *, entry: viewed.append(entry) or 0)
    assert main(['--view-flow', 'nested/flow.md.html'], base_root=ROOT, _worktrees_verified=True) == 0
    assert calls == [['--view-flow', 'nested/flow.md.html']]
    assert viewed == [tmp_path / 'nested/flow.md.html']


@pytest.mark.parametrize('arguments', [
    ['--collect-only'], ['--tui-mode=unit'], ['tests/unit'], ['--', 'selector'], ['--start'],
])
def test_rejects_execution_and_test_selection(arguments):
    with pytest.raises(ValueError):
        _validate_view_flow_arguments(arguments)


def test_viewer_help_does_not_load_execution_schema_or_providers(monkeypatch, capsys):
    import api_agent.config as config
    from api_agent.providers import loading
    from api_agent.runtime import hook_assurance

    def poison(*args, **kwargs):
        raise AssertionError('normal execution bootstrap reached')

    monkeypatch.setattr(config, 'load_shared_schema', poison)
    monkeypatch.setattr(loading, 'bootstrap_schema', poison)
    monkeypatch.setattr(hook_assurance, 'assure_hook', lambda root: 'present')
    assert main(['--view-flow', '--help'], base_root=ROOT, _worktrees_verified=True) == 0
    assert './launch.sh --view-flow' in capsys.readouterr().out




def test_viewer_dependency_handoff_preserves_selected_context_and_arguments(monkeypatch):
    import api_agent.config as config
    from api_agent.providers import loading
    from api_agent.runtime import dependencies, hook_assurance, view_flow

    def poison(*args, **kwargs):
        raise AssertionError('execution bootstrap or viewer started during dependency handoff')

    prepared = []

    def handoff(context, arguments, *, groups, replace_process):
        prepared.append((context, arguments, groups, replace_process))
        return 7

    monkeypatch.setattr(config, 'load_shared_schema', poison)
    monkeypatch.setattr(loading, 'bootstrap_schema', poison)
    monkeypatch.setattr(hook_assurance, 'assure_hook', lambda root: 'present')
    monkeypatch.setattr(dependencies, 'ensure_environment', handoff)
    monkeypatch.setattr(view_flow, 'run_view_flow', poison)

    assert main(['--view-flow', '--debug', '--no-std-in'], base_root=ROOT,
                _worktrees_verified=True, _replace_process=True) == 7
    context, arguments, groups, replace_process = prepared.pop()
    assert context.base_root == ROOT
    assert context.pack_root == ROOT / 'demo'
    assert arguments == ['--view-flow', '--debug', '--no-std-in']
    assert groups == ('browser',)
    assert replace_process is True
    assert prepared == []


def test_prepared_viewer_propagates_interrupt_status_without_execution_bootstrap(monkeypatch):
    import api_agent.config as config
    from api_agent.providers import loading
    from api_agent.runtime import dependencies, hook_assurance, view_flow

    def poison(*args, **kwargs):
        raise AssertionError('normal execution bootstrap reached')

    prepared = []
    viewed = []

    def prepare(context, arguments, *, groups):
        prepared.append((context, arguments, groups))
        return None

    def run(context):
        viewed.append(context)
        return 130

    monkeypatch.setattr(config, 'load_shared_schema', poison)
    monkeypatch.setattr(loading, 'bootstrap_schema', poison)
    monkeypatch.setattr(hook_assurance, 'assure_hook', lambda root: 'present')
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)
    monkeypatch.setattr(view_flow, 'run_view_flow', run)

    assert main(['--no-std-in', '--view-flow', '--debug'], base_root=ROOT,
                _worktrees_verified=True) == 130
    context, arguments, groups = prepared.pop()
    assert context.base_root == ROOT
    assert context.pack_root == ROOT / 'demo'
    assert arguments == ['--view-flow', '--no-std-in', '--debug']
    assert groups == ('browser',)
    assert viewed == [context]
    assert prepared == []
