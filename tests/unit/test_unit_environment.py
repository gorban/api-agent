from pathlib import Path

import pytest

from api_agent.config import compose_schema, resolve_config, schema_from_dict


def declaration(**metadata):
    return schema_from_dict({'version': 1, 'fields': {'ARTIFACT_ROOT': {
        'type': 'path', 'anchor': 'owner_root', 'default': 'reports', **metadata}}})


@pytest.mark.parametrize('value', ['true', 1, None])
def test_unit_environment_metadata_requires_boolean(value):
    with pytest.raises(ValueError, match='unit_environment must be boolean'):
        declaration(unit_environment=value)


def test_unit_environment_cannot_export_sensitive_field():
    with pytest.raises(ValueError, match='sensitive fields cannot use unit_environment'):
        schema_from_dict({'version': 1, 'fields': {'TOKEN': {
            'type': 'str', 'sensitive': True, 'unit_environment': True}}})


def test_unit_environment_rejects_inherited_sensitive_field():
    base = schema_from_dict({'version': 1, 'fields': {'TOKEN': {'type': 'str', 'sensitive': True}}})
    with pytest.raises(ValueError, match='sensitive fields cannot use unit_environment'):
        extension = schema_from_dict({'version': 1, 'fields': {'TOKEN': {
            'type': 'str', 'override': True, 'unit_environment': True}}}, base_schema=base)
        compose_schema(base, extension)


@pytest.mark.parametrize(('argv', 'environment', 'dotenv', 'expected'), [
    ([], {}, {}, 'reports'),
    ([], {}, {'ARTIFACT_ROOT': 'dotenv'}, 'dotenv'),
    ([], {'ARTIFACT_ROOT': 'environment'}, {'ARTIFACT_ROOT': 'dotenv'}, 'environment'),
    (['--artifact-root', 'command'], {'ARTIFACT_ROOT': 'environment'}, {'ARTIFACT_ROOT': 'dotenv'}, 'command'),
])
def test_unit_environment_uses_resolved_precedence_and_path_anchor(tmp_path, argv, environment, dotenv, expected):
    from api_agent.testing.runner import unit_test_environment
    schema = declaration(unit_environment=True)
    result = resolve_config(schema, argv=argv, environ=environment, dotenv=dotenv,
                            anchors={'owner_root': tmp_path})
    result.raise_for_errors()
    assert unit_test_environment(schema, result.values) == {'ARTIFACT_ROOT': str(tmp_path / expected)}


def test_unit_environment_serializes_only_opted_in_resolved_values():
    from api_agent.testing.runner import unit_test_environment
    schema = schema_from_dict({'version': 1, 'fields': {
        'ENABLED': {'type': 'bool', 'unit_environment': True},
        'COUNT': {'type': 'int', 'unit_environment': True},
        'RATE': {'type': 'float', 'unit_environment': True},
        'OPTIONS': {'type': 'jsonobject', 'unit_environment': True},
        'MISSING': {'type': 'str', 'unit_environment': True},
        'TOKEN': {'type': 'str', 'sensitive': True},
        'OTHER': {'type': 'str'},
    }})
    values = {'ENABLED': False, 'COUNT': 2, 'RATE': 1.5, 'OPTIONS': {'key': 'value'},
              'TOKEN': 'secret', 'OTHER': 'ignored'}
    assert unit_test_environment(schema, values) == {
        'ENABLED': 'False', 'COUNT': '2', 'RATE': '1.5', 'OPTIONS': '{"key":"value"}', 'MISSING': None}
    assert not declaration().fields['ARTIFACT_ROOT'].unit_environment


@pytest.mark.parametrize('environment', [{'BAD=KEY': 'text'}, {'KEY': 2}, {'KEY': 'bad\0value'}, {'KEY': 'line\nvalue'}])
def test_unit_runner_rejects_invalid_environment_before_collection(tmp_path, environment):
    from api_agent.testing.runner import run_unit_tests
    with pytest.raises(ValueError, match='test_environment'):
        run_unit_tests(None, [], test_environment=environment)


def test_inactive_unit_field_clears_inherited_environment(tmp_path, monkeypatch):
    from api_agent.testing.runner import unit_test_environment
    monkeypatch.setenv('REPORT_PATH', 'stale-parent-path')
    schema = schema_from_dict({'version': 1, 'fields': {
        'ENABLED': {'type': 'bool', 'default': False},
        'REPORT_PATH': {'type': 'str', 'unit_environment': True,
                        'applies_when': {'ENABLED': True}},
    }})
    result = resolve_config(schema, environ={'REPORT_PATH': 'stale-parent-path'})
    result.raise_for_errors()
    assert 'REPORT_PATH' not in result.values
    assert unit_test_environment(schema, result.values) == {'REPORT_PATH': None}


def test_unit_environment_defensively_rejects_sensitive_direct_declaration():
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from api_agent.testing.runner import unit_test_environment
    schema = ConfigSchema({'TOKEN': FieldDeclaration('TOKEN', 'str', sensitive=True, unit_environment=True)})
    with pytest.raises(ValueError, match='sensitive fields cannot use unit_environment'):
        unit_test_environment(schema, {'TOKEN': 'never-export'})


def test_unit_environment_opt_in_survives_omitted_override_metadata():
    base = declaration(unit_environment=True)
    extension = declaration(override=True, default='updated')
    assert compose_schema(base, extension).fields['ARTIFACT_ROOT'].unit_environment


def test_present_optional_empty_integer_exports_empty_literal():
    from api_agent.testing.runner import unit_test_environment
    schema = schema_from_dict({'version': 1, 'fields': {
        'LIMIT': {'type': 'int', 'allow_empty': True, 'unit_environment': True},
    }})
    result = resolve_config(schema, environ={'LIMIT': ''})
    result.raise_for_errors()
    assert result.values == {'LIMIT': None}
    assert unit_test_environment(schema, result.values) == {'LIMIT': ''}
