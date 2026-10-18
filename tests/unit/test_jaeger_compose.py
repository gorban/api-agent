"""Controlled local Jaeger storage and viewer publication contracts."""

import json
from pathlib import Path
import shutil

import pytest

from api_agent.runtime.compose import (ComposeError, plan_compose, snapshot_compose,
                                       validate_document)
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


@pytest.mark.parametrize('mutation', ['ephemeral', 'path', 'query', 'exporter'])
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
