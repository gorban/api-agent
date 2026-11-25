"""Unrelated engine objects never expand owned container discovery."""
from contextlib import nullcontext
import json

import pytest

from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.runtime.ownership import StackScope


CURRENT = '1' * 16
LEGACY = '2' * 16
OWNED = 'a' * 64
FOREIGN = 'b' * 64
NONCE = 'c' * 32
FINGERPRINT = 'd' * 64


def record(identifier, scope=CURRENT, *, nonce=NONCE):
    return {'Id': identifier, 'Name': '/test',
            'Config': {'Labels': {'api-agent.managed': 'true', 'api-agent.scope': scope,
                'api-agent.profile': 'otel', 'api-agent.acquisition': nonce,
                'api-agent.config': FINGERPRINT, 'com.docker.compose.project': 'sa-' + scope,
                'com.docker.compose.service': 'service'}},
            'State': {'Running': False, 'Status': 'exited'}}


def test_inventory_queries_only_current_and_legacy_scope(monkeypatch):
    runtime = ContainerRuntime('docker', StackScope(CURRENT, (LEGACY,)))
    seen = []
    def engine(args, **_kwargs):
        seen.append(args)
        if args[0] == 'ps':
            assert args in (['ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=api-agent.managed=true', '--filter', f'label=api-agent.scope={CURRENT}'],
                            ['ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=api-agent.managed=true', '--filter', f'label=api-agent.scope={LEGACY}'])
            return OWNED if f'label=api-agent.scope={CURRENT}' in args else ''
        assert args == ['inspect', OWNED]
        return json.dumps([record(OWNED)])
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    assert [item.id for item in runtime.inventory()] == [OWNED]
    assert len(seen) == 3


def test_inventory_scoped_legacy_still_refuses_migration(monkeypatch):
    runtime = ContainerRuntime('docker', StackScope(CURRENT, (LEGACY,)))
    def engine(args, **_kwargs):
        if args[0] == 'ps':
            assert any(value in args for value in (f'label=api-agent.scope={CURRENT}', f'label=api-agent.scope={LEGACY}'))
            return OWNED if f'label=api-agent.scope={LEGACY}' in args else ''
        assert args == ['inspect', OWNED]
        return json.dumps([record(OWNED, LEGACY)])
    monkeypatch.setattr(runtime, '_run', engine)
    with pytest.raises(ContainerError, match='before migration'):
        runtime.inventory()


def test_dispose_checks_exact_pinned_id_even_if_labels_change(monkeypatch, tmp_path):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    calls = []
    def engine(args, **_kwargs):
        calls.append(args)
        if args[0] == 'ps':
            assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', f'id={OWNED}']
            return OWNED
        if args[0] == 'inspect':
            changed = record(OWNED)
            changed['Config']['Labels']['api-agent.scope'] = 'foreign'
            return json.dumps([changed])
        raise AssertionError('must not mutate objects or list unrelated containers')
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    with pytest.raises(ContainerError, match='ownership changed'):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})
    assert len(calls) == 2


def test_dispose_rejects_ambiguous_id_filter_result(monkeypatch, tmp_path):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    def engine(args, **_kwargs):
        assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', f'id={OWNED}']
        return FOREIGN
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    with pytest.raises(ContainerError, match='inventory cannot be verified'):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})


@pytest.mark.parametrize('response', [f'{OWNED} {OWNED}', 'bad-id', f'{OWNED} {FOREIGN}'])
def test_inventory_rejects_bad_ids_or_inspect_mismatch(monkeypatch, response):
    runtime = ContainerRuntime('docker', StackScope(CURRENT))
    def engine(args, **_kwargs):
        if args[0] == 'ps':
            assert f'label=api-agent.scope={CURRENT}' in args
            return response
        return json.dumps([record(OWNED)])
    monkeypatch.setattr(runtime, '_run', engine)
    with pytest.raises(ContainerError, match='invalid container IDs|identities changed'):
        runtime.inventory()


def test_inventory_deduplicates_legacy_scope_queries(monkeypatch):
    runtime = ContainerRuntime('docker', StackScope(CURRENT, (LEGACY, LEGACY, CURRENT)))
    seen = []
    monkeypatch.setattr(runtime, '_run', lambda args, **_: seen.append(args) or '')
    assert runtime.inventory() == ()
    assert len(seen) == 2


