"""Offline recorded-run controls for the shared setup publication barrier."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from api_agent.auth import credentials as helpers
from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.usage import QuotaSnapshot
from api_agent.testing import provider_setup, recorded, setup_backend
from api_agent.testing.collection import collect_tests


def _suite(tmp_path, *, config=None):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_finished.py').write_text('def test_finished():\n    assert True\n')
    provider = tmp_path / 'synthetic_provider.py'
    provider.write_text('def factory(config, services):\n    raise AssertionError("inert declaration only")\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,))
    settings = {'DEBUG': True, 'TUI_MODE': 'integration', 'TEST_PARALLEL_JOBS': 0,
        'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': tmp_path / 'allure',
        'LLM_PROVIDER': str(provider) + ':factory', **(config or {})}
    return context, settings, collect_tests(context, 'integration', config=settings)


def _loaded(implementation):
    async def close():
        pass
    return SimpleNamespace(implementation=implementation, aclose=close)


def _starting_backend(monkeypatch, entered, *, cleanup_timeout=.1):
    from api_agent.providers.setup_transport import SetupClient
    owners, clients = [], []

    class Backend(setup_backend.SetupBackend):
        def start(self):
            descriptor = super().start()
            owners.append(self)

            def request():
                client = SetupClient(descriptor, timeout=10)
                try:
                    client.ensure_ready()
                except Exception:
                    pass
                finally:
                    client.close()

            thread = threading.Thread(target=request, name='synthetic-setup-client')
            clients.append(thread)
            thread.start()
            assert entered.wait(timeout=5)
            return descriptor

    Backend.cleanup_timeout = cleanup_timeout
    monkeypatch.setattr(setup_backend, 'SetupBackend', Backend)
    return owners, clients


def test_resistant_quota_blocks_recorded_finalization_but_keeps_diagnostics(tmp_path, monkeypatch):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    owners, clients = _starting_backend(monkeypatch, entered, cleanup_timeout=.05)
    instant = datetime(2028, 1, 1, tzinfo=timezone.utc)

    class Auth:
        async def __call__(self):
            return 'synthetic-auth'

        async def aclose(self):
            pass

    class Adapter:
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def asetup(self, **kwargs):
            return Auth()

        async def asnapshot(self, *, auth):
            entered.set()
            pending = asyncio.create_task(asyncio.to_thread(release.wait))
            while True:
                try:
                    await asyncio.shield(pending)
                    break
                except asyncio.CancelledError:
                    if pending.done():
                        break
            completed.set()
            return QuotaSnapshot(10, 'window', instant + timedelta(hours=1), instant)

    async def load(*args, **kwargs):
        return _loaded(Adapter())

    monkeypatch.setattr(provider_setup, 'aload_provider', load)
    context, config, selected = _suite(tmp_path)
    persisted = []
    events = []
    storage = SimpleNamespace(persist=lambda *args: persisted.append(args))

    try:
        with pytest.raises(CleanupPendingError) as caught:
            recorded.run_recorded_integration(context, selected, config, accepted=True,
                storage=storage, on_event=events.append)
        error = caught.value
        assert not persisted and not completed.is_set()
        assert error.execution.exit_code == 1
        assert [row.status for row in error.execution.tests] == ['passed']
        assert any(row.kind == 'test_finished' and row.status == 'passed' for row in events)
        results = list((tmp_path / 'allure').rglob('*-result.json'))
        assert len(results) == 1 and json.loads(results[0].read_text())['status'] == 'passed'
        assert not list((tmp_path / 'allure').rglob('manifest.json'))
        assert not list((tmp_path / 'allure').rglob('accounting.json'))
        from api_agent.testing.inspection_controls import InspectionGroup, ReceiptInspection
        assert error.inspections[0] is owners[0]
        assert isinstance(error.inspections[1], ReceiptInspection)
        inspection = InspectionGroup(error.inspections)
        assert owners[0].pending and not owners[0].closed
    finally:
        release.set()
        assert completed.wait(timeout=5)
        for owner in owners:
            # The emergency disposal attempted while the quota snapshot was
            # held may still be returning its original cleanup receipt.
            previous = owner._close_future
            assert previous is not None
            try:
                previous.result(timeout=5)
            except CleanupPendingError as error:
                assert error.inspections == (owner,)
            owner.cleanup_timeout = 5
            owner.close()
        assert inspection.close(force=True)
        assert not inspection.active
        for client in clients:
            client.join(timeout=5)
            assert not client.is_alive()
    assert completed.is_set() and all(owner.closed for owner in owners)


def test_username_helper_is_drained_and_revoked_before_recorded_publication(tmp_path, monkeypatch):
    from api_agent.providers.shared_setup import RevocableCredentials

    entered, release, completed, settling = (threading.Event() for _ in range(4))
    owners, clients = _starting_backend(monkeypatch, entered)
    observed = []
    draining = []

    original_dispose = RevocableCredentials._dispose

    async def observe_read_drain(credentials):
        provider = owners[0].registry._owner.provider
        draining.append((credentials._closed, len(credentials._reads),
            len(provider._credential_tasks)))
        settling.set()
        await original_dispose(credentials)

    monkeypatch.setattr(RevocableCredentials, '_dispose', observe_read_drain)

    def username(source, *, timeout, interaction=None, recovery_timeout=None):
        assert source.vault == 'fixture' and source.user == 'login'
        assert 0 < timeout <= helpers.DEFAULT_TIMEOUT
        entered.set()
        assert release.wait(timeout=5)
        completed.set()
        return 'synthetic-user'

    monkeypatch.setattr(helpers, 'get_username', username)

    class Auth:
        async def __call__(self):
            return 'synthetic-auth'

        async def aclose(self):
            pass

    class Adapter:
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def asetup(self, *, credentials, **kwargs):
            assert credentials is not None
            task = asyncio.create_task(credentials(0, 'username'))
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            return Auth()

    async def load(*args, **kwargs):
        return _loaded(Adapter())

    monkeypatch.setattr(provider_setup, 'aload_provider', load)
    context, config, selected = _suite(tmp_path, config={
        'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_USER_INDEX': 0,
        'UNATTENDED_AUTH': {'password_manager': {'manager': '1password', 'cli_path': 'op'},
            'roles': {'LLM User': [{'vault': 'fixture', 'user': 'login'}]}}})

    class Storage:
        def persist(self, directory, manifest):
            backend = owners[0]
            provider = backend.registry._owner.provider
            credentials = provider._shared_setup._credentials
            observed.append((completed.is_set(), credentials._closed,
                len(credentials._reads), len(provider._credential_tasks), manifest['status']))

    outcome = []

    def run():
        try:
            outcome.append(recorded.run_recorded_integration(context, selected, config,
                accepted=True, storage=Storage()))
        except BaseException as error:
            outcome.append(error)

    runner = threading.Thread(target=run, name='synthetic-recorded-run')
    runner.start()
    try:
        assert settling.wait(timeout=10)
        assert draining == [(True, 1, 1)]
        # Disposal runs on the backend loop before its submitting thread may
        # store the publication future; assert the blocked behavior directly.
        assert entered.is_set() and not completed.is_set() and not observed and not outcome
        release.set()
        runner.join(timeout=10)
        assert not runner.is_alive() and len(outcome) == 1
        assert not isinstance(outcome[0], BaseException), repr(outcome[0])
        assert outcome[0].persisted and outcome[0].execution.tests[0].status == 'passed'
        assert observed == [(True, True, 0, 0, 'completed')]
    finally:
        release.set()
        runner.join(timeout=10)
        for owner in owners:
            if not owner.closed:
                owner.close()
        for client in clients:
            client.join(timeout=5)
            assert not client.is_alive()
