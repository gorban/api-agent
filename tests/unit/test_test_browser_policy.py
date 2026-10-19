"""Operation configuration cannot turn ordinary test browsers into inspection."""
import pytest
from api_agent.config import load_shared_schema, resolve_config
from api_agent.runtime.bootstrap import generated_help
from api_agent.testing.debug_confirmation import debug_warning
from api_agent.api.composition import activation_kinds
from types import SimpleNamespace


@pytest.mark.parametrize('mode', ['integration','e2e'])
@pytest.mark.parametrize('source', ['dotenv','environ','cli'])
def test_test_modes_ignore_local_frontend_flag_and_keep_debug_ownership(tmp_path,mode,source):
    values={'TUI_MODE':mode,'ALLURE_S3_BUCKET':'synthetic-results'}
    kwargs={'environ':values,'anchors':{'owner_root':tmp_path}}
    if source == 'cli':
        kwargs['argv']=['--run-ui=True']
    else:
        kwargs[source]={**(values if source=='environ' else {}),'RUN_UI':'True'}
    result=resolve_config(load_shared_schema(),**kwargs)
    result.raise_for_errors()
    assert 'RUN_UI' not in result.values
    assert 'RUN_UI' not in result.fields
    assert result.values['DEBUG'] is False
    assert ('ui' in activation_kinds(result.values)) is (mode=='e2e')


@pytest.mark.parametrize('mode',['run-local','integration','e2e'])
def test_debug_remains_explicitly_configurable_without_frontend_flag(tmp_path,mode):
    result=resolve_config(load_shared_schema(),environ={'TUI_MODE':mode,'DEBUG':'True','ALLURE_S3_BUCKET':'synthetic-results'},anchors={'owner_root':tmp_path})
    result.raise_for_errors()
    assert result.values['DEBUG'] is True
    assert result.fields['DEBUG'].explicit
    assert result.values.get('RUN_UI',False) is False


def test_help_explains_optional_local_frontend_and_debug_only_test_visibility():
    schema=load_shared_schema()
    help_text=generated_help(schema)
    assert '--run-ui: enable the optional Run local frontend' in help_text
    assert 'test browsers stay headless unless DEBUG' in help_text
    assert 'E2E_DEBUG for E2E' in help_text
    assert 'Run local' in schema.fields['RUN_UI'].help
    assert 'E2E always requires its frontend' in schema.fields['RUN_UI'].help


@pytest.mark.parametrize('mode',['integration','e2e'])
def test_run_ui_alone_does_not_authorize_multi_browser_debug_warning(mode):
    tests=[SimpleNamespace(id='first'),SimpleNamespace(id='second')]
    assert debug_warning(mode,False,tests) is None
    assert '2 selected tests' in debug_warning(mode,True,tests)
