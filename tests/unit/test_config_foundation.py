from pathlib import Path
import pytest
from api_agent.config import (ConfigError, compose_schema, load_declarations, load_shared_schema,
                                 parse_options, read_dotenv, resolve_config, schema_from_dict)

def test_precedence_and_explicit_mode_preserve_positionals():
    result = resolve_config(load_shared_schema(), argv=['--tui_mode=unit', 'one', '--no-std-in', 'False', 'two'],
                            dotenv={'TUI_MODE': 'e2e'}, environ={'TUI_MODE': 'integration'})
    result.raise_for_errors()
    assert result.values['TUI_MODE'] == 'unit'
    assert result.fields['TUI_MODE'].source == 'cli'
    assert result.fields['TUI_MODE'].explicit
    assert result.positionals == ('one', 'two')

def test_help_skips_bad_execution_config_but_rejects_unknown_cli():
    result = resolve_config(load_shared_schema(), argv=['-h'], environ={'NO_STD_IN': 'bad', 'E2E_RERUNS': 'bad'})
    assert result.help_requested and not result.errors
    assert resolve_config(load_shared_schema(), argv=['-h', '--typo'], environ={}).errors


@pytest.mark.parametrize(('argv', 'option'), [
    (['--tui-mode', 'unattended-auth', '--tui-mode', 'unit'], '--tui-mode'),
    (['--tui-mode=unit', '--tui_mode=e2e'], '--tui-mode'),
    (['--TUI_MODE=unit', '--tui-mode=unit'], '--tui-mode'),
    (['--no-std-in', '--no_std_in=False'], '--no-std-in'),
    (['-h', '--help'], '--help'),
    (['--help=False', '-h'], '--help'),
])
def test_duplicate_options_are_errors_across_normalized_spellings(argv, option):
    parsed = parse_options(load_shared_schema(), argv)
    assert parsed.errors == (f'Duplicate option {option}',)


def test_duplicate_detection_preserves_separator_and_first_value():
    schema = load_shared_schema()
    parsed = parse_options(schema, ['--tui-mode=unit', '--', '--tui-mode=e2e'])
    assert parsed.values['TUI_MODE'] == 'unit' and not parsed.errors
    assert parsed.positionals == ('--tui-mode=e2e',)
    parsed = parse_options(schema, ['--tui-mode=unit', '--tui-mode=e2e'])
    assert parsed.values['TUI_MODE'] == 'unit'
    assert parsed.errors == ('Duplicate option --tui-mode',)
    assert resolve_config(schema, argv=['-h', '--help'], environ={}).errors == ('Duplicate option --help',)

def test_strict_booleans_and_types():
    schema = load_shared_schema()
    for value in ['true', 'false', '1', 'yes']:
        result = resolve_config(schema, argv=[f'--no-std-in={value}'], environ={})
        assert result.errors
    with pytest.raises(ValueError):
        compose_schema(schema, overrides={'E2E_RERUNS': True})
    with pytest.raises(ValueError):
        schema_from_dict({'version': 1, 'fields': {'X': {'default': 1}}})

def test_missing_aggregate_and_interactive_data():
    schema = schema_from_dict({'version': 1, 'fields': {
        'FIRST': {'type': 'str', 'required': True}, 'SECOND': {'type': 'int', 'required': True}}})
    result = resolve_config(schema, environ={})
    assert result.missing == ('FIRST', 'SECOND')
    with pytest.raises(ConfigError, match='--first'):
        result.raise_for_errors()
    interactive = resolve_config(schema, environ={}, interactive=True)
    assert interactive.missing == result.missing and not interactive.errors

def test_conditional_defaults_and_pack_override_provenance():
    schema = load_shared_schema()
    result = resolve_config(schema, environ={'TUI_MODE': 'run-local', 'NO_STD_IN': 'True'})
    assert result.values['RUN_LOG_SOURCE'] == 'all'
    assert not result.fields['RUN_LOG_SOURCE'].explicit
    result = resolve_config(compose_schema(schema, overrides={'RUN_LOG_SOURCE': 'custom'}),
                            environ={'TUI_MODE': 'run-local', 'NO_STD_IN': 'True'})
    assert result.values['RUN_LOG_SOURCE'] == 'custom'
    assert result.fields['RUN_LOG_SOURCE'].source == 'pack_default'

