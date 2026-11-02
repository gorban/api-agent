"""Sidecar decisions and isolation checks use synthetic engine evidence."""
import json
from types import MappingProxyType

import pytest

from api_agent.runtime.compose import ComposeError
from api_agent.runtime.tool_policy import sidecar_policy, isolation_fragment, verify_isolation
from test_infrastructure_plan import service, plan

POLICY = dict(version=1, runtime='runsc', user='65532:65532', memory_mb=128, cpus=.5, pids_limit=32)


def inspected():
    return {'Config': {'User': '65532:65532'}, 'HostConfig': {
        'Runtime': 'runsc', 'Privileged': False, 'ReadonlyRootfs': True,
        'CapDrop': ['ALL'], 'CapAdd': None, 'SecurityOpt': ['no-new-privileges:true'],
        'Memory': 128 * 1024 * 1024, 'NanoCpus': 500000000, 'PidsLimit': 32,
        'NetworkMode': 'owned_default',
        'Tmpfs': {'/tmp': 'rw,noexec,nosuid,nodev,size=16777216'}}}


@pytest.mark.parametrize('value,source,profile,mode', [
    ('', 'default', 'local', 'managed-local'),
    ('https://example.test/service', 'cli', 'local', 'explicit-external'),
    ('http://127.0.0.1:8090', 'environment', 'deployment', 'deployment-sidecar')])
def test_sidecar_policy_retains_original_intent(value, source, profile, mode):
    result = sidecar_policy(value, source=source, profile=profile)
    assert (result.mode, result.configured, result.source) == (mode, value, source)
    assert value not in repr(result) or not value


def test_unused_tool_needs_no_address():
    assert sidecar_policy(None, source='default', profile='deployment', selected=False).mode == 'unused'


@pytest.mark.parametrize('value,source', [('', 'environment'), ('http://localhost', 'default'),
                                         ('http://localhost', 'cli')])
def test_deployment_requires_environment_provenance(value, source):
    with pytest.raises(ComposeError, match='explicit nonempty environment'):
        sidecar_policy(value, source=source, profile='deployment')


@pytest.mark.parametrize('value', [None, ' ', '\thttp://localhost', 'http://localhost ',
    'http://user@localhost', 'http://:secret@localhost', 'file:///tmp/service',
    'http://localhost:bad', 'http://localhost:0', 'http://localhost?',
    'http://localhost#', 'http://local\\host', 'http://localhost/\n',
    'http://localhost/é', 'http://[invalid', 'http://[::1]garbage', 'http://[::1]:'])
def test_invalid_addresses_rejected(value):
    with pytest.raises(ComposeError):
        sidecar_policy(value, source='environment')


@pytest.mark.parametrize('options', [dict(source=None), dict(source='default', profile='unknown')])
def test_unsupported_policy_rejected(options):
    with pytest.raises(ComposeError):
        sidecar_policy('', **options)


def test_isolation_emits_only_finite_controls_and_integrates_with_planner():
    fragment = dict(service().compose, isolation=MappingProxyType(POLICY))
    result = plan((service(compose=MappingProxyType(fragment)),))
    name = result.conditional[0].compose_name
    spec = json.loads(result.assets['compose.json'])['services'][name]
    assert all(spec[key] == value for key, value in isolation_fragment(POLICY).items())
    assert spec['read_only'] is True
    verify_isolation(spec, inspected(), network='owned_default')
    assert 'privileged' not in spec


@pytest.mark.parametrize('change', [dict(version=True), dict(version=2), dict(runtime='runc'),
    dict(extra=True), dict(user='0:1000'), dict(user='1000:0'), dict(user=1000),
    dict(memory_mb=True), dict(memory_mb=15), dict(memory_mb=8193),
    dict(cpus=True), dict(cpus=float('nan')), dict(cpus=float('inf')), dict(cpus=.01),
    dict(cpus=9), dict(pids_limit=7), dict(pids_limit=513), dict(pids_limit=True)])
def test_isolation_declaration_rejections(change):
    with pytest.raises(ComposeError):
        isolation_fragment(dict(POLICY, **change))


@pytest.mark.parametrize('policy', [None, {}, [], {'runtime': 'runsc'}])
def test_incomplete_isolation_rejected(policy):
    with pytest.raises(ComposeError):
        isolation_fragment(policy)


