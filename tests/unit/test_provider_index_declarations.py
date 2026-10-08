"""Selected providers can declare account selection without a secondary role."""
import pytest
from api_agent.config import compose_schema, load_shared_schema, resolve_config, schema_from_dict
from api_agent.runtime.bootstrap import generated_help

def selected_schema():
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_USER_INDEX': {
        'type': 'int', 'allow_empty': True, 'validation': 'nonnegative', 'acknowledge_default': False}}}, base_schema=shared, source='pack_default')
    return compose_schema(shared, extension)

def test_bare_core_does_not_expose_provider_selection():
    shared = load_shared_schema()
    assert 'UNATTENDED_USER_INDEX' not in shared.fields
    assert '--unattended-user-index' not in generated_help(shared)

def test_llm_only_declaration_exposes_generic_index_without_secondary_role():
    schema = selected_schema()
    assert 'UNATTENDED_SECONDARY_USER' not in schema.fields
    assert '--unattended-user-index' in generated_help(schema)
    result = resolve_config(schema, environ={'TUI_MODE': 'run-local'}, argv=['--unattended-user-index=1'])
    result.raise_for_errors()
    assert result.values['UNATTENDED_USER_INDEX'] == 1
    assert result.fields['UNATTENDED_USER_INDEX'].explicit
    assert resolve_config(schema, environ={'TUI_MODE': 'run-local'}).values.get('UNATTENDED_USER_INDEX') is None
    assert not schema.fields['UNATTENDED_USER_INDEX'].acknowledge_default

@pytest.mark.parametrize('index', ['-1', 'invalid'])
def test_invalid_declared_index_is_rejected(index):
    assert resolve_config(selected_schema(), environ={'TUI_MODE': 'run-local'}, argv=['--unattended-user-index=' + index]).errors


def test_empty_cli_index_overrides_saved_and_environment_selection(tmp_path):
    path = tmp_path / '.env'
    path.write_text('UNATTENDED_USER_INDEX=2\n')
    result = resolve_config(selected_schema(), dotenv={'UNATTENDED_USER_INDEX': '2'},
        environ={'TUI_MODE': 'run-local', 'UNATTENDED_USER_INDEX': '1'},
        argv=['--unattended-user-index='])
    result.raise_for_errors()
    assert result.values['UNATTENDED_USER_INDEX'] is None
    assert result.fields['UNATTENDED_USER_INDEX'].explicit
    assert result.fields['UNATTENDED_USER_INDEX'].source == 'cli'
    assert path.read_text() == 'UNATTENDED_USER_INDEX=2\n'


def test_optional_int_empty_only_when_declared_and_keeps_other_type_checks():
    from api_agent.config.resolve import convert_value
    field = selected_schema().fields['UNATTENDED_USER_INDEX']
    assert convert_value(field, '', external=True, anchors={}) is None
    assert convert_value(field, '', external=False, anchors={}) is None
    for value in [None, True, 1.0]:
        with pytest.raises(ValueError, match='expected int'):
            convert_value(field, value, external=False, anchors={})
    strict = schema_from_dict({'version': 1, 'fields': {'INDEX': {'type': 'int'}}}).fields['INDEX']
    with pytest.raises(ValueError, match='expected integer'):
        convert_value(strict, '', external=True, anchors={})


@pytest.mark.parametrize('setting,expected', [(None, False), ('False', False), ('True', True)])
def test_integration_keeps_run_ui_for_login_visibility_without_frontend_requirements(tmp_path, setting, expected):
    environ = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic-results'}
    if setting is not None:
        environ['RUN_UI'] = setting
    result = resolve_config(load_shared_schema(), environ=environ, anchors={'owner_root': tmp_path})
    result.raise_for_errors()
    assert result.values['RUN_UI'] is expected
    assert 'UI_SOURCE_DIR' not in result.values
