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


@pytest.mark.parametrize('mode', ['OTEL', '', 'LangFuse'])
@pytest.mark.parametrize('target, expected', [
    ('http://localhost:3000', ('admin@local.dev', 'password')),
    ('http://127.0.0.1:3000/', ('admin@local.dev', 'password')),
    ('https://trace.example.invalid', (None, None)),
])
def test_session_review_admin_defaults_follow_target_without_writes(tmp_path, mode, target, expected):
    from api_agent.config.session import ConfigSession
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False,
        anchors={'base_root': tmp_path, 'pack_root': tmp_path, 'owner_root': tmp_path})
    session = ConfigSession(context, load_shared_schema(), environ={
        'TUI_MODE': 'run-local', 'TRACE_MODE': mode, 'TRACE_TARGET': target})

    result = session.result(_review=True)

    names = ('LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD')
    assert tuple(result.values.get(name) for name in names) == (expected if mode == 'LangFuse' else (None, None))
    assert set(names).intersection(result.active_fields) == (set(names) if mode == 'LangFuse' else set())
    assert set(names).intersection(result.missing) == (set(names) if mode == 'LangFuse' and expected == (None, None) else set())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('source', ['environment', 'dotenv', 'interactive'])
def test_session_explicit_empty_admin_credentials_do_not_restore_defaults(tmp_path, source):
    from api_agent.config.session import ConfigSession
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False,
        anchors={'base_root': tmp_path, 'pack_root': tmp_path, 'owner_root': tmp_path})
    env = {'TUI_MODE': 'run-local', 'TRACE_MODE': 'LangFuse'}
    credentials = {'LANGFUSE_ADMIN_EMAIL': '', 'LANGFUSE_ADMIN_PASSWORD': ''}
    if source == 'environment':
        env.update(credentials)
    if source == 'dotenv':
        context.dotenv_path.write_text('LANGFUSE_ADMIN_EMAIL=\nLANGFUSE_ADMIN_PASSWORD=\n')
    session = ConfigSession(context, load_shared_schema(), environ=env)
    if source == 'interactive':
        for name in credentials:
            session.set_value(name, '')
    before = context.dotenv_path.read_bytes() if context.dotenv_path.exists() else None

    result = session.result(_review=True)

    for name in credentials:
        assert result.values[name] == ''
        assert result.fields[name].explicit
    assert (context.dotenv_path.read_bytes() if context.dotenv_path.exists() else None) == before


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_remote_test_mode_does_not_require_dashboard_login(mode, tmp_path):
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': mode, 'TRACE_MODE': 'LangFuse',
        'TRACE_TARGET': 'https://trace.example.invalid',
        'LANGFUSE_PUBLIC_KEY': 'synthetic-public', 'LANGFUSE_SECRET_KEY': 'synthetic-secret',
        'ALLURE_S3_BUCKET': 'synthetic-results', 'UI_SOURCE_DIR': '/example/frontend'},
        anchors={'owner_root': tmp_path})

    assert not result.errors and not result.missing
    assert 'LANGFUSE_ADMIN_EMAIL' not in result.values
    assert 'LANGFUSE_ADMIN_PASSWORD' not in result.values


@pytest.mark.parametrize('env', [
    {'TUI_MODE': 'unit', 'TRACE_MODE': 'LangFuse'},
    {'TUI_MODE': 'integration', 'TRACE_MODE': 'OTEL'},
    {'TUI_MODE': 'e2e', 'TRACE_MODE': ''},
    {'TUI_MODE': 'integration', 'TRACE_MODE': 'LangFuse', 'COLLECT_ONLY': 'True'},
])
def test_dashboard_provisioning_credentials_remain_dormant_when_inactive(env):
    result = resolve_config(load_shared_schema(), environ={
        **env, 'LANGFUSE_ADMIN_EMAIL': 'operator@example.invalid',
        'LANGFUSE_ADMIN_PASSWORD': 'synthetic-password'})

    assert 'LANGFUSE_ADMIN_EMAIL' not in result.values
    assert 'LANGFUSE_ADMIN_PASSWORD' not in result.values


MANAGED_LANGFUSE_TARGETS = (
    'http://localhost:3000', 'http://127.0.0.1:3000',
    'http://localhost:3000/', 'http://127.0.0.1:3000/',
)
CUSTOM_LANGFUSE_TARGETS = (
    'http://LOCALHOST:3000', 'HTTP://localhost:3000',
    'http://localhost:3000//', 'http://127.0.0.1:3000///',
    'http://localhost:3000/?viewer=1', 'http://localhost:3001',
)
LANGFUSE_CREDENTIAL_FIELDS = (
    'LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD',
    'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY',
)


def test_managed_langfuse_defaults_declare_exact_profile_target_inventory():
    schema = load_shared_schema()

    for name in LANGFUSE_CREDENTIAL_FIELDS:
        declaration = schema.fields[name]
        assert len(declaration.conditional_defaults) == 1
        assert declaration.conditional_defaults[0]['when'] == {
            'TRACE_TARGET': list(MANAGED_LANGFUSE_TARGETS)}


@pytest.mark.parametrize('mode', ['run-local', 'integration', 'e2e'])
@pytest.mark.parametrize('target', MANAGED_LANGFUSE_TARGETS + CUSTOM_LANGFUSE_TARGETS)
def test_resolver_and_managed_langfuse_profiles_admit_same_targets(tmp_path, mode, target):
    from api_agent.runtime.compose import selected_profiles
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': mode, 'TRACE_MODE': 'LangFuse', 'TRACE_TARGET': target,
        'ALLURE_S3_BUCKET': 'synthetic-results', 'UI_SOURCE_DIR': '/example/frontend'},
        anchors={'owner_root': tmp_path}, interactive=True)

    assert not result.errors
    assert selected_profiles(result.values) == (('langfuse',) if target in MANAGED_LANGFUSE_TARGETS else ())
    assert all(name in result.values for name in LANGFUSE_CREDENTIAL_FIELDS) is (target in MANAGED_LANGFUSE_TARGETS)


@pytest.mark.parametrize('mode', ['run-local', 'integration', 'e2e'])
@pytest.mark.parametrize('target', MANAGED_LANGFUSE_TARGETS + CUSTOM_LANGFUSE_TARGETS)
def test_custom_langfuse_credentials_preserve_explicit_values_and_target(tmp_path, mode, target):
    from api_agent.runtime.compose import selected_profiles
    credentials = dict(zip(LANGFUSE_CREDENTIAL_FIELDS,
        ('operator@example.invalid', 'synthetic-password', 'synthetic-public', 'synthetic-secret')))
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': mode, 'TRACE_MODE': 'LangFuse', 'TRACE_TARGET': target,
        'ALLURE_S3_BUCKET': 'synthetic-results', 'UI_SOURCE_DIR': '/example/frontend', **credentials},
        anchors={'owner_root': tmp_path})

    assert not result.errors and not result.missing
    assert result.values['TRACE_TARGET'] == target
    assert {name: result.values[name] for name in credentials} == credentials
    assert all(result.fields[name].explicit for name in credentials)
    assert selected_profiles(result.values) == (('langfuse',) if target in MANAGED_LANGFUSE_TARGETS else ())