@pytest.mark.parametrize('key,value', [
    ('Runtime', 'runc'), ('Privileged', True), ('ReadonlyRootfs', False),
    ('CapDrop', []), ('CapAdd', ['SYS_ADMIN']), ('SecurityOpt', []),
    ('Memory', 0), ('NanoCpus', 0), ('PidsLimit', 0), ('NetworkMode', 'host'),
    ('NetworkMode', 'container'), ('NetworkMode', 'container:' + 'a' * 64),
    ('NetworkMode', ''), ('NetworkMode', 'none'), ('Tmpfs', {})])
def test_engine_controls_independently_rejected(key, value):
    record = inspected()
    record['HostConfig'][key] = value
    with pytest.raises(ComposeError, match='Engine isolation'):
        verify_isolation(isolation_fragment(POLICY), record, network='owned_default')


@pytest.mark.parametrize('record', [{}, {'HostConfig': None},
    dict(inspected(), Config={'User': '0'})])
def test_malformed_or_root_engine_evidence_rejected(record):
    with pytest.raises(ComposeError):
        verify_isolation(isolation_fragment(POLICY), record, network='owned_default')


def test_unisolated_existing_services_unchanged():
    verify_isolation({}, {}, network='owned_default')
    record = inspected()
    record['HostConfig']['CapAdd'] = []
    verify_isolation(isolation_fragment(POLICY), record, network='owned_default')

# Reuse the existing lifecycle fixture instead of inventing a second manager.
from test_infrastructure_lifecycle import synthetic
from api_agent.runtime.prerequisites import PrerequisiteError


def isolated_manager(synthetic, monkeypatch, runtimes):
    manager, engine, _, _, _ = synthetic
    fragment = isolation_fragment(POLICY)
    for name in manager.conditional:
        manager.specs[name].update(fragment)
        manager.document['services'][name].update(fragment)
    original = engine.run
    def run(command, **options):
        if command[1] == 'info':
            engine.calls.append((command, options))
            return runtimes
        result = original(command, **options)
        for record in engine.records.values():
            record['HostConfig'] = dict(inspected()['HostConfig'], NetworkMode=manager.scope.project + '_default')
            record['Config']['User'] = '65532:65532'
        return result
    monkeypatch.setattr(manager, 'runner', run)
    return manager, engine


def test_runtime_capability_and_inspection_cover_actual_acquire_release(synthetic, monkeypatch):
    manager, engine = isolated_manager(synthetic, monkeypatch, '{"runsc":{}}')
    lease = manager.acquire()
    assert lease.container_ids
    assert engine.calls[0][0][1] == 'info'
    manager.release()
    assert not any(record['State']['Running'] for record in engine.records.values())


@pytest.mark.parametrize('runtimes', ['{}', '[]', '{invalid'])
def test_missing_or_invalid_runtime_rejects_before_start(synthetic, monkeypatch, runtimes):
    manager, engine = isolated_manager(synthetic, monkeypatch, runtimes)
    with pytest.raises(PrerequisiteError, match='runtime'):
        manager.acquire()
    assert not engine.records
    assert not any('up' in command for command, _ in engine.calls)


def test_runtime_inspection_failure_uses_typed_lifecycle_error(synthetic, monkeypatch):
    manager, engine = isolated_manager(synthetic, monkeypatch, '{"runsc":{}}')
    manager.acquire()
    name = next(iter(manager.conditional))
    record = next(item for item in engine.records.values()
                  if item['Config']['Labels']['com.docker.compose.service'] == name)
    monkeypatch.setattr(manager, 'runner', engine.run)
    record['HostConfig']['Runtime'] = 'runc'
    with pytest.raises(PrerequisiteError, match='isolation'):
        manager.verify_conditional_storage(name, record['Id'], 100)


@pytest.mark.parametrize('network', [None, ''])
def test_expected_network_must_be_explicit(network):
    with pytest.raises(ComposeError):
        verify_isolation(isolation_fragment(POLICY), inspected(), network=network)


def test_ipv6_external_address():
    assert sidecar_policy('http://[::1]:8000', source='cli').mode == 'explicit-external'


def test_bad_runtime_rejects_before_any_readiness_traffic(synthetic, monkeypatch):
    manager, engine = isolated_manager(synthetic, monkeypatch, '{"runsc":{}}')
    good_runner = manager.runner
    def run(command, **options):
        result = good_runner(command, **options)
        for record in engine.records.values():
            record['HostConfig']['Runtime'] = 'runc'
        return result
    monkeypatch.setattr(manager, 'runner', run)
    monkeypatch.setattr('api_agent.runtime.prerequisites._identity_response',
                        lambda *args, **kwargs: pytest.fail('readiness reached before isolation'))
    with pytest.raises(PrerequisiteError, match='isolation'):
        manager.acquire()


