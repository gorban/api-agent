"""Provider-owned tracing configuration stays lazy and progressively visible."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import ConfigError
from api_agent.config.declarations import load_shared_schema
from api_agent.config.session import ConfigSession


@pytest.fixture
def tracing_session(tmp_path):
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False,
        anchors={'base_root': tmp_path, 'pack_root': tmp_path, 'owner_root': tmp_path})
    return ConfigSession(context, load_shared_schema(), environ={})


EXPORT_FIELDS = {'TRACE_EXPORT_ENDPOINT', 'TRACE_EXPORT_PROTOCOL', 'TRACE_EXPORT_HEADERS',
                 'TRACE_RESOURCE_ATTRIBUTES', 'TRACELOOP_TRACE_CONTENT', 'TRACE_VIEWER_URL'}
METRIC_DETAILS = {'TRACE_METRICS_ENDPOINT', 'TRACE_METRICS_TEMPORALITY', 'TRACE_METRIC_PROFILE'}
LANGFUSE_FIELDS = {'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY',
                   'LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD'}


@pytest.mark.parametrize('mode', ['', 'LangFuse', 'OTEL'])
def test_bundled_review_visibility_follows_mode(tracing_session, mode):
    tracing_session.set_value('TRACE_MODE', mode)
    result = tracing_session.result(_review=True)
    visible = set(result.active_fields)
    assert 'TRACE_MODE' in visible
    assert 'LOG_CONTENT' in visible
    assert EXPORT_FIELDS <= visible if mode else EXPORT_FIELDS.isdisjoint(visible)
    assert LANGFUSE_FIELDS <= visible if mode == 'LangFuse' else LANGFUSE_FIELDS.isdisjoint(visible)
    assert ('TRACE_METRICS' in visible) is (mode == 'OTEL')
    assert ('JAEGER_VIEWER_PORT' in visible) is (mode == 'OTEL')
    assert METRIC_DETAILS.isdisjoint(visible)


def test_metric_details_require_explicit_opt_in(tracing_session):
    tracing_session.set_value('TRACE_MODE', 'OTEL')
    tracing_session.set_value('TRACE_METRICS', 'True')
    assert METRIC_DETAILS <= set(tracing_session.result(_review=True).active_fields)
    tracing_session.set_value('TRACE_METRICS', 'False')
    assert METRIC_DETAILS.isdisjoint(tracing_session.result(_review=True).active_fields)


@pytest.mark.parametrize('source', ['environment', 'cli'])
def test_inactive_langfuse_metric_request_stays_inert(tracing_session, source):
    tracing_session.set_value('TRACE_MODE', 'LangFuse')
    if source == 'environment':
        tracing_session.environ['TRACE_METRICS'] = 'True'
    else:
        tracing_session.argv = ('--trace-metrics=True',)
    result = tracing_session.result(_review=True)
    assert 'TRACE_METRICS' not in result.values
    assert METRIC_DETAILS.isdisjoint(result.active_fields)
    assert not result.errors


def test_saved_inactive_metric_settings_remain_removable(tracing_session):
    tracing_session.set_value('TRACE_MODE', 'OTEL')
    tracing_session.set_value('TRACE_METRICS', 'True')
    tracing_session.set_value('TRACE_METRICS_ENDPOINT', 'https://metrics.example/v1/metrics', persist=True)
    tracing_session.set_value('TRACE_MODE', '')
    assert 'TRACE_METRICS_ENDPOINT' not in tracing_session.result(_review=True).active_fields
    row = next(row for row in tracing_session.cleanup_rows() if row.name == 'TRACE_METRICS_ENDPOINT')
    assert row.present
    tracing_session.delete_keys(['TRACE_METRICS_ENDPOINT'])
    assert not next(row for row in tracing_session.cleanup_rows() if row.name == 'TRACE_METRICS_ENDPOINT').present


@pytest.mark.parametrize('mode', ['LangFuse', 'OTEL'])
def test_explicit_empty_endpoint_is_preserved(tracing_session, mode):
    tracing_session.set_value('TRACE_MODE', mode)
    tracing_session.set_value('TRACE_EXPORT_ENDPOINT', '')
    value = tracing_session.result().fields['TRACE_EXPORT_ENDPOINT']
    assert value.value == ''
    assert value.explicit


@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli', 'interactive'])
def test_endpoint_precedence(tracing_session, source):
    tracing_session.dotenv_path.write_text('TRACE_MODE=OTEL\nTRACE_EXPORT_ENDPOINT=https://saved.example/v1/traces\n')
    if source in {'environment', 'cli', 'interactive'}:
        tracing_session.environ['TRACE_EXPORT_ENDPOINT'] = 'https://environment.example/v1/traces'
    if source in {'cli', 'interactive'}:
        tracing_session.argv = ('--trace-export-endpoint=https://cli.example/v1/traces',)
    if source == 'interactive':
        tracing_session.set_value('TRACE_EXPORT_ENDPOINT', 'https://interactive.example/v1/traces')
    expected = 'saved' if source == 'dotenv' else source
    assert tracing_session.result().values['TRACE_EXPORT_ENDPOINT'] == f'https://{expected}.example/v1/traces'


def test_unsupported_protocol_is_actionable(tracing_session):
    tracing_session.set_value('TRACE_MODE', 'OTEL')
    assert tracing_session.schema.fields['TRACE_EXPORT_PROTOCOL'].enum == ('', 'http/protobuf')
    with pytest.raises(ConfigError, match='TRACE_EXPORT_PROTOCOL.*http/protobuf'):
        tracing_session.set_value('TRACE_EXPORT_PROTOCOL', 'grpc')


def test_remote_langfuse_does_not_inherit_local_api_keys(tracing_session):
    tracing_session.set_value('TRACE_MODE', 'LangFuse')
    tracing_session.set_value('TRACE_EXPORT_ENDPOINT', 'https://observations.example/api/public/otel/v1/traces')
    review = tracing_session.result(_review=True)
    assert {'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'} <= set(review.missing)
    assert not {'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'} & review.fields.keys()


def test_custom_provider_replaces_bundled_settings_without_factory_import(tracing_session, tmp_path):
    provider = tmp_path / 'custom'
    (provider / 'cfg').mkdir(parents=True)
    (provider / 'provider.py').write_text("raise AssertionError('factory import during review')\n")
    (provider / 'cfg/config.toml').write_text('version=1\n[fields.CUSTOM_DESTINATION]\ntype="str"\ndefault="https://custom.example/traces"\n')
    tracing_session.set_value('TRACE_PROVIDER', str(provider / 'provider.py') + ':create')
    review = tracing_session.result(_review=True)
    assert review.values['CUSTOM_DESTINATION'] == 'https://custom.example/traces'
    assert 'TRACE_MODE' not in tracing_session.schema.fields
    assert EXPORT_FIELDS.difference({'TRACELOOP_TRACE_CONTENT'}).isdisjoint(review.active_fields)
    assert 'TRACELOOP_TRACE_CONTENT' in review.active_fields
    assert 'LOG_CONTENT' in review.active_fields
    assert not review.errors


def test_shared_schema_has_no_bundled_backend_fields():
    shared = load_shared_schema()
    assert shared.fields['TRACE_PROVIDER'].default.endswith('/telemetry/trace_adapters/provider.py:create_tracing_provider')
    assert not {'TRACE_MODE', 'TRACE_EXPORT_ENDPOINT', 'LANGFUSE_PUBLIC_KEY'} & shared.fields.keys()
