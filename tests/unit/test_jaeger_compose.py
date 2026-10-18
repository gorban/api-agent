"""Controlled local Jaeger storage and viewer publication contracts."""

import json
from pathlib import Path
import shutil

import pytest

from api_agent.runtime.compose import (ComposeError, plan_compose, snapshot_compose,
                                       validate_document, rebind_reporting_plan)
from test_compose import Scope


def test_jaeger_uses_persistent_badger_and_reviewed_port(tmp_path):
    plan = plan_compose({'TRACE_MODE': 'OTEL', 'JAEGER_VIEWER_PORT': 17686}, Scope())
    document = json.loads(plan.assets['compose.json'])
    jaeger = document['services']['jaeger']
    storage = json.loads(plan.assets['jaeger-config.json'])

    assert plan.services == ('jaeger',)
    assert plan.environment['SA_JAEGER_VIEWER_PORT'] == '17686'
    assert jaeger['ports'] == ['127.0.0.1:4318:4318',
                               '127.0.0.1:${SA_JAEGER_VIEWER_PORT:?controlled local development value required}:16686']
    assert jaeger['volumes'][0] == 'jaeger-badger:/tmp'
    assert 'user' not in jaeger  # Preserve the pinned image's non-root UID.
    assert document['volumes']['jaeger-badger']['labels']['api-agent.profile'] == 'otel'
    assert storage['extensions']['jaeger_storage']['backends']['local_traces']['badger']['ephemeral'] is False
    assert storage['receivers']['otlp']['protocols']['http']['endpoint'] == '0.0.0.0:4318'
    assert 'jaeger-config.json' in plan.assets
    command, environment = snapshot_compose(plan, tmp_path)
    assert Path(environment['SA_JAEGER_CONFIG']).read_bytes() == plan.assets['jaeger-config.json']
    assert command[-2:] == ('--profile', 'otel')


@pytest.mark.parametrize('port', [0, 65536, True, '16686', -1])
def test_jaeger_viewer_port_rejects_invalid_values(port):
    with pytest.raises(ComposeError, match='JAEGER_VIEWER_PORT'):
        plan_compose({'TRACE_MODE': 'OTEL', 'JAEGER_VIEWER_PORT': port}, Scope())


@pytest.mark.parametrize('config', [
    {'TRACE_MODE': 'OTEL', 'JAEGER_VIEWER_PORT': 4318},
    {'TRACE_MODE': 'OTEL', 'ALLURE_S3_USE_LOCALSTACK': True, 'JAEGER_VIEWER_PORT': 4566},
    {'TRACE_MODE': 'OTEL', 'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_LOCALSTACK_PORT': 4318},
])
def test_selected_host_port_collisions_are_rejected_before_compose(config):
    with pytest.raises(ComposeError, match='host ports must not conflict'):
        plan_compose(config, Scope())


def test_unselected_localstack_port_does_not_conflict_with_jaeger():
    plan = plan_compose({'TRACE_MODE': 'OTEL', 'ALLURE_LOCALSTACK_PORT': 4318}, Scope())
    assert plan.services == ('jaeger',)


@pytest.mark.parametrize('mutation', ['volume', 'config_mount', 'config_command'])
def test_jaeger_rejects_missing_persistence_contract(mutation):
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, Scope())
    document = json.loads(plan.assets['compose.json'])
    if mutation == 'volume':
        document['services']['jaeger']['volumes'].pop(0)
    elif mutation == 'config_mount':
        document['services']['jaeger']['volumes'].pop()
    else:
        document['services']['jaeger']['command'] = []

    with pytest.raises(ComposeError, match='controlled persistent storage'):
        validate_document(document, Scope(), ('otel',))


@pytest.mark.parametrize('mutation', ['viewer_destination', 'ingest_destination',
                                     'extra_mapping', 'reordered_mappings'])
def test_jaeger_rejects_port_mapping_drift_before_start(tmp_path, mutation):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    path = directory / 'compose.json'
    document = json.loads(path.read_text())
    ports = document['services']['jaeger']['ports']
    if mutation == 'viewer_destination':
        ports[1] = ports[1].replace(':16686', ':4318')
    elif mutation == 'ingest_destination':
        ports[0] = '127.0.0.1:4318:16686'
    elif mutation == 'extra_mapping':
        ports.append('127.0.0.1:16687:16686')
    else:
        ports.reverse()
    path.write_text(json.dumps(document))

    with pytest.raises(ComposeError, match='controlled persistent storage, configuration and ports'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)


@pytest.mark.parametrize('mutation', ['ephemeral', 'path', 'query', 'exporter',
                                     'pipeline_debug_only', 'pipeline_exporters_wrong_type'])
