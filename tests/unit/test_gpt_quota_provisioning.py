"""The provider enters native quota setup only on an execution snapshot."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from api_agent.llm_providers.gpt import factory, quota_client, quota_setup
from api_agent.testing.provider_setup import IntegrationLLM
from api_agent.telemetry.usage import QuotaReadError


def test_provider_snapshot_prepares_missing_client_and_matches_attended_login(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', subject='subject', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))
    monkeypatch.setattr(quota_setup.shutil, 'which', lambda name: None)
    installed = []

    async def install():
        await asyncio.sleep(.01)
        installed.append(True)
        return '/managed/codex'

    monkeypatch.setattr(quota_setup, 'install_native_client', install)

    class Process:
        def __init__(self):
            self.frames = [dict(id=1, result={}), dict(id=2, result={'account': None}),
                           dict(id=3, result={'type': 'chatgpt', 'authUrl': 'https://auth.openai.com/authorize',
                                              'loginId': 'one'}),
                           {'method': 'account/login/completed', 'params': {'loginId': 'one', 'success': True}},
                           dict(id=5, result={'account': {'type': 'chatgpt', 'email': 'SELECTED@example.test'}})]
            self.stdin = self.stdout = self
            self.writes = []
            self.returncode = None

        def write(self, raw):
            self.writes.append(json.loads(raw))

        async def drain(self):
            pass

        async def readline(self):
            return json.dumps(self.frames.pop(0)).encode() + b'\n'

        def terminate(self):
            self.returncode = -15

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn(*args, **options):
        assert args[0] == '/managed/codex'
        return process

    monkeypatch.setattr(quota_setup.asyncio, 'create_subprocess_exec', spawn)
    shown = []

    class BrowserOwner:
        async def start(self):
            class Page:
                async def goto(self, url, *, wait_until):
                    await asyncio.sleep(.01)
                    shown.append((url, wait_until))

                def is_closed(self):
                    return False

            return SimpleNamespace(page=Page(), browser=SimpleNamespace(is_connected=lambda: True))

        async def aclose(self):
            pass

    monkeypatch.setattr(factory, 'subscription_browser', lambda **options: BrowserOwner())
    readings = []

    async def read(selected, *, period, executable=None):
        readings.append((selected, period, executable))
        return QuotaReadError('source') if executable is None else QuotaReadError('window')

    monkeypatch.setattr(quota_client, 'read_quota', read)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    subject.quota_interactive = True
    scopes = []
    native_scopes = []
    from api_agent.providers import progress
    @asynccontextmanager
    async def budget(name, seconds):
        scopes.append((name, seconds))
        async with asyncio.timeout(.005 if name == 'Provider quota snapshot' else .1):
            yield
    monkeypatch.setattr(progress, 'component_timeout', budget)
    @asynccontextmanager
    async def native_budget(name, seconds):
        native_scopes.append((name, seconds))
        async with asyncio.timeout(.1):
            yield
    monkeypatch.setattr(quota_setup, 'component_timeout', native_budget)

    class Owner:
        async def call(self, method):
            return await method(auth='app-secret')

    observed = []
    llm = object.__new__(IntegrationLLM)
    llm._quota_lock = asyncio.Lock()
    llm._quota_before = llm._quota_after = False
    llm._execution_finished = False
    llm.provider = SimpleNamespace(implementation=subject)
    llm.owner = Owner()
    llm._quota_observer = lambda phase, snapshot: observed.append((phase, snapshot))

    async def run():
        await llm._capture_quota('before')
        await subject.aclose()

    asyncio.run(run())
    assert installed == [True]
    assert shown == [('https://auth.openai.com/authorize', 'domcontentloaded')]
    assert readings == [(connection, 'week', '/managed/codex')]
    assert scopes == [('Provider accounting preparation', 330), ('Provider quota snapshot', 3)]
    assert native_scopes == [('GPT quota client preparation', 120)]
    assert observed == [('before', QuotaReadError('window'))]
    assert process.returncode == -15
    assert 'app-secret' not in json.dumps(process.writes)


def test_provider_snapshot_retains_pure_success_without_preparation(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))
    async def read(selected, *, period):
        return QuotaReadError('window')
    monkeypatch.setattr(quota_client, 'read_quota', read)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    assert asyncio.run(subject.asnapshot(auth='app-secret')) == QuotaReadError('window')


def test_provider_snapshot_caches_optional_setup_error(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))
    reads = []
    async def read(selected, *, period):
        reads.append(True)
        return QuotaReadError('source')
    monkeypatch.setattr(quota_client, 'read_quota', read)
    prepared = []
    async def prepare(*args, **kwargs):
        prepared.append(True)
        return QuotaReadError('source')
    monkeypatch.setattr(quota_setup, 'prepare_quota_client', prepare)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    async def run():
        assert await subject.aprepare_accounting(auth='app-secret') == QuotaReadError('source')
        assert await subject.asnapshot(auth='app-secret') == QuotaReadError('source')
        assert await subject.asnapshot(auth='app-secret') == QuotaReadError('source')
    asyncio.run(run())
    assert not reads
    assert prepared == [True]


def test_provider_snapshot_reuses_prepared_executable(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))
    paths = []

    async def read(selected, *, period, executable):
        paths.append(executable)
        return QuotaReadError('window')

    monkeypatch.setattr(quota_client, 'read_quota', read)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    subject.quota_executable = '/managed/codex'
    assert asyncio.run(subject.asnapshot(auth='app-secret')) == QuotaReadError('window')
    assert paths == ['/managed/codex']


def test_provider_snapshot_reports_safe_preparation_failure(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))

    async def read(selected, *, period):
        return QuotaReadError('source')

    async def prepare(*args, **kwargs):
        raise OSError('private path')

    monkeypatch.setattr(quota_client, 'read_quota', read)
    monkeypatch.setattr(quota_setup, 'prepare_quota_client', prepare)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    assert asyncio.run(subject.aprepare_accounting(auth='app-secret')) == QuotaReadError('request')
    assert asyncio.run(subject.asnapshot(auth='app-secret')) == QuotaReadError('request')


def test_provider_snapshot_preserves_preparation_cancellation(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))

    async def read(selected, *, period):
        return QuotaReadError('source')

    async def prepare(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(quota_client, 'read_quota', read)
    monkeypatch.setattr(quota_setup, 'prepare_quota_client', prepare)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    async def run():
        await subject.aprepare_accounting(auth='app-secret')
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


def test_pure_snapshot_never_prepares_missing_client(monkeypatch):
    connection = SimpleNamespace(email='selected@example.test', access_token='app-secret')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: (connection,)))

    async def read(selected, *, period):
        return QuotaReadError('source')

    async def forbidden(*args, **kwargs):
        pytest.fail('Snapshot must not install or sign in')

    monkeypatch.setattr(quota_client, 'read_quota', read)
    monkeypatch.setattr(quota_setup, 'prepare_quota_client', forbidden)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    assert asyncio.run(subject.asnapshot(auth='app-secret')) == QuotaReadError('source')


def test_cached_setup_error_does_not_freeze_changed_selected_account(monkeypatch):
    first = SimpleNamespace(email='first@example.test', access_token='first-token')
    second = SimpleNamespace(email='second@example.test', access_token='second-token')
    connections = [first]
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: tuple(connections)))

    async def prepare(connection, **kwargs):
        assert connection is first
        return QuotaReadError('identity')

    seen = []
    async def read(connection, *, period):
        seen.append(connection)
        return QuotaReadError('source')

    monkeypatch.setattr(quota_setup, 'prepare_quota_client', prepare)
    monkeypatch.setattr(quota_client, 'read_quota', read)
    subject = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    assert asyncio.run(subject.aprepare_accounting(auth='first-token')) == QuotaReadError('identity')
    connections[:] = [second]
    assert asyncio.run(subject.asnapshot(auth='second-token')) == QuotaReadError('source')
    assert seen == [second]


def test_accounting_preparation_skips_api_key_and_requires_selected_connection(monkeypatch):
    key_provider = factory.GPTProvider({'GPT_AUTH_MODE': 'api_key'})
    assert asyncio.run(key_provider.aprepare_accounting(auth='key')) is None
    monkeypatch.setattr(factory, 'ConnectionStore', lambda root: SimpleNamespace(list=lambda: ()))
    subscription = factory.GPTProvider({'GPT_AUTH_MODE': 'subscription'})
    assert asyncio.run(subscription.aprepare_accounting(auth='missing')) == QuotaReadError('identity')
    subscription.closed = True
    with pytest.raises(Exception, match='closed'):
        asyncio.run(subscription.aprepare_accounting(auth='missing'))