def test_active_required_and_collection_exemptions(tmp_path):
    schema = load_shared_schema()
    run = resolve_config(schema, environ={'TUI_MODE': 'run-local', 'RUN_UI': 'True'}, anchors={'owner_root': tmp_path})
    assert run.missing == ('UI_SOURCE_DIR',)
    assert run.errors
    collect = resolve_config(schema, environ={'TUI_MODE': 'e2e', 'RUN_UI': 'True',
                            'COLLECT_ONLY': 'True', 'E2E_RERUNS': 'bad'})
    assert not collect.errors and 'E2E_RERUNS' not in collect.values
    resolved = resolve_config(schema, environ={'TUI_MODE': 'run-local', 'RUN_UI': 'True',
                              'UI_SOURCE_DIR': 'web'}, anchors={'owner_root': tmp_path})
    assert resolved.values['UI_SOURCE_DIR'] == tmp_path / 'web'

def test_no_stdin_requires_mode_and_unknown_external_keys_ignored():
    result = resolve_config(load_shared_schema(), environ={'NO_STD_IN': 'True', 'UNKNOWN': 'x'}, dotenv={'OTHER': 'x'})
    assert result.missing == ('TUI_MODE',)
    assert len(result.errors) == 1

def test_dotenv_is_literal_and_missing_file_empty(tmp_path):
    path = tmp_path / '.env'
    assert read_dotenv(path) == {}
    path.write_text('# comment\nexport NAME="$(touch /tmp/no)"\nFLAG=True\n')
    assert read_dotenv(path) == {'NAME': '$(touch /tmp/no)', 'FLAG': 'True'}

def resource_pack_schema():
    from pathlib import Path
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {
        'UI_SOURCE_DIR': {'type': 'path', 'anchor': 'owner_root', 'override': True, 'required': True,
            'acknowledge_default': False, 'persist_default': False, 'applies_when': {
                'any': [{'TUI_MODE': 'e2e'}, {'TUI_MODE': 'run-local', 'RUN_UI': True}], 'COLLECT_ONLY': False}},
        'UI_NX_APP': {'type': 'str', 'override': True, 'default': 'shell', 'acknowledge_default': False, 'persist_default': False,
                     'applies_when': {'any': [{'TUI_MODE': 'e2e'}, {'TUI_MODE': 'run-local', 'RUN_UI': True}], 'COLLECT_ONLY': False}},
        'UNATTENDED_SECONDARY_USER': {'type': 'str', 'default': 'Resource Pack User', 'acknowledge_default': False, 'persist_default': False,
                                    'applies_when': {'TUI_MODE': ['run-local', 'integration', 'e2e', 'unattended-auth'], 'COLLECT_ONLY': False}}}},
        base_schema=shared, source='pack_default')
    if 'UNATTENDED_USER_INDEX' not in extension.fields:
        selection = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_USER_INDEX': {
            'type': 'int', 'validation': 'nonnegative', 'acknowledge_default': False,
            'persist_default': False, 'applies_when': {
                'TUI_MODE': ['run-local', 'integration', 'e2e', 'unattended-auth'],
                'COLLECT_ONLY': False}}}}, base_schema=shared, source='pack_default')
        extension = compose_schema(extension, selection)
    token = schema_from_dict({'version': 1, 'fields': {'PACK_TEST_TOKEN': {
        'type': 'str', 'required': True, 'applies_when': {
            'TUI_MODE': ['run-local', 'integration', 'e2e'], 'COLLECT_ONLY': False}}}},
        base_schema=shared, source='pack_default')
    if 'UNATTENDED_USER_INDEX' not in extension.fields:
        selection = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_USER_INDEX': {
            'type': 'int', 'validation': 'nonnegative', 'acknowledge_default': False,
            'persist_default': False, 'applies_when': {
                'TUI_MODE': ['run-local', 'integration', 'e2e', 'unattended-auth'],
                'COLLECT_ONLY': False}}}}, base_schema=shared, source='pack_default')
        extension = compose_schema(extension, selection)
    extension = compose_schema(extension, token)
    return compose_schema(shared, extension)

def test_json_resolution_index_validation_and_secret_diagnostics(tmp_path):
    result = resolve_config(resource_pack_schema(), environ={'TUI_MODE': 'e2e', 'UI_SOURCE_DIR': '/example/frontend',
                            'UNATTENDED_AUTH': 'secret-invalid', 'UNATTENDED_USER_INDEX': '-1',
                            'E2E_BROWSER_RESOLUTION': '0x900', 'PACK_TEST_TOKEN': 'test-only'}, anchors={'owner_root': tmp_path})
    assert len(result.errors) == 3
    assert 'secret-invalid' not in '\n'.join(result.errors)

