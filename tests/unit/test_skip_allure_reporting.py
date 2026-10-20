from types import SimpleNamespace

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
