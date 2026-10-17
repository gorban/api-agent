"""Recording prerequisites are mode-specific and collection never needs storage."""
from pathlib import Path
import pytest
from api_agent.config import load_shared_schema, resolve_config


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_recording_requires_primary_s3_and_optional_secondary_does_not_replace_it(mode, tmp_path):
    schema = load_shared_schema()
    result = resolve_config(schema, environ={'TUI_MODE': mode, 'UI_SOURCE_DIR': '/example/frontend', 'ALLURE_SECONDARY_DIR': 'extra'},
                            anchors={'owner_root': tmp_path})
    assert result.missing == ('ALLURE_S3_BUCKET',)
    assert result.values['ALLURE_SECONDARY_DIR'] == tmp_path / 'extra'
    ready = resolve_config(schema, environ={'TUI_MODE': mode, 'UI_SOURCE_DIR': '/example/frontend', 'ALLURE_S3_BUCKET': 'test-results',
                                           'TOKEN_USAGE_CSV_ENABLED': 'True',
                                           'TOKEN_USAGE_CSV': 'usage.csv'}, anchors={'owner_root': tmp_path})
    assert not ready.errors and not ready.missing
    assert ready.values['ALLURE_RESULTS_DIR'] == tmp_path / 'artifacts/allure'
    assert ready.values['ALLURE_S3_PREFIX'] == 'allure'
    assert ready.values['TOKEN_USAGE_CSV'] == tmp_path / 'usage.csv'


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_collection_and_units_ignore_storage_values(mode):
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': mode, 'COLLECT_ONLY': 'True',
                            'ALLURE_S3_BUCKET': '', 'ALLURE_RESULTS_DIR': 'ignored'})
    assert not result.errors and not result.missing
    assert not any(name.startswith('ALLURE_') for name in result.values)
    assert 'TOKEN_USAGE_CSV' not in result.values


def test_allure_viewing_has_optional_s3_without_execution_requirement(tmp_path):
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': 'allure', 'ALLURE_S3_BUCKET': 'history',
                            'ALLURE_S3_ENDPOINT': 'http://localhost:4566'}, anchors={'owner_root': tmp_path})
    assert not result.errors and not result.missing
    assert result.values['ALLURE_S3_BUCKET'] == 'history'
    assert result.values['ALLURE_S3_ENDPOINT'] == 'http://localhost:4566'
    assert result.values['ALLURE_S3_REGION'] == 'us-east-1'


def test_unit_execution_has_no_recording_settings():
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': 'unit', 'ALLURE_S3_BUCKET': 'unused'})
    assert not result.errors and not result.missing
    assert not any(name.startswith('ALLURE_') for name in result.values)


def test_reports_dependencies_are_prepared_only_when_requested():
    from types import SimpleNamespace
    from api_agent.runtime.dependencies import requirements
    root = Path(__file__).resolve().parents[2]
    context = SimpleNamespace(base_root=root, pack_root=root / 'demo')
    normal = requirements(context, collect=True)
    reports = requirements(context, groups=('reports',))
    assert not any(item.startswith('boto3') for item in normal)
    assert 'boto3>=1.37.32,<2' in reports


@pytest.mark.parametrize('mode', ['integration', 'e2e', 'allure'])
@pytest.mark.parametrize('path', [None, '', '  '])
def test_secondary_view_requires_nonempty_directory(mode, path, tmp_path):
    environ = {'TUI_MODE': mode, 'UI_SOURCE_DIR': '/example/frontend',
               'ALLURE_S3_BUCKET': 'reports', 'ALLURE_VIEW_SECONDARY': 'True'}
    if path is not None:
        environ['ALLURE_SECONDARY_DIR'] = path

    result = resolve_config(load_shared_schema(), environ=environ, anchors={'owner_root': tmp_path})

    assert result.missing == ('ALLURE_SECONDARY_DIR',)
    assert result.errors == ('ALLURE_SECONDARY_DIR: required; provide ALLURE_SECONDARY_DIR or --allure-secondary-dir',)


@pytest.mark.parametrize('mode', ['integration', 'e2e', 'allure'])
def test_secondary_view_is_visible_default_off_and_uses_normal_precedence(mode, tmp_path):
    schema = load_shared_schema()
    environ = {'TUI_MODE': mode, 'UI_SOURCE_DIR': '/example/frontend', 'ALLURE_S3_BUCKET': 'reports'}
    default = resolve_config(schema, environ=environ, anchors={'owner_root': tmp_path})
    assert default.values['ALLURE_VIEW_SECONDARY'] is False
    assert not default.errors
    enabled = resolve_config(schema, environ={**environ, 'ALLURE_VIEW_SECONDARY': 'False'},
                             argv=['--allure-view-secondary', '--allure-secondary-dir=extra'],
                             anchors={'owner_root': tmp_path})
    assert not enabled.errors
    assert enabled.values['ALLURE_VIEW_SECONDARY'] is True
    assert enabled.values['ALLURE_SECONDARY_DIR'] == tmp_path / 'extra'
    assert enabled.fields['ALLURE_VIEW_SECONDARY'].source == 'cli'
    assert schema.fields['ALLURE_VIEW_SECONDARY'].acknowledge_default


@pytest.mark.parametrize('mode,collect', [('unit', False), ('run-local', False),
                                        ('integration', True), ('e2e', True), ('allure', True)])
def test_secondary_view_does_not_require_storage_outside_recording_modes(mode, collect):
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': mode, 'COLLECT_ONLY': str(collect), 'ALLURE_VIEW_SECONDARY': 'True'})

    assert not result.errors and not result.missing
    assert 'ALLURE_VIEW_SECONDARY' not in result.values
    assert 'ALLURE_SECONDARY_DIR' not in result.values


def test_secondary_view_does_not_remove_primary_publication_requirement():
    result = resolve_config(load_shared_schema(), environ={'TUI_MODE': 'integration',
                            'ALLURE_VIEW_SECONDARY': 'True', 'ALLURE_SECONDARY_DIR': 'extra'})

    assert result.missing == ('ALLURE_S3_BUCKET',)


@pytest.mark.parametrize('enabled,expected_missing', [('False', False), ('True', True)])
def test_review_preserves_optional_empty_path_enablement(tmp_path, enabled, expected_missing):
    from types import SimpleNamespace
    from api_agent.config.session import ConfigSession
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
                              dotenv_path=tmp_path / '.env', external_pack=False,
                              anchors={'owner_root': tmp_path})
    session = ConfigSession(context, load_shared_schema(), environ={
        'TUI_MODE': 'allure', 'ALLURE_VIEW_SECONDARY': enabled, 'ALLURE_SECONDARY_DIR': ''})

    result = session.result(_review=True)

    assert ('ALLURE_SECONDARY_DIR' in result.missing) is expected_missing
    assert result.values['ALLURE_SECONDARY_DIR'] == ''