def test_pack_only_path_and_metadata(tmp_path):
    schema = load_shared_schema()
    assert 'API_AGENT_DIR' not in resolve_config(schema, environ={}).values
    result = resolve_config(schema, environ={}, pack_launcher=True, anchors={'pack_root': tmp_path})
    assert result.values['API_AGENT_DIR'] == (tmp_path / '../api-agent').resolve()
    assert schema.fields['NO_STD_IN'].acknowledge_default is False
    assert schema.fields['NO_STD_IN'].persist_default is False
    assert schema.fields['E2E_DEBUG'].acknowledge_default is False
    assert 'UNATTENDED_USER_INDEX' not in schema.fields
    assert resource_pack_schema().fields['UNATTENDED_USER_INDEX'].persist_default is False

def test_cli_missing_value_and_end_options():
    parsed = parse_options(load_shared_schema(), ['--tui-mode', '--no-std-in', '--', '--literal'])
    assert parsed.errors and parsed.positionals == ('--literal',)

def test_separate_lowercase_boolean_rejected():
    result = resolve_config(load_shared_schema(), argv=['--no-std-in', 'true'], environ={})
    assert any('True or False' in error for error in result.errors)

def test_empty_extension_and_explicit_override_anchor(tmp_path):
    empty = schema_from_dict({'version': 1})
    schema = compose_schema(load_shared_schema(), empty,
                            {'UI_SOURCE_DIR': {'value': 'frontend', 'anchor': 'pack_root'}})
    result = resolve_config(schema, environ={'TUI_MODE': 'run-local', 'RUN_UI': 'True'},
                            anchors={'pack_root': tmp_path})
    assert result.values['UI_SOURCE_DIR'] == tmp_path / 'frontend'

@pytest.mark.parametrize('spec', [
    {'type': 'str', 'enum': 'abc'}, {'type': 'bool', 'enum': ['True']},
    {'type': 'str', 'validation': 'nonnegative'}, {'type': 'int', 'validation': 'resolution'},
    {'type': 'str', 'help': 5}, {'type': 'str', 'applies_when': {'UNKNOWN': True}},
])
def test_strict_schema_metadata(spec):
    with pytest.raises(ValueError):
        schema_from_dict({'version': 1, 'fields': {'X': spec}})

def test_pack_extension_predicate_uses_shared_control():
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {'PACK_TOKEN': {
        'type': 'str', 'required': True, 'applies_when': {'TUI_MODE': 'run-local'}}}}, base_schema=shared)
    schema = compose_schema(shared, extension)
    assert resolve_config(schema, environ={'TUI_MODE': 'unit'}).missing == ()
    assert resolve_config(schema, environ={'TUI_MODE': 'run-local'}).missing == ('PACK_TOKEN',)

def test_conditional_controllers_resolve_before_applicability():
    schema = schema_from_dict({'version': 1, 'fields': {
        'DEPENDENT': {'type': 'str', 'required': True, 'applies_when': {'ENABLED': True}},
        'ENABLED': {'type': 'bool', 'default': False,
                    'conditional_defaults': [{'when': {'CONTROL': True}, 'value': True}]},
        'CONTROL': {'type': 'bool', 'default': True}}})
    result = resolve_config(schema, environ={})
    assert result.missing == ('DEPENDENT',)
    assert result.values['ENABLED'] is True

def test_resource_pack_local_defaults_and_shared_user_index(tmp_path):
    from pathlib import Path
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {
        'UI_NX_APP': {'type': 'str', 'override': True, 'default': 'shell',
                     'applies_when': {'any': [{'TUI_MODE': 'e2e'}, {'TUI_MODE': 'run-local', 'RUN_UI': True}], 'COLLECT_ONLY': False}},
        'UNATTENDED_SECONDARY_USER': {'type': 'str', 'default': 'Resource Pack User',
                                    'applies_when': {'TUI_MODE': ['run-local', 'integration', 'e2e', 'unattended-auth'], 'COLLECT_ONLY': False}}}},
        base_schema=shared, source='pack_default')
    if 'UNATTENDED_USER_INDEX' not in extension.fields:
        selection = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_USER_INDEX': {
            'type': 'int', 'validation': 'nonnegative', 'acknowledge_default': False,
            'persist_default': False, 'applies_when': {
                'TUI_MODE': ['run-local', 'integration', 'e2e', 'unattended-auth'],
                'COLLECT_ONLY': False}}}}, base_schema=shared, source='pack_default')
        extension = compose_schema(extension, selection)
    schema = compose_schema(shared, extension)
    result = resolve_config(schema, environ={'TUI_MODE': 'run-local', 'RUN_UI': 'True',
                            'UNATTENDED_USER_INDEX': '1', 'PACK_TEST_TOKEN': 'synthetic-secret', 'UI_SOURCE_DIR': str(tmp_path / 'ui')}, anchors={'owner_root': tmp_path})
    result.raise_for_errors()
    assert result.values['UI_NX_APP'] == 'shell'
    assert result.values['UNATTENDED_SECONDARY_USER'] == 'Resource Pack User'
    assert result.values['UNATTENDED_USER_INDEX'] == 1