def test_explicit_status_readiness_admission_and_plan():
    from api_agent.runtime.infrastructure_contract import _readiness, Readiness
    from dataclasses import replace
    rule = _readiness({'kind': 'http-status', 'target': 'api/ready'})
    result = plan((replace(service(), readiness=rule, seed=None),))
    assert result.conditional[0].readiness.kind == 'http-status'
    assert result.conditional[0].readiness.url.endswith('/ready')
    with pytest.raises(ComposeError, match='JSON identity'):
        plan((replace(service(), readiness=Readiness('http-status', 'api/ready',
            identity_field='id', identity_value='server')),))


@pytest.mark.parametrize('extra', [{}, {'target': 'api/ready', 'identity_field': ''},
                                    {'target': 'api/ready', 'identity_value': ''}])
def test_status_readiness_cannot_pretend_to_prove_identity(extra):
    from api_agent.runtime.infrastructure_contract import _readiness, InfrastructureDeclarationError
    with pytest.raises(InfrastructureDeclarationError):
        _readiness({'kind': 'http-status', **extra})


@pytest.mark.parametrize('status,body,expected', [(200, b'Healthy', True),
    (200, b'', True), (503, b'Unhealthy', False), (302, b'', False),
    (200, b'x' * 65537, False)])
def test_status_only_probe_preserves_finite_status_and_body_checks(monkeypatch, status, body, expected):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from api_agent.runtime.infrastructure_plan import ResolvedCheck
    from api_agent.runtime import prerequisites as p
    @contextmanager
    def response(*args, **kwargs):
        yield SimpleNamespace(status=status, read=lambda limit: body)
    monkeypatch.setattr(p, '_identity_response', response)
    assert p._http_identity(ResolvedCheck('http-status', 'http://127.0.0.1:8000/ready'), 1) is expected


def test_status_readiness_rejects_redirect_even_when_requested_status_matches(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from api_agent.runtime.infrastructure_plan import ResolvedCheck
    from api_agent.runtime import prerequisites as p
    @contextmanager
    def response(*args, **kwargs):
        yield SimpleNamespace(status=302, read=lambda limit: b'')
    monkeypatch.setattr(p, '_identity_response', response)
    assert not p._http_identity(ResolvedCheck('http-status', 'http://127.0.0.1:8000', status=302), 1)


@pytest.mark.parametrize('interruption', ['deadline', 'cancelled'])
def test_status_readiness_rechecks_budget_after_body(monkeypatch, interruption):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from api_agent.runtime.infrastructure_plan import ResolvedCheck
    from api_agent.runtime import prerequisites as p
    state = {'time': 0, 'cancelled': False}
    monkeypatch.setattr(p.time, 'monotonic', lambda: state['time'])
    def read(limit):
        state['time'] = 2 if interruption == 'deadline' else 0
        state['cancelled'] = interruption == 'cancelled'
        return b'Healthy'
    @contextmanager
    def response(*args, **kwargs):
        yield SimpleNamespace(status=200, read=read)
    monkeypatch.setattr(p, '_identity_response', response)
    check = ResolvedCheck('http-status', 'http://127.0.0.1:8000')
    if interruption == 'cancelled':
        with pytest.raises(p.PrerequisiteCancelled):
            p._http_identity(check, 1, cancelled=lambda: state['cancelled'])
    else:
        assert not p._http_identity(check, 1, cancelled=lambda: state['cancelled'])


from test_passive_inventory import source


def test_passive_recovery_authority_survives_an_empty_selected_plan(source):
    from api_agent.config.declarations import ConfigSchema
    from api_agent.runtime.infrastructure_plan import plan_infrastructure
    from api_agent.runtime.inventory import capture_inventory
    catalog = capture_inventory(source.context)
    planned = plan_infrastructure({}, ConfigSchema({}), (), source.scope, inventory=catalog)
    assert planned.services == ()
    assert planned.inventory.verify(source.scope) == catalog.pairs
    assert len(planned.inventory.pairs) == 3
    assert not (source.fixture / 'ABSENT').exists()
