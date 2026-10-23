"""Declared tracing keys follow target defaults and normal precedence."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import load_shared_schema, resolve_config
from api_agent.runtime.dependencies import requirements


@pytest.mark.parametrize('mode', ['run-local', 'integration', 'e2e'])
def test_local_keys_and_public_ca_configuration(mode, tmp_path):
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': mode, 'UI_SOURCE_DIR': '/example/frontend', 'TRACE_MODE': 'LangFuse', 'CLIENT_CA_FILE': 'public-ca.pem',
        'ALLURE_S3_BUCKET': 'results', 'LANGFUSE_ADMIN_PASSWORD': 'synthetic-password'}, anchors={'owner_root': tmp_path})
    assert not result.errors and not result.missing
    assert result.values['LANGFUSE_PUBLIC_KEY'] == 'pk-lf-local-dev-0000000000'
    assert result.values['LANGFUSE_SECRET_KEY'] == 'sk-lf-local-dev-0000000000'
    assert result.values['CLIENT_CA_FILE'] == tmp_path / 'public-ca.pem'
    assert not result.fields['LANGFUSE_SECRET_KEY'].explicit


def test_remote_keys_have_no_default_and_both_are_required():
    schema = load_shared_schema()
    env = {'TUI_MODE': 'run-local', 'TRACE_MODE': 'LangFuse', 'TRACE_TARGET': 'https://trace.example',
           'LANGFUSE_ADMIN_EMAIL': 'admin@example.invalid', 'LANGFUSE_ADMIN_PASSWORD': 'synthetic-password'}
    missing = resolve_config(schema, environ=env)
    assert missing.missing == ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')
    ready = resolve_config(schema, environ=env | {'LANGFUSE_PUBLIC_KEY': 'public', 'LANGFUSE_SECRET_KEY': 'secret'})
    assert not ready.errors and not ready.missing
    assert ready.values['LANGFUSE_SECRET_KEY'] == 'secret'
    assert ready.fields['LANGFUSE_SECRET_KEY'].source == 'environment'


@pytest.mark.parametrize('env', [{'TUI_MODE': 'unit', 'TRACE_MODE': 'LangFuse'},
    {'TUI_MODE': 'integration', 'COLLECT_ONLY': 'True', 'TRACE_MODE': 'LangFuse'},
    {'TUI_MODE': 'run-local', 'TRACE_MODE': 'OTEL'}, {'TUI_MODE': 'run-local'}])
def test_keys_not_required_or_included_when_not_applicable(env):
    result = resolve_config(load_shared_schema(), environ=env)
    assert not result.missing and not result.errors
    assert 'LANGFUSE_SECRET_KEY' not in result.values


def test_tracing_dependencies_selected_only_by_operation_preflight():
    root = Path(__file__).resolve().parents[2]
    context = SimpleNamespace(base_root=root, pack_root=root / 'demo')
    assert not any(item.startswith('opentelemetry') for item in requirements(context))
    wanted = requirements(context, groups=('tracing',))
    assert 'opentelemetry-sdk==1.45.1' in wanted
    assert 'opentelemetry-exporter-otlp-proto-http==1.45.1' in wanted