@pytest.mark.parametrize('argv', [
    ['--tui-mode', '--no-std-in'],
])
def test_option_like_value_becomes_next_key(argv):
    result = resolve_config(load_shared_schema(), argv=argv, environ={})
    assert result.values['NO_STD_IN'] is True
    assert any('TUI_MODE: option requires a value' in error for error in result.errors)

def test_boolean_space_option_like_value_becomes_bare_flag():
    result = resolve_config(load_shared_schema(), argv=['--no-std-in', '--tui-mode=unit'], environ={})
    result.raise_for_errors()
    assert result.values['NO_STD_IN'] is True and result.values['TUI_MODE'] == 'unit'

def test_safe_leading_hyphen_directory_and_literal_json_dotenv(tmp_path):
    path = tmp_path / '.env'
    credentials = '{"Resource Pack User":[{"password":"literal$secret","username":"person"}]}'
    path.write_text("TUI_MODE=run-local\nUNATTENDED_AUTH='" + credentials + "'\n")
    result = resolve_config(load_shared_schema(), dotenv=read_dotenv(path), environ={},
                            argv=['--run-ui', '--ui-source-dir=./--name'], anchors={'owner_root': tmp_path})
    result.raise_for_errors()
    assert result.values['UI_SOURCE_DIR'] == tmp_path / '--name'
    assert result.values['UNATTENDED_AUTH']['Resource Pack User'][0]['password'] == 'literal$secret'

@pytest.mark.parametrize('value', ['', '   '])
def test_empty_path_rejected_before_anchor_resolution(value, tmp_path):
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': 'run-local', 'RUN_UI': 'True'},
                            argv=['--ui-source-dir=' + value], anchors={'owner_root': tmp_path})
    assert any('UI_SOURCE_DIR: expected nonempty path' in error for error in result.errors)
    assert 'UI_SOURCE_DIR' not in result.values

def test_malformed_conditional_default_is_schema_error():
    with pytest.raises(ValueError, match='conditional default needs when and value'):
        schema_from_dict({'version': 1, 'fields': {'X': {'type': 'int', 'conditional_defaults': [1]}}})

def test_extension_default_source_from_loaded_toml(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('version = 1\n[fields.PACK_NAME]\ntype = "str"\ndefault = "demo"\n')
    schema = compose_schema(load_shared_schema(), load_declarations(path, source='pack_default'))
    result = resolve_config(schema, environ={})
    assert result.fields['PACK_NAME'].source == 'pack_default'
    assert result.fields['PACK_NAME'].explicit is False


def test_removed_secondary_index_is_rejected():
    schema = load_shared_schema()
    assert 'UNATTENDED_SECONDARY_USER_INDEX' not in schema.fields
    result = resolve_config(schema, argv=['--unattended-secondary-user-index=1'], environ={})
    assert result.errors


def test_equals_preserves_option_like_literal():
    schema = schema_from_dict({'version': 1, 'fields': {'TEXT': {'type': 'str'}}})
    result = resolve_config(schema, argv=['--text=--literal=value'], environ={})
    result.raise_for_errors()
    assert result.values['TEXT'] == '--literal=value'


@pytest.mark.parametrize('mode,run_ui,active', [
    ('integration', 'False', False), ('integration', 'True', False),
    ('run-local', 'False', False), ('run-local', 'True', True),
    ('e2e', 'False', True), ('e2e', 'True', True)])
def test_ui_applies_to_e2e_and_optional_local_only(tmp_path, mode, run_ui, active):
    from pathlib import Path
    base = Path(__file__).resolve().parents[2]
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {'UI_NX_APP': {
        'type': 'str', 'override': True, 'default': 'shell', 'applies_when': {
            'any': [{'TUI_MODE': 'e2e'}, {'TUI_MODE': 'run-local', 'RUN_UI': True}],
            'COLLECT_ONLY': False}}}}, base_schema=shared, source='pack_default')
    for schema, app in [(shared, 'agent'), (compose_schema(shared, extension), 'shell')]:
        result = resolve_config(schema, environ={'TUI_MODE': mode, 'RUN_UI': run_ui, 'PACK_TEST_TOKEN': 'test-only', 'UI_SOURCE_DIR': str(tmp_path / 'ui')},
                                anchors={'owner_root': tmp_path})
        result.raise_for_errors()
        assert ('UI_NX_APP' in result.values) == active
        assert ('UI_SOURCE_DIR' in result.values) == active
        if active:
            assert result.values['UI_NX_APP'] == app


