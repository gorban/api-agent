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