def test_jaeger_rejects_nonpersistent_or_disconnected_storage(tmp_path, mutation):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    config = directory / 'jaeger-config.json'
    document = json.loads(config.read_text())
    backend = document['extensions']['jaeger_storage']['backends']['local_traces']['badger']
    if mutation == 'ephemeral':
        backend['ephemeral'] = True
    elif mutation == 'path':
        backend['directories']['keys'] = '/tmp/other'
    elif mutation == 'query':
        document['extensions']['jaeger_query']['storage']['traces'] = 'other'
    elif mutation == 'pipeline_debug_only':
        document['service']['pipelines']['traces']['exporters'] = ['debug']
    elif mutation == 'pipeline_exporters_wrong_type':
        document['service']['pipelines']['traces']['exporters'] = 'jaeger_storage_exporter'
    else:
        document['exporters']['jaeger_storage_exporter']['trace_storage'] = 'other'
    config.write_text(json.dumps(document))

    with pytest.raises(ComposeError, match='storage must be persistent'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'malformed', 'wrong_type'])
def test_controlled_jaeger_config_must_be_real_valid_file(tmp_path, damage):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    config = directory / 'jaeger-config.json'
    if damage == 'missing':
        config.unlink()
    elif damage == 'symlink':
        alternate = directory / 'valid-alternate.json'
        alternate.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(alternate)
    elif damage == 'malformed':
        config.write_text('{')
    else:
        config.write_text('[]')

    with pytest.raises(ComposeError, match='Jaeger configuration'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)


@pytest.mark.parametrize('mutation', ['unpublished_receiver', 'missing_storage_extension',
                                     'listener_drift', 'processor_drift'])
def test_published_jaeger_receiver_must_reach_persistent_storage(tmp_path, mutation):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    config = directory / 'jaeger-config.json'
    document = json.loads(config.read_text())
    if mutation == 'unpublished_receiver':
        document['receivers']['otlp/storage'] = {
            'protocols': {'http': {'endpoint': '0.0.0.0:4319'}}}
        document['service']['pipelines']['traces']['receivers'] = ['otlp/storage']
        document['exporters']['debug'] = {}
        document['service']['pipelines']['traces/debug'] = {
            'receivers': ['otlp'], 'exporters': ['debug']}
    elif mutation == 'missing_storage_extension':
        document['service']['extensions'].remove('jaeger_storage')
    elif mutation == 'listener_drift':
        document['receivers']['otlp']['protocols']['http']['endpoint'] = '0.0.0.0:4319'
    else:
        document['processors']['batch'] = {'send_batch_size': 1}
    config.write_text(json.dumps(document))

    with pytest.raises(ComposeError, match='storage must be persistent'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)


@pytest.mark.parametrize(('key', 'value'), [
    ('image', 'docker.io/library/python:3.13-slim'),
    ('entrypoint', ['python', '-m', 'http.server']),
    ('user', '0'),
    ('environment', {'SPAN_STORAGE_TYPE': 'memory'}),
    ('healthcheck', {'test': ['CMD', 'true']}),
    ('profiles', ['langfuse']),
])
def test_jaeger_requires_the_reviewed_service_identity(key, value):
    document = json.loads(plan_compose({}, Scope()).assets['compose.json'])
    document['services']['jaeger'][key] = value

    with pytest.raises(ComposeError, match='controlled persistent storage'):
        validate_document(document, Scope(), ('otel',))


@pytest.mark.parametrize(('section', 'value'), [
    ('jaeger_query', {'storage': {'traces': 'local_traces'},
                      'http': {'endpoint': '0.0.0.0:16687'}}),
    ('jaeger_query', {'storage': {'traces': 'local_traces'},
                      'http': {'endpoint': '127.0.0.1:16686'}}),
    ('jaeger_storage', {'backends': {'local_traces': {'badger': {
        'directories': {'keys': '/tmp/jaeger/keys', 'values': '/tmp/jaeger/values'},
        'ephemeral': False, 'ttl': {'spans': '1h'}}}}}),
])
def test_jaeger_requires_reviewed_query_binding_and_retention(tmp_path, section, value):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    path = directory / 'jaeger-config.json'
    document = json.loads(path.read_text())
    document['extensions'][section] = value
    path.write_text(json.dumps(document))

    with pytest.raises(ComposeError, match='storage must be persistent'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)


def test_rebinding_ownership_preserves_reviewed_jaeger_assets():
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, Scope())
    other = Scope(id='fedcba9876543210', project='sa-fedcba9876543210')

    rebound = rebind_reporting_plan(plan, other)

    assert rebound.assets is plan.assets
    assert rebound.environment['SA_SCOPE'] == other.id
    assert rebound.command[rebound.command.index('--project-name') + 1] == other.project
    assert rebound.services == ('jaeger',)


@pytest.mark.parametrize('ephemeral', [0, 0.0])
def test_jaeger_ephemeral_requires_json_false(tmp_path, ephemeral):
    source = plan_compose({}, Scope()).directory
    directory = tmp_path / 'compose'
    shutil.copytree(source, directory)
    path = directory / 'jaeger-config.json'
    document = json.loads(path.read_text())
    document['extensions']['jaeger_storage']['backends']['local_traces']['badger']['ephemeral'] = ephemeral
    path.write_text(json.dumps(document))

    with pytest.raises(ComposeError, match='storage must be persistent'):
        plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), compose_directory=directory)