def test_explicit_field_override_validation():
    shared = schema_from_dict({'version': 1, 'fields': {'APP': {'type': 'str'}}})
    for spec, message in [({'type': 'str'}, 'requires override'),
                          ({'type': 'int', 'override': True}, 'preserve the shared type')]:
        extension = schema_from_dict({'version': 1, 'fields': {'APP': spec}})
        with pytest.raises(ValueError, match=message):
            compose_schema(shared, extension)


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_parallel_jobs_defaults_serial_and_validation(tmp_path, mode):
    schema = load_shared_schema()
    options = {'TUI_MODE': mode, 'UI_SOURCE_DIR': str(tmp_path / 'frontend')}
    result = resolve_config(schema, environ=options, anchors={'owner_root': tmp_path})
    result.raise_for_errors()
    assert result.values['TEST_PARALLEL_JOBS'] == 8
    if mode == 'e2e':
        assert result.values['E2E_RERUNS'] == 2
    serial = resolve_config(schema, environ=options, argv=['--test-parallel-jobs=0'],
                            anchors={'owner_root': tmp_path})
    serial.raise_for_errors()
    assert serial.values['TEST_PARALLEL_JOBS'] == 0
    invalid = resolve_config(schema, environ=options, argv=['--test-parallel-jobs=-1'],
                             anchors={'owner_root': tmp_path})
    assert any('TEST_PARALLEL_JOBS' in error for error in invalid.errors)


def test_secondary_role_is_resource_pack_only():
    shared = load_shared_schema()
    assert 'UNATTENDED_SECONDARY_USER' not in shared.fields
    result = resolve_config(shared, argv=['--unattended-secondary-user=Resource Pack User'], environ={})
    assert result.errors


def test_shared_runtime_rejects_pack_only_account_index():
    result = resolve_config(load_shared_schema(), argv=['--unattended-user-index=1'], environ={})
    assert any('Unknown option' in error for error in result.errors)


def test_cli_and_bootstrap_defaults_skip_interactive_review():
    schema = load_shared_schema()
    for name in ('NO_STD_IN', 'COLLECT_ONLY', 'API_AGENT_DIR', 'UI_SOURCE_DIR', 'UI_NX_APP'):
        assert schema.fields[name].acknowledge_default is False
        assert schema.fields[name].persist_default is False


def test_resource_pack_internal_defaults_skip_interactive_review():
    schema = resource_pack_schema()
    for name in ('UI_SOURCE_DIR', 'UI_NX_APP', 'UNATTENDED_SECONDARY_USER'):
        assert schema.fields[name].acknowledge_default is False
        assert schema.fields[name].persist_default is False


def test_resource_pack_token_required_for_api_and_test_execution(tmp_path):
    schema = resource_pack_schema()
    anchors = {'owner_root': tmp_path, 'pack_root': tmp_path}
    for mode in ('run-local', 'integration', 'e2e'):
        missing = resolve_config(schema, environ={'TUI_MODE': mode, 'UI_SOURCE_DIR': str(tmp_path / 'ui')}, anchors=anchors, interactive=True)
        assert missing.missing == ('PACK_TEST_TOKEN',)
        supplied = resolve_config(schema, environ={'TUI_MODE': mode, 'PACK_TEST_TOKEN': 'test-only', 'UI_SOURCE_DIR': str(tmp_path / 'ui')}, anchors=anchors)
        assert not supplied.errors
        collected = resolve_config(schema, environ={'TUI_MODE': mode, 'COLLECT_ONLY': 'True'}, anchors=anchors)
        assert not collected.errors
    for mode in ('unit', 'config-cleanup', 'unattended-auth'):
        result = resolve_config(schema, environ={'TUI_MODE': mode}, anchors=anchors)
        assert not result.errors
        assert 'PACK_TEST_TOKEN' not in result.values
