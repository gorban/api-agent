"""Credential lifecycle regressions at the provider boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def test_authorization_registers_code_and_verifier_before_token_request(monkeypatch):
    from api_agent.llm_providers.gpt import oauth
    from test_gpt_oauth import HOST, URI
    attempt = oauth.begin_authorization(HOST, URI)
    registered = []
    expected = object()

    async def request(*args, **kwargs):
        assert registered == ['synthetic-code', attempt.verifier]
        return {'token': 'synthetic'}

    async def connection(*args, **kwargs):
        return expected

    monkeypatch.setattr(oauth, '_request', request)
    monkeypatch.setattr(oauth, '_connection', connection)
    async def scenario():
        return await oauth.finish_authorization(attempt,
            dict(state=attempt.state, code='synthetic-code', client_id='issued'),
            client=object(), now=1000, register_secret=registered.append)
    assert asyncio.run(scenario()) is expected


def test_pending_refresh_for_different_token_is_rejected_without_request(monkeypatch):
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.contracts import AuthError
    from test_gpt_oauth import run
    connection = run()[0]
    request = AsyncMock()
    monkeypatch.setattr(oauth, '_request', request)
    async def scenario():
        with pytest.raises(AuthError, match='pending_refresh_mismatch'):
            await oauth.refresh_connection(connection, client=object(), now=1000,
                pending={'identity': b'wrong', 'data': {}})
    asyncio.run(scenario())
    request.assert_not_awaited()


def test_provider_registers_only_nonempty_connection_secrets():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    boundary = EmissionBoundary(ContentPolicy(True))
    provider = GPTProvider({})
    provider.bind_credential_authority(boundary)
    async def scenario():
        await provider._register_connection(SimpleNamespace(access_token='synthetic-access',
            refresh_token='', id_token=None))
    asyncio.run(scenario())
    assert 'synthetic-access' in boundary._secrets
    assert '' not in boundary._secrets


def test_response_capture_rejects_authority_change_without_publishing(monkeypatch):
    from api_agent.llm_providers.gpt.inference import _capture_safe_response
    from api_agent.workflows import inference_capture
    boundary = EmissionBoundary(ContentPolicy(True))
    published = []
    monkeypatch.setattr(inference_capture, 'capture_inference', lambda *args, **kwargs: published.append(args))
    monkeypatch.setattr(boundary, 'commit_if_current', lambda generation, publish: False)
    async def scenario():
        with pytest.raises(ProviderAccessError, match='protected authentication content'):
            await _capture_safe_response({'text': 'safe'}, 'cause', 'synthetic-token', boundary)
    asyncio.run(scenario())
    assert published == []


def test_unsafe_provider_error_without_usage_keeps_no_diagnostics():
    from api_agent.llm_providers.gpt.inference import _guard_error
    error = ProviderAccessError('authentication', 'synthetic-token', status=401)
    async def scenario():
        with pytest.raises(ProviderAccessError, match='protected authentication content') as caught:
            await _guard_error(error, 'synthetic-token')
        assert caught.value.usage is None
        assert 'synthetic-token' not in str(caught.value)
    asyncio.run(scenario())


def test_stream_view_rejects_invalid_configured_authority_before_provider_call():
    from api_agent.providers.credential_guard import CredentialProtectionError
    from api_agent.providers.loading import _StreamView
    method = AsyncMock()
    implementation = SimpleNamespace(asetup=method, credential_protection_version=1,
        bind_credential_authority=lambda authority: None)
    owner = SimpleNamespace(implementation=implementation, _credential_authority='not-an-authority',
        _generation_check=lambda: None)
    async def scenario():
        with pytest.raises(CredentialProtectionError, match='authority is unavailable'):
            await _StreamView(implementation, owner).asetup()
    asyncio.run(scenario())
    method.assert_not_awaited()


def test_pending_refresh_can_retry_after_each_failed_admission(tmp_path):
    from dataclasses import replace
    from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import Connection
    store = ConnectionStore(tmp_path / 'private')
    original = Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)
    updated = replace(original, access_token='new-access', refresh_token='new-refresh', expires_at=200)
    key = store.save(original)
    pending, admitted, calls, registered = {}, False, [], []

    async def refresh(value, now):
        calls.append(value.refresh_token)
        pending['value'] = updated
        if not admitted:
            raise TimeoutError('synthetic admission failure')
        return pending['value']

    async def scenario():
        nonlocal admitted
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50,
            register_connection=lambda value: registered.append(value.access_token),
            has_pending=lambda: bool(pending), on_saved=lambda _: pending.clear())
        try:
            with pytest.raises(TimeoutError):
                await session()
            with pytest.raises(TimeoutError):
                await session()
            assert store.load(key) == original
            admitted = True
            assert await session() == 'new-access'
            assert store.load(key) == updated
        finally:
            await session.aclose()
    asyncio.run(scenario())
    assert calls == ['refresh', 'refresh', 'refresh']
    assert registered == ['access', 'new-access']


def test_untrusted_response_does_not_attach_invalid_usage():
    from api_agent.llm_providers.gpt.inference import _guard_observed
    async def scenario():
        with pytest.raises(ProviderAccessError, match='protected authentication content') as caught:
            await _guard_observed({'text': 'synthetic-token'}, 'synthetic-token', None, usage=object())
        assert caught.value.usage is None
        assert caught.value.http_status == 200
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['invalid-material', 'registration'])
def test_login_rejects_unprotectable_selected_material_and_closes_browser(tmp_path, monkeypatch, failure):
    from api_agent.auth.login_credentials import SelectedLoginCredentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.providers.contracts import ContractError
    browser = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(factory, 'subscription_browser', lambda **kwargs: browser)
    provider = factory.GPTProvider({'GPT_STATE_DIR': tmp_path})
    provider.bind_credential_authority(EmissionBoundary(ContentPolicy(True)))
    selected = object() if failure == 'invalid-material' else SelectedLoginCredentials(
        True, username='person@example.test', password='synthetic-password')

    class Credentials:
        async def __call__(self, index, kind):
            return 'synthetic-otp'
        async def login_credentials(self, index, *, expected_username, relying_party_id):
            return selected

    async def connect(store, **options):
        assert await options['login_credentials']('otp') == 'synthetic-otp'
        await options['login_material']()
        pytest.fail('Invalid material must not reach authentication')

    provider._connect_account = connect
    if failure == 'registration':
        async def register(value):
            if value == 'synthetic-password':
                raise RuntimeError('synthetic-password')
        provider._register_secret = register

    async def scenario():
        with pytest.raises(ContractError, match='selected login material|protect selected login material') as caught:
            await provider._login(object(), interactive=False, username='person@example.test',
                                  user_index=4, credentials=Credentials())
        assert 'synthetic-password' not in str(caught.value)
        await provider.aclose()
    asyncio.run(scenario())
    browser.aclose.assert_awaited_once()


def test_two_expired_rotations_are_saved_and_retryable_until_fresh(tmp_path):
    from dataclasses import replace
    from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import AuthError, Connection
    store = ConnectionStore(tmp_path / 'private')
    original = Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)
    key = store.save(original)
    pending = {'active': True}
    issued = []

    async def refresh(value, now):
        number = len(issued) + 1
        issued.append(value.refresh_token)
        return replace(value, access_token=f'access-{number}', refresh_token=f'refresh-{number}',
                       expires_at=40 if number <= 2 else 200)

    def saved(value):
        if value.expires_at > 50:
            pending.clear()

    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50,
                                      has_pending=lambda: bool(pending), on_saved=saved)
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            assert store.load(key).refresh_token == 'refresh-2'
            assert await session() == 'access-3'
            assert store.load(key).refresh_token == 'refresh-3'
        finally:
            await session.aclose()
    asyncio.run(scenario())
    assert issued == ['refresh', 'refresh-1', 'refresh-2']


def test_saved_rotation_is_used_after_post_save_failure(tmp_path):
    from dataclasses import replace
    from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import AuthError, Connection
    store = ConnectionStore(tmp_path / 'private')
    original = Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)
    key = store.save(original)
    pending, issued = {'active': True}, []

    async def refresh(value, now):
        issued.append(value.refresh_token)
        number = len(issued)
        return replace(value, access_token=f'access-{number}', refresh_token=f'refresh-{number}', expires_at=200)

    def saved(value):
        if value.refresh_token == 'refresh-1':
            raise RuntimeError('synthetic post-save failure')
        pending.clear()

    async def scenario():
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50,
            has_pending=lambda: bool(pending), on_saved=saved,
            recover_saved=lambda previous, updated: pending.clear())
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            assert store.load(key).refresh_token == 'refresh-1'
            assert await session() == 'access-1'
            assert store.load(key).refresh_token == 'refresh-1'
        finally:
            await session.aclose()
    asyncio.run(scenario())
    assert issued == ['refresh']


def test_cancelled_pending_continuation_keeps_latest_saved_rotation(tmp_path):
    from dataclasses import replace
    from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import AuthError, Connection
    store = ConnectionStore(tmp_path / 'private')
    original = Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)
    key = store.save(original)
    pending, issued = {'active': True}, []

    async def scenario():
        waiting = asyncio.Event()
        async def refresh(value, now):
            issued.append(value.refresh_token)
            number = len(issued)
            if number == 3:
                waiting.set()
                await asyncio.Event().wait()
            return replace(value, access_token=f'access-{number}', refresh_token=f'refresh-{number}',
                           expires_at=40 if number <= 2 else 200)
        def saved(value):
            if value.expires_at > 50:
                pending.clear()
        session = SubscriptionSession(store, key, refresh, clock=lambda: 50,
                                      has_pending=lambda: bool(pending), on_saved=saved)
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            assert store.load(key).refresh_token == 'refresh-2'
            caller = asyncio.create_task(session())
            await waiting.wait()
            session._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert store.load(key).refresh_token == 'refresh-4'
            assert not pending
        finally:
            await session.aclose()
    asyncio.run(scenario())
    assert issued == ['refresh', 'refresh-1', 'refresh-2', 'refresh-2']


@pytest.mark.parametrize('failure', ['acknowledgement', 'uncertain-save'])
def test_owned_oauth_recovers_saved_rotation_without_reissuing_http(tmp_path, monkeypatch, failure):
    from dataclasses import replace
    import httpx
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import AuthError
    from api_agent.llm_providers.gpt.factory import OwnedSubscriptionSession
    from test_gpt_oauth import run
    store = ConnectionStore(tmp_path / 'private')
    original = replace(run()[0], host_id=store.host_id())
    profile = store.save(original)
    sent, saves = [], []
    payload = dict(access_token='new-access', refresh_token='new-refresh', token_type='Bearer',
                   expires_in=100, scope=oauth.SCOPES)
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=payload)
    real_save = store.save
    def save(value):
        saves.append(value)
        result = real_save(value)
        if failure == 'uncertain-save' and len(saves) == 1:
            raise OSError('synthetic uncertain save')
        return result
    monkeypatch.setattr(store, 'save', save)

    async def scenario():
        session = OwnedSubscriptionSession(store, profile)
        session.clock = lambda: 1050
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if failure == 'acknowledgement':
            def fail_before_ack(value):
                raise RuntimeError('synthetic acknowledgement failure')
            session.on_saved = fail_before_ack
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            assert store.load(profile).refresh_token == 'new-refresh'
            assert session._pending_refresh
            assert await session() == 'new-access'
            assert not session._pending_refresh
            assert store.load(profile).refresh_token == 'new-refresh'
            assert len(sent) == 1
            if failure == 'uncertain-save':
                assert len(saves) == 2 and saves[0] == saves[1]
        finally:
            await session.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['acknowledgement', 'uncertain-save'])
def test_owned_recovery_rejects_changed_stored_credentials_without_http(tmp_path, monkeypatch, failure):
    from dataclasses import replace
    import httpx
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import AuthError
    from api_agent.llm_providers.gpt.factory import OwnedSubscriptionSession
    from test_gpt_oauth import run
    store = ConnectionStore(tmp_path / 'private')
    original = replace(run()[0], host_id=store.host_id())
    profile = store.save(original)
    sent = []
    payload = dict(access_token='new-access', refresh_token='new-refresh', token_type='Bearer',
                   expires_in=100, scope=oauth.SCOPES)
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=payload)
    real_save = store.save
    calls = []
    def save(value):
        calls.append(value)
        result = real_save(value)
        if failure == 'uncertain-save' and len(calls) == 1:
            raise OSError('synthetic uncertain save')
        return result
    monkeypatch.setattr(store, 'save', save)

    async def scenario():
        session = OwnedSubscriptionSession(store, profile)
        session.clock = lambda: 1050
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if failure == 'acknowledgement':
            def failed_ack(value):
                raise RuntimeError('synthetic acknowledgement failure')
            session.on_saved = failed_ack
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            verified = store.load(profile)
            real_save(replace(verified, refresh_token='unexpected-refresh'))
            with pytest.raises(AuthError, match='refresh_admission_failed'):
                await session()
            assert store.load(profile).refresh_token == 'unexpected-refresh'
            assert len(sent) == 1 and session._pending_refresh
            real_save(verified)
            await session.aclose()
            assert store.load(profile) == verified
            assert not session._pending_refresh and len(sent) == 1
        finally:
            if not session._closed:
                await session.aclose()
    asyncio.run(scenario())


def test_owned_acknowledgement_partial_success_does_not_repeat_http(tmp_path):
    from dataclasses import replace
    import httpx
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.factory import OwnedSubscriptionSession
    from test_gpt_oauth import run
    store = ConnectionStore(tmp_path / 'private')
    original = replace(run()[0], host_id=store.host_id())
    profile = store.save(original)
    sent = []
    payload = dict(access_token='new-access', refresh_token='new-refresh', token_type='Bearer',
                   expires_in=100, scope=oauth.SCOPES)
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=payload)
    async def scenario():
        session = OwnedSubscriptionSession(store, profile)
        session.clock = lambda: 1050
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        def partial_ack(value):
            session._pending_refresh.clear()
            raise RuntimeError('synthetic-secret-after-acknowledgement')
        session.on_saved = partial_ack
        try:
            from api_agent.providers.contracts import InteractionRequiredError
            with pytest.raises(InteractionRequiredError) as caught:
                await session()
            error = caught.value
            while error is not None:
                assert 'synthetic-secret-after-acknowledgement' not in str(error)
                error = error.__context__
            assert not session._pending_refresh
            assert store.load(profile).refresh_token == 'new-refresh'
            assert await session() == 'new-access'
            assert len(sent) == 1
        finally:
            await session.aclose()
        assert len(sent) == 1
    asyncio.run(scenario())


def test_owned_cancelled_ack_recovers_before_cancellation_and_close(tmp_path):
    from dataclasses import replace
    import httpx
    from api_agent.llm_providers.gpt import oauth
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.factory import OwnedSubscriptionSession
    from test_gpt_oauth import run
    store = ConnectionStore(tmp_path / 'private')
    original = replace(run()[0], host_id=store.host_id())
    profile = store.save(original)
    sent = []
    payload = dict(access_token='new-access', refresh_token='new-refresh', token_type='Bearer',
                   expires_in=100, scope=oauth.SCOPES)
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=payload)
    async def scenario():
        session = OwnedSubscriptionSession(store, profile)
        session.clock = lambda: 1050
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        def cancelled(value):
            raise asyncio.CancelledError()
        session.on_saved = cancelled
        try:
            with pytest.raises(asyncio.CancelledError):
                await session()
            assert store.load(profile).refresh_token == 'new-refresh'
            assert not session._pending_refresh and len(sent) == 1
        finally:
            await session.aclose()
    asyncio.run(scenario())
