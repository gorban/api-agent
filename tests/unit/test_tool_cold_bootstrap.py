"""Cold graph-aware launch uses core dependency preparation before parsing."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.runtime.bootstrap import _prepare_workflow_discovery
from test_tool_selection import selection_fixture


def test_graphless_launcher_never_prepares_parser_dependencies(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    prepare = Mock()
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)

    assert _prepare_workflow_discovery(SimpleNamespace(base_root=tmp_path), ['--help']) is None

    prepare.assert_not_called()


def test_installed_parser_packages_need_no_launch_preparation(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, tool_catalog_path=tmp_path / 'catalog.toml')
    prepare = Mock()
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)

    assert _prepare_workflow_discovery(context, ['--help']) is None

    prepare.assert_not_called()


@pytest.mark.parametrize('option,missing', [('--help', 'jsonschema'), ('--tui-mode=integration', 'langgraph')])
def test_missing_core_parser_package_hands_off_before_discovery(tmp_path, monkeypatch, option, missing):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, workflow_entry=tmp_path / 'flow.md.html')
    monkeypatch.setattr('importlib.import_module', Mock(side_effect=ModuleNotFoundError(name=missing)))
    prepare = Mock(return_value=7)
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)

    assert _prepare_workflow_discovery(context, [option], replace_process=True) == 7

    prepare.assert_called_once_with(context, [option], groups=(), replace_process=True)


def test_discovery_preparation_error_is_not_misreported_as_a_plugin_failure(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, workflow_entry=tmp_path / 'flow.md.html')
    monkeypatch.setattr('importlib.import_module', Mock(side_effect=ModuleNotFoundError(name='jsonschema')))
    monkeypatch.setattr(dependencies, 'ensure_environment', Mock(side_effect=OSError('core packages unavailable')))

    with pytest.raises(OSError, match='core packages unavailable'):
        _prepare_workflow_discovery(context, [])


def test_ready_environment_retries_the_core_import_once_after_preparation(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, workflow_entry=tmp_path / 'flow.md.html')
    load = Mock(side_effect=[ModuleNotFoundError(name='typing_extensions'), object()])
    monkeypatch.setattr('importlib.import_module', load)
    prepare = Mock(return_value=None)
    monkeypatch.setattr(dependencies, 'ensure_environment', prepare)

    assert _prepare_workflow_discovery(context, []) is None

    assert load.call_count == 2
    assert all(call.args == ('api_agent.workflows.definition',) for call in load.call_args_list)
    prepare.assert_called_once_with(context, [], groups=())


def test_unrepaired_core_import_failure_survives_the_single_retry(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, workflow_entry=tmp_path / 'flow.md.html')
    load = Mock(side_effect=ModuleNotFoundError(name='jsonschema'))
    monkeypatch.setattr('importlib.import_module', load)
    monkeypatch.setattr(dependencies, 'ensure_environment', Mock(return_value=None))

    with pytest.raises(ModuleNotFoundError):
        _prepare_workflow_discovery(context, [])

    assert load.call_count == 2


def test_main_returns_cold_discovery_handoff_before_tool_composition(monkeypatch):
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv('NO_STD_IN', 'True')
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', Mock())
    monkeypatch.setattr('api_agent.runtime.bootstrap._prepare_workflow_discovery', Mock(return_value=17))
    compose = Mock(side_effect=AssertionError('tool discovery must follow dependency handoff'))
    monkeypatch.setattr('api_agent.config.tool_resolution.compose_operation_tools', compose)

    assert main(['--help'], base_root=root) == 17
    compose.assert_not_called()


@pytest.mark.parametrize('mode,provided', [('unit', False), ('unit', True), ('integration', False), ('integration', True)])
def test_terminal_exact_selection_validates_required_tool_inputs(selection_fixture, monkeypatch, capsys, mode, provided):
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    from api_agent.config import load_shared_schema
    from api_agent.testing.collection import TestIdentity
    from test_tool_config_session import DUAL
    from dataclasses import replace
    context, _ = selection_fixture
    context = replace(context, launcher_root=context.base_root)
    context.config_path.write_text("version=1\n")
    root = Path(__file__).resolve().parents[2]
    selected = [TestIdentity(DUAL, 'base', 'tests/test_flow.py', 'tests/test_flow.py::test_dual')]
    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', Mock(return_value=context))
    monkeypatch.setattr('api_agent.config.load_shared_schema', lambda _: load_shared_schema(root))
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', Mock())
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', Mock())
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', Mock(return_value=None))
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', Mock(return_value=selected))
    monkeypatch.setattr('api_agent.config.read_dotenv', Mock(return_value={}))
    monkeypatch.setattr('api_agent.config.dotenv.read_dotenv', Mock(return_value={}))
    monkeypatch.setattr('os.environ', {})
    run = Mock(return_value=SimpleNamespace(exit_code=0))
    monkeypatch.setattr('api_agent.testing.runner.run_unit_tests', run)
    monkeypatch.setattr('api_agent.testing.runner.format_unit_summary', Mock(return_value='synthetic summary'))
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    if mode == 'unit':
        from api_agent.config.tool_resolution import bind_operation_tools
        def bind_selected(plan, result, **kwargs):
            bound = bind_operation_tools(plan, result, **kwargs)
            if plan.selection.mode == 'unit' and not provided:
                return replace(bound, missing=('API_ENDPOINT', 'API_2_ENDPOINT'))
            return bound
        monkeypatch.setattr('api_agent.config.tool_resolution.bind_operation_tools', bind_selected)
    args = ['--tui-mode=' + mode, '--no-std-in', '--start', '--skip-allure-reporting', DUAL]
    if provided and mode != 'unit':
        args += ['--api-endpoint=https://one.invalid', '--api-2-endpoint=https://two.invalid']

    status = main(args, base_root=context.base_root)

    if provided:
        assert status == 0
        run.assert_called_once()
        assert run.call_args.args[1] == selected
    else:
        assert status != 0
        run.assert_not_called()
        assert 'Missing required configuration: API_ENDPOINT, API_2_ENDPOINT' in capsys.readouterr().err


@pytest.mark.parametrize('mode,ui,provided', [('unit', False, False), ('integration', False, False), ('integration', True, False), ('integration', True, True)])
def test_terminal_legacy_context_validates_ui_intent_without_tool_plan(selection_fixture, monkeypatch, capsys, mode, ui, provided):
    from dataclasses import replace
    from pathlib import Path
    from api_agent.config import load_shared_schema
    from api_agent.runtime.bootstrap import main
    from api_agent.testing.collection import TestIdentity
    context, _ = selection_fixture
    context = replace(context, launcher_root=context.base_root, workflow_entry=None, tool_catalog_path=None)
    context.config_path.write_text('version=1\n[fields.UI_REQUIRED]\ntype="str"\nrequired=true\nacknowledge_default=false\n[fields.UI_REQUIRED.applies_when]\noperation="ui_session"\n')
    root = Path(__file__).resolve().parents[2]
    selected = [TestIdentity('base::test_case.py::test_ready', 'base', 'test_case.py', 'test_case.py::test_ready',
                             tags=frozenset({'requires_ui_session'}) if ui else frozenset())]
    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', Mock(return_value=context))
    monkeypatch.setattr('api_agent.config.load_shared_schema', lambda _: load_shared_schema(root))
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', Mock())
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', Mock())
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', Mock(return_value=None))
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', Mock(return_value=selected))
    monkeypatch.setattr('api_agent.config.read_dotenv', Mock(return_value={}))
    monkeypatch.setattr('api_agent.config.dotenv.read_dotenv', Mock(return_value={}))
    monkeypatch.setattr('os.environ', {})
    run = Mock(return_value=SimpleNamespace(exit_code=0))
    monkeypatch.setattr('api_agent.testing.runner.run_unit_tests', run)
    monkeypatch.setattr('api_agent.testing.runner.format_unit_summary', Mock(return_value='synthetic summary'))
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', run)
    args = ['--tui-mode=' + mode, '--no-std-in', '--start', '--skip-allure-reporting', 'test_case.py']
    if provided:
        args.append('--ui-required=ready')

    status = main(args, base_root=context.base_root)

    if ui and not provided:
        assert status != 0
        run.assert_not_called()
        assert 'Missing required configuration: UI_REQUIRED' in capsys.readouterr().err
    else:
        assert status == 0
        run.assert_called_once()
        assert run.call_args.args[1] == selected
        if ui:
            assert run.call_args.args[2]['UI_REQUIRED'] == 'ready'
