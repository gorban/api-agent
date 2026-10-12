"""Selected pack managers reach startup and lazy execution without vault access."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.auth.managers import load_manager_registry
from api_agent.config import schema_from_dict
from api_agent.config.session import ConfigSession
from api_agent.testing import provider_setup
from api_agent.testing.setup_backend import SetupBackend, _SetupOwner
from api_agent.tui.app import ApiAgentApp


def contributed_pack(tmp_path):
    pack = tmp_path / 'pack'
    cfg = pack / 'cfg'
    cfg.mkdir(parents=True)
    adapter = pack / 'adapter.py'
    adapter.write_text("version = 1\ndef command(source, kind, cli):\n    return [cli, kind, source.vault, source.user]\ndef parse(output, kind):\n    return output.rstrip('\\n')\n")
    (cfg / 'password_managers.toml').write_text('version = 1\n[[managers]]\nid = "fixture"\nlabel = "Fixture vault"\nimplementation = "../adapter.py"\ncli_default = "fixture-cli"\n')
    return pack


def descriptors():
    return {'password_manager': {'manager': 'fixture', 'cli_path': 'fixture-cli'},
            'roles': {'LLM User': [{'vault': 'Example', 'user': 'account'}]}}


def test_custom_manager_satisfies_startup_and_account_picker(tmp_path):
    pack = contributed_pack(tmp_path)
    base = tmp_path / 'base'
    base.mkdir()
    context = SimpleNamespace(base_root=base, owner_root=base, pack_root=pack,
        launcher_root=base, external_pack=True, dotenv_path=base / '.env',
        anchors={}, credential_role_fields=())
    schema = schema_from_dict({'version': 1, 'fields': {
        'UNATTENDED_AUTH': {'type': 'jsonobject'},
        'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True},
        'NO_STD_IN': {'type': 'bool', 'default': False}}})
    session = ConfigSession(context, schema, environ={'UNATTENDED_AUTH': json.dumps(descriptors())})
    app = ApiAgentApp(context, schema, session=session, environ={},
                     container_runtime=SimpleNamespace(inventory=lambda: ()))
    assert app.credential_roles() == ('LLM User',)
    assert not app.auth_setup_needed()
    assert app.account_options() == ((0, 'account'),)
    (pack / 'cfg/password_managers.toml').unlink()
    assert app.auth_setup_needed()
    assert app.account_options() == ()
    # Inactive descriptors remain stored, rather than replaced by another manager.
    assert session.result(_review=True).values['UNATTENDED_AUTH']['password_manager']['manager'] == 'fixture'


@pytest.mark.parametrize('explicit_registry', [False, True])
def test_lazy_consumer_uses_pack_registry_not_owner_directory(tmp_path, monkeypatch, explicit_registry):
    pack = contributed_pack(tmp_path)
    owner_root = tmp_path / 'different-owner'
    owner_root.mkdir()
    kwargs = ({'password_manager_registry': load_manager_registry(pack_root=pack)}
              if explicit_registry else {'password_manager_pack_root': pack})
    consumer = provider_setup.IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User',
        'UNATTENDED_AUTH': descriptors()}, owner_root, **kwargs)
    from api_agent.auth import credentials
    calls = []
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda cli: '/synthetic/fixture-cli')
    monkeypatch.setattr(credentials, '_run', lambda argv, timeout: calls.append(argv) or 'synthetic-key\n')
    try:
        scoped = consumer._credentials(0)
        value = consumer.loop.run_until_complete(scoped(0, 'password'))
        assert value == 'synthetic-key'
        assert calls == [['/synthetic/fixture-cli', 'password', 'Example', 'account']]
        assert all(task.done() for task in consumer._credential_tasks)
    finally:
        consumer.close()
    assert consumer.loop.is_closed()


def test_owner_directory_fallback_and_explicit_empty_registry(tmp_path):
    pack = contributed_pack(tmp_path)
    consumer = provider_setup.IntegrationLLM({}, pack)
    try:
        assert consumer.password_manager_registry.get('fixture').label == 'Fixture vault'
    finally:
        consumer.close()
    from api_agent.auth.managers import PasswordManagerRegistry
    empty = PasswordManagerRegistry(())
    consumer = provider_setup.IntegrationLLM({}, pack, password_manager_registry=empty)
    try:
        assert consumer.password_manager_registry is empty
    finally:
        consumer.close()


def test_shared_backend_freezes_registry_and_includes_it_in_identity(tmp_path, monkeypatch):
    pack = contributed_pack(tmp_path)
    registry = load_manager_registry(pack_root=pack)
    backend = SetupBackend({}, tmp_path, password_manager_registry=registry)
    baseline = SetupBackend({}, tmp_path)
    assert backend.password_manager_registry is registry
    assert backend.selected_identity != baseline.selected_identity
    with pytest.raises(AttributeError):
        backend.password_manager_registry = None
    calls = []
    class SyntheticProvider:
        def __init__(self, config, root, **kwargs):
            calls.append((root, kwargs['password_manager_registry'], kwargs['loop']))
    monkeypatch.setattr(provider_setup, 'IntegrationLLM', SyntheticProvider)
    (pack / 'cfg/password_managers.toml').unlink()
    async def create_owner():
        _SetupOwner(backend)
        assert calls == [(tmp_path.resolve(), registry, asyncio.get_running_loop())]
    asyncio.run(create_owner())
    backend.close()
    baseline.close()


@pytest.mark.parametrize('bad_root', [4, 'relative/pack'])
def test_snapshot_keeps_selected_pack_location_and_rejects_invalid_root(tmp_path, monkeypatch, bad_root):
    pack = contributed_pack(tmp_path)
    directory = tmp_path / 'run'
    directory.mkdir()
    context = SimpleNamespace(owner_root=tmp_path, pack_root=pack, external_pack=True)
    path = provider_setup.write_launch_snapshot(directory, context, {})
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    data = provider_setup._read_snapshot()
    assert data['password_manager_pack_root'] == str(pack)
    data['password_manager_pack_root'] = bad_root
    path.write_text(json.dumps(data))
    with pytest.raises(provider_setup.ContractError, match='context is unavailable'):
        provider_setup._read_snapshot()


def test_worker_fixture_uses_snapshot_pack_location(tmp_path, monkeypatch):
    pack = contributed_pack(tmp_path)
    monkeypatch.setattr(provider_setup, '_read_snapshot', lambda: {
        'config': {}, 'owner_root': str(tmp_path), 'password_manager_pack_root': str(pack),
        'password_manager_definitions': provider_setup._manager_definitions(load_manager_registry(pack_root=pack))})
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    fixture = provider_setup.llm_provider.__wrapped__()
    consumer = next(fixture)
    assert consumer.password_manager_registry.get('fixture').implementation == pack / 'adapter.py'
    with pytest.raises(StopIteration):
        next(fixture)
    assert consumer.closed and consumer.loop.is_closed()


@pytest.mark.parametrize('change', ['adapter', 'remove', 'invalid', 'escape', 'cli'])
def test_debug_worker_rejects_declaration_drift_before_allocating_loop(tmp_path, monkeypatch, change):
    pack = contributed_pack(tmp_path)
    run = tmp_path / 'run'
    run.mkdir()
    context = SimpleNamespace(owner_root=tmp_path / 'other-owner', pack_root=pack, external_pack=True)
    path = provider_setup.write_launch_snapshot(run, context, {'DEBUG': True})
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    data = provider_setup._read_snapshot()
    assert data['password_manager_definitions'][-1]['implementation'] == str(pack / 'adapter.py')
    declaration = pack / 'cfg/password_managers.toml'
    if change == 'remove':
        declaration.unlink()
    elif change == 'invalid':
        declaration.write_text('invalid toml')
    elif change == 'escape':
        (tmp_path / 'outside.py').write_text('raise AssertionError("adapter must stay inert")')
        declaration.write_text(declaration.read_text().replace('../adapter.py', '../../outside.py'))
    elif change == 'cli':
        declaration.write_text(declaration.read_text().replace('fixture-cli', 'changed-cli'))
    else:
        (pack / 'new_adapter.py').write_text('raise AssertionError("adapter must stay inert")')
        declaration.write_text(declaration.read_text().replace('../adapter.py', '../new_adapter.py'))
    monkeypatch.setattr(provider_setup.asyncio, 'new_event_loop',
                        lambda: pytest.fail('drift must be rejected before loop allocation'))
    fixture = provider_setup.llm_provider.__wrapped__()
    with pytest.raises(provider_setup.ContractError, match='declarations changed after launch; restart'):
        next(fixture)
    fixture.close()


def test_multiple_debug_workers_keep_validated_registry_after_fixture_start(tmp_path, monkeypatch):
    pack = contributed_pack(tmp_path)
    run = tmp_path / 'run'
    run.mkdir()
    context = SimpleNamespace(owner_root=tmp_path / 'other-owner', pack_root=pack, external_pack=True)
    path = provider_setup.write_launch_snapshot(run, context, {
        'DEBUG': True, 'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': descriptors()})
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    from api_agent.auth import credentials
    calls = []
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda cli: '/synthetic/fixture-cli')
    monkeypatch.setattr(credentials, '_run', lambda argv, timeout: calls.append(argv) or 'synthetic-key\n')
    fixtures = [provider_setup.llm_provider.__wrapped__() for _ in range(2)]
    workers = []
    try:
        workers = [next(fixture) for fixture in fixtures]
        assert workers[0].password_manager_registry is not workers[1].password_manager_registry
        (pack / 'cfg/password_managers.toml').unlink()
        for worker in workers:
            scoped = worker._credentials(0)
            assert worker.loop.run_until_complete(scoped(0, 'password')) == 'synthetic-key'
        assert calls == [['/synthetic/fixture-cli', 'password', 'Example', 'account']] * 2
    finally:
        for fixture in fixtures:
            fixture.close()
    assert all(worker.closed and worker.loop.is_closed() for worker in workers)


@pytest.mark.parametrize('change', ['unchanged', 'adapter', 'remove', 'before_first'])
def test_debug_run_captures_one_registry_across_repository_suites(tmp_path, monkeypatch, change):
    import io
    from api_agent.auth import credentials, managers
    from api_agent.testing import integration
    from api_agent.testing.collection import TestIdentity
    pack = contributed_pack(tmp_path)
    base, owner = tmp_path / 'base', tmp_path / 'different-owner'
    base.mkdir()
    owner.mkdir()
    context = SimpleNamespace(base_root=base, owner_root=owner, pack_root=pack, external_pack=True)
    identities = tuple(TestIdentity(f'{root.name}::test.py::test_ready', root.name,
        'test.py', 'test.py::test_ready', root=root) for root in (base, owner))
    declaration = pack / 'cfg/password_managers.toml'
    replacement = pack / 'replacement.py'
    replacement.write_text((pack / 'adapter.py').read_text().replace(
        'return [cli, kind,', "return [cli, 'changed-command', kind,"))
    captured, snapshots, calls, workers, providers, finalized, errors = [], [], [], [], [], [], []
    original_provider = provider_setup.IntegrationLLM
    class CredentialProbe(original_provider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._credential_loaded = False
            providers.append(self)
        async def _ensure(self):
            if not self._credential_loaded:
                scoped = self._credentials(0)
                assert await scoped(0, 'password') == 'synthetic-key'
                self._credential_loaded = True
    monkeypatch.setattr(provider_setup, 'IntegrationLLM', CredentialProbe)
    real_discovery = managers.registry_for_context
    def discover(selected_context):
        registry = real_discovery(selected_context)
        captured.append(registry)
        return registry
    monkeypatch.setattr(managers, 'registry_for_context', discover)
    before_count = 0
    def before_run():
        nonlocal before_count
        before_count += 1
        if change == 'before_first' and before_count == 1 or before_count == 2 and change != 'unchanged':
            if change == 'remove':
                declaration.unlink()
            else:
                declaration.write_text(declaration.read_text().replace('../adapter.py', '../replacement.py'))
    class ProcessDouble:
        def __init__(self, args, **kwargs):
            self.stdout = io.StringIO('')
            self.returncode = 0
            env = kwargs['env']
            snapshots.append(json.loads(Path(env['API_AGENT_INTEGRATION_CONTEXT']).read_text()))
            with monkeypatch.context() as worker_env:
                worker_env.setenv('API_AGENT_INTEGRATION_CONTEXT', env['API_AGENT_INTEGRATION_CONTEXT'])
                worker_env.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
                fixture = provider_setup.llm_provider.__wrapped__()
                try:
                    try:
                        worker = next(fixture)
                    except provider_setup.ContractError as error:
                        errors.append(str(error))
                        self.returncode = 1
                    else:
                        workers.append(worker)
                        worker.ensure_ready()
                finally:
                    fixture.close()
            node = args[-1]
            rows = [dict(kind='report', node=node, phase=phase,
                status='failed' if self.returncode and phase == 'setup' else 'passed',
                duration=0, traceback=errors[-1] if self.returncode and phase == 'setup' else '')
                for phase in ('setup', 'call', 'teardown')]
            rows.append(dict(kind='run_complete', exit_code=self.returncode))
            Path(env['API_AGENT_INTEGRATION_EVENTS']).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        def poll(self):
            return self.returncode
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: identities)
    monkeypatch.setattr(integration.subprocess, 'Popen', ProcessDouble)
    monkeypatch.setattr(integration, 'finalize', finalized.append)
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda cli: '/synthetic/fixture-cli')
    monkeypatch.setattr(credentials, '_run', lambda argv, timeout: calls.append(argv) or 'synthetic-key\n')
    result = integration.run_integration_tests(context, identities, accepted=True, before_run=before_run,
        config={'DEBUG': True, 'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': descriptors()})
    assert len(captured) == 1
    assert len(snapshots) == len(finalized) == 2
    assert all(snapshot['password_manager_definitions'] == provider_setup._manager_definitions(captured[0])
               for snapshot in snapshots)
    assert all(snapshot['password_manager_pack_root'] == str(pack) for snapshot in snapshots)
    expected_workers = 2 if change == 'unchanged' else 0 if change == 'before_first' else 1
    expected_calls = int(expected_workers > 0)
    assert calls == [['/synthetic/fixture-cli', 'password', 'Example', 'account']] * expected_calls
    assert len(workers) == expected_workers and all(worker.client._closed and worker.loop.is_closed() for worker in workers)
    assert len(providers) == expected_calls
    assert all(provider.password_manager_registry is captured[0] for provider in providers)
    if change == 'unchanged':
        assert result.exit_code == 0 and all(item.status == 'passed' for item in result.tests)
        assert errors == []
    else:
        assert result.exit_code == 1 and result.tests[-1].status == 'error'
        assert len(errors) == 2 - expected_workers
        assert all(error == 'Password manager declarations changed after launch; restart through launch.py.'
                   for error in errors)
        assert 'declarations changed after launch' in result.tests[-1].traceback


def test_explicit_empty_registry_snapshot_does_not_reload_declarations(tmp_path, monkeypatch):
    from api_agent.auth.managers import PasswordManagerRegistry
    monkeypatch.setattr('api_agent.auth.managers.registry_for_context',
                        lambda context: pytest.fail('explicit registry must not trigger discovery'))
    path = provider_setup.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {},
        password_manager_registry=PasswordManagerRegistry(()))
    assert json.loads(path.read_text())['password_manager_definitions'] == []