def test_dispose_live_lease_precedes_engine_access(monkeypatch, tmp_path):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext({OWNED}))
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    monkeypatch.setattr(runtime, '_run', lambda *_a, **_k: pytest.fail('engine must not be called'))
    with pytest.raises(ContainerError, match='active invocation'):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})


@pytest.mark.parametrize('change', ['session', 'distrobox', 'nonce', 'fingerprint'])
def test_dispose_guard_failures_never_mutate(monkeypatch, tmp_path, change):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    item = record(OWNED)
    if change == 'distrobox':
        item['Config']['Labels']['distrobox.name'] = 'protected'
    if change == 'nonce':
        item['Config']['Labels']['api-agent.acquisition'] = 'e' * 32
    if change == 'fingerprint':
        item['Config']['Labels']['api-agent.config'] = 'f' * 64
    def engine(args, **_kwargs):
        if args[0] == 'ps':
            assert args[-1] == f'id={OWNED}'
            return OWNED
        if args[0] == 'inspect':
            return json.dumps([item])
        pytest.fail('must not mutate')
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: {OWNED} if change == 'session' else set())
    with pytest.raises(ContainerError):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})


def test_dispose_absent_id_still_runs_network_guard(monkeypatch, tmp_path):
    from api_agent.runtime.prerequisites import _NetworkCleanup
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    seen = []
    def engine(args, **_kwargs):
        assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', f'id={OWNED}']
        return ''
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    monkeypatch.setattr(_NetworkCleanup, '_prune_networks', lambda _self, _deadline, **kw: seen.append(kw))
    runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})
    assert seen == [{'required_labels': {'api-agent.acquisition': NONCE}}]


@pytest.mark.parametrize('response', ['invalid', f'{OWNED} {OWNED}', f'{OWNED} {FOREIGN}'])
def test_dispose_rejects_malformed_duplicate_or_extra_id(monkeypatch, tmp_path, response):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    def engine(args, **_kwargs):
        assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', f'id={OWNED}']
        return response
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    with pytest.raises(ContainerError, match='inventory cannot be verified'):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})


@pytest.mark.parametrize('inspected', [[record(FOREIGN)], [record(OWNED), record(OWNED)]])
def test_inventory_requires_exact_inspection_identity_and_count(monkeypatch, inspected):
    runtime = ContainerRuntime('docker', StackScope(CURRENT))
    def engine(args, **_kwargs):
        if args[0] == 'ps':
            assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=api-agent.managed=true', '--filter', f'label=api-agent.scope={CURRENT}']
            return OWNED
        assert args == ['inspect', OWNED]
        return json.dumps(inspected)
    monkeypatch.setattr(runtime, '_run', engine)
    with pytest.raises(ContainerError, match='identities changed'):
        runtime.inventory()


@pytest.mark.parametrize('inspected', [[record(FOREIGN)], [record(OWNED), record(OWNED)]])
def test_dispose_requires_exact_inspection_identity_and_count(monkeypatch, tmp_path, inspected):
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path)
    def engine(args, **_kwargs):
        if args[0] == 'ps':
            assert args == ['ps', '--all', '--quiet', '--no-trunc', '--filter', f'id={OWNED}']
            return OWNED
        assert args == ['inspect', OWNED]
        return json.dumps(inspected)
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.prerequisites.guarded_cleanup', lambda *_: nullcontext(frozenset()))
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    with pytest.raises(ContainerError, match='ownership changed'):
        runtime.dispose([OWNED], nonce=NONCE, fingerprints={'service': FINGERPRINT})


@pytest.mark.parametrize(('project', 'checkout', 'listed'), [
    ('sa-' + CURRENT, 'app', True), ('sa-app-11111111', 'app', True), ('sa-' + CURRENT, '', True),
    ('sa-app-22222222', 'app', False), ('sa-other-11111111', 'app', False), ('sa-app-11111111', '', False)])
def test_inventory_lists_a_container_only_when_its_labels_reproduce_its_project_name(
        monkeypatch, project, checkout, listed):
    row = record(OWNED)
    row['Config']['Labels'].update({'com.docker.compose.project': project, 'api-agent.checkout': checkout})
    runtime = ContainerRuntime('docker', StackScope(CURRENT, checkout='app'))
    def engine(args, **_kwargs):
        return OWNED if args[0] == 'ps' else json.dumps([row])
    monkeypatch.setattr(runtime, '_run', engine)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())

    assert [item.id for item in runtime.inventory()] == ([OWNED] if listed else [])
