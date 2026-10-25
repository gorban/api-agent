from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.launched import run_launched_integration


def test_skip_routes_accepted_integration_without_reporting_setup(monkeypatch, tmp_path):
    from api_agent.testing import integration, launched
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    calls = []

    def execute(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(exit_code=0, tests=(), cancelled=False, inspections=())

    monkeypatch.setattr(integration, 'run_integration_tests', execute)
    monkeypatch.setattr(launched, 'local_recording_prerequisites',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('reporting started')))
    result = run_launched_integration(context, (test,),
        {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True)
    assert result.reporting_skipped is True
    assert result.manifest is None and result.directory is None and result.exit_code == 0
    assert len(calls) == 1 and calls[0]['accepted'] is True


def test_skip_option_uses_boolean_cli_and_configuration_precedence():
    from api_agent.config import load_shared_schema, resolve_config
    schema = load_shared_schema()
    result = resolve_config(schema, argv=['--tui-mode=integration', '--skip-allure-reporting'],
                            environ={'SKIP_ALLURE_REPORTING': 'False'},
                            dotenv={'SKIP_ALLURE_REPORTING': 'False'})
    assert result.values['SKIP_ALLURE_REPORTING'] is True
    assert result.fields['SKIP_ALLURE_REPORTING'].source == 'cli'
    assert 'ALLURE_S3_BUCKET' not in result.missing
    result = resolve_config(schema, argv=['--tui-mode=integration', '--skip-allure-reporting=False'],
                            environ={'SKIP_ALLURE_REPORTING': 'True'})
    assert result.values['SKIP_ALLURE_REPORTING'] is False
    assert resolve_config(schema, environ={'TUI_MODE': 'integration',
        'SKIP_ALLURE_REPORTING': 'bad'}).errors


def test_skip_reporting_still_prepares_selected_tool_dependencies(monkeypatch, tmp_path):
    from api_agent.testing import integration, prerequisites
    test = TestIdentity('owner::test.py::test_one', 'owner', 'test.py', 'test.py::test_one')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    plan = object()
    prepared = Mock()
    monkeypatch.setattr(prerequisites, 'prepare_selected_tool_dependencies', prepared)
    execute = Mock(return_value=SimpleNamespace(exit_code=0, tests=(), cancelled=False, inspections=()))
    monkeypatch.setattr(integration, 'run_integration_tests', execute)

    result = run_launched_integration(context, (test,),
        {'SKIP_ALLURE_REPORTING': True, 'TUI_MODE': 'integration'}, accepted=True, tool_plan=plan)

    prepared.assert_called_once_with(context, plan)
    assert execute.call_args.kwargs['tool_plan'] is plan
    assert result.reporting_skipped


@pytest.mark.parametrize('active,status', [(False, None), (True, 0), (True, None)])
def test_tool_dependency_preparation_requires_active_ready_environment(monkeypatch, tmp_path, active, status):
    from api_agent.runtime import dependencies
    from api_agent.testing.prerequisites import prepare_selected_tool_dependencies
    from api_agent.tooling import services
    context = SimpleNamespace(base_root=tmp_path / 'base', pack_root=tmp_path / 'pack', external_pack=True)
    manifests = (object(),)
    monkeypatch.setattr(services, 'selected_tool_manifests', lambda plan: manifests)
    probe = Mock(return_value=active)
    install = Mock(return_value=status)
    monkeypatch.setattr(dependencies, 'active_environment', probe)
    monkeypatch.setattr(dependencies, 'ensure_environment', install)

    if not active or status is not None:
        with pytest.raises(RuntimeError, match='interpreter_handoff_required'):
            prepare_selected_tool_dependencies(context, object())
    else:
        prepare_selected_tool_dependencies(context, object())

    probe.assert_called_once_with(context.pack_root / '.venv')
    if active:
        install.assert_called_once_with(context, (), groups=(), selected_provider_manifests=manifests)
    else:
        install.assert_not_called()


def test_no_selected_tool_dependencies_has_no_environment_effect(monkeypatch):
    from api_agent.runtime import dependencies
    from api_agent.testing.prerequisites import prepare_selected_tool_dependencies
    from api_agent.tooling import services
    monkeypatch.setattr(services, 'selected_tool_manifests', lambda plan: ())
    install = Mock()
    monkeypatch.setattr(dependencies, 'ensure_environment', install)

    prepare_selected_tool_dependencies(SimpleNamespace(), object())

    install.assert_not_called()
