"""A rejected code may restart the same registration once, without new owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from api_agent.llm_providers.gpt import connect, oauth
from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.providers.progress import progress_sink
from test_gpt_connect import Store, request
from test_gpt_oauth import HOST, URI, run
from test_gpt_software_connect import passkey_owner


def rejected_code(*, code='authorization_failed', stage='token', status=400, reason='invalid_grant'):
    error = AuthError(code)
    error.stage, error.http_status, error.oauth_error = stage, status, reason
    return error


@pytest.fixture
def authorization_lane(passkey_owner, monkeypatch):
    async def use(check, *, finish, navigation_error=None):
        owner, inspection, channel, browser, material = await passkey_owner()
        session = await owner.start()
        navigations = []
        async def navigate(url, **options):
            query = {key: value[0] for key, value in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}
            navigations.append(query)
            if len(navigations) == 2:
                stale = navigations[0]
                assert b'400 Bad Request' in await request(stale['redirect_uri'],
                    target='/auth/callback?' + urlencode({'state': stale['state'], 'code': 'stale'}))
                if navigation_error is not None:
                    raise navigation_error
            assert b'200 OK' in await request(query['redirect_uri'],
                target='/auth/callback?' + urlencode({'state': query['state'], 'code': 'code-' + str(len(navigations)),
                                                     'client_id': 'issued'}))
        session.page.goto = navigate
        monkeypatch.setattr(oauth, 'finish_authorization', finish)
        store = Store()
        store.host_id = lambda: HOST
        rows = []
        try:
            with progress_sink(rows.append):
                await check(owner, material, store, navigations, rows, channel)
        finally:
            await inspection.aclose()
            browser.close.assert_awaited_once()
    return use


async def authorize(owner, material, store):
    return await connect.connect_account(store, browser_owner=owner, client=object(),
        app_name='chosen-app', login_username='person@example.test',
        login_credentials=AsyncMock(), login_material=AsyncMock(return_value=material))


def test_retry_rotates_proof_and_callback_but_reuses_registration_page_key_and_deadline(authorization_lane):
    finish = AsyncMock(side_effect=[rejected_code(), SimpleNamespace(email='person@example.test')])
    async def check(owner, material, store, navigations, rows, channel):
        assert await authorize(owner, material, store) == 'account'
        initial, repeated = navigations
        assert initial['client_id'] == oauth.INITIAL_CLIENT
        assert initial['agent_name_hint'] == 'chosen-app'
        assert repeated['client_id'] == 'issued' and 'agent_name_hint' not in repeated
        assert repeated['ext_agent_host_id'] == initial['ext_agent_host_id'] == HOST
        assert repeated['redirect_uri'] == initial['redirect_uri']
        assert repeated['resource'] == initial['resource'] == oauth.RESOURCE
        assert repeated['scope'] == initial['scope'] == oauth.SCOPES
        assert all(initial[key] != repeated[key] for key in ('state', 'nonce', 'code_challenge'))
        assert len(store.saved) == 1 and finish.await_count == 2
        assert finish.await_args.args[0].client_id == 'issued'
        assert [call.args[1]['code'] for call in finish.await_args_list] == ['code-1', 'code-2']
        assert len([call for call in channel.send.call_args_list if call.args[0] == 'WebAuthn.addCredential']) == 1
        assert len([call for call in channel.send.call_args_list if call.args[0] == 'WebAuthn.removeVirtualAuthenticator']) == 1
        login = [row for row in rows if row.name == 'GPT login']
        assert len(login) == 2 and login[0].id == login[1].id
        assert login[0].active and not login[1].active and login[0].timeout == login[1].timeout == 180
    asyncio.run(authorization_lane(check, finish=finish))


@pytest.mark.parametrize('failure', [
    rejected_code(code='invalid_identity'), rejected_code(stage='metadata'),
    rejected_code(status=401), rejected_code(reason='invalid_client'),
])
def test_other_failures_never_repeat_browser_authorization(authorization_lane, failure):
    finish = AsyncMock(side_effect=failure)
    async def check(owner, material, store, navigations, rows, channel):
        with pytest.raises(AuthError) as caught:
            await authorize(owner, material, store)
        assert caught.value is failure
        assert len(navigations) == finish.await_count == 1 and not store.saved
    asyncio.run(authorization_lane(check, finish=finish))


def test_second_invalid_grant_stops_without_saving_or_looping(authorization_lane):
    finish = AsyncMock(side_effect=rejected_code())
    async def check(owner, material, store, navigations, rows, channel):
        with pytest.raises(AuthError, match='authorization_failed'):
            await authorize(owner, material, store)
        assert len(navigations) == finish.await_count == 2 and not store.saved
    asyncio.run(authorization_lane(check, finish=finish))


def test_retry_navigation_cancellation_preserves_cleanup(authorization_lane):
    finish = AsyncMock(side_effect=rejected_code())
    async def check(owner, material, store, navigations, rows, channel):
        with pytest.raises(asyncio.CancelledError):
            await authorize(owner, material, store)
        assert len(navigations) == 2 and finish.await_count == 1 and not store.saved
        assert channel.send.call_args_list[-3].args[0] == 'WebAuthn.removeVirtualAuthenticator'
    asyncio.run(authorization_lane(check, finish=finish, navigation_error=asyncio.CancelledError()))


def test_returning_retry_retains_registered_identity_and_hint():
    saved = run()[0]
    initial = oauth.begin_authorization(HOST, URI, saved)
    repeated = oauth.restart_authorization(initial, {'state': initial.state, 'client_id': saved.client_id})
    query = parse_qs(urlsplit(repeated.authorization_url).query)
    assert repeated.registration is saved and repeated.client_id == saved.client_id
    assert query['id_token_hint'] == [saved.id_token] and query['login_hint'] == [saved.email]
    assert repeated.nonce != initial.nonce and repeated.verifier != initial.verifier
    assert 'agent_name_hint' not in query


@pytest.mark.parametrize('changes,expected', [
    ({'state': 'wrong'}, 'invalid_state'), ({'error': 'access_denied'}, 'authorization_denied'),
    ({'client_id': ''}, 'incomplete_registration'), ({'client_id': None}, 'incomplete_registration'),
    ({'client_id': oauth.INITIAL_CLIENT}, 'incomplete_registration'),
])
def test_retry_rejects_unbound_or_incomplete_callback(changes, expected):
    initial = oauth.begin_authorization(HOST, URI)
    callback = {'state': initial.state, 'client_id': 'issued', **changes}
    with pytest.raises(AuthError, match=expected):
        oauth.restart_authorization(initial, callback)


def test_provisional_registration_rejects_a_different_issued_client():
    initial = oauth.begin_authorization(HOST, URI)
    repeated = oauth.restart_authorization(initial, {'state': initial.state, 'client_id': 'issued'})
    with pytest.raises(AuthError, match='client_mismatch'):
        run(a=repeated, claims={'aud': 'other'}, callback={'state': repeated.state, 'code': 'new', 'client_id': 'other'})



def test_initial_material_wait_closure_retains_browser_closed_diagnostic(passkey_owner):
    async def scenario():
        owner, inspection, channel, browser, material = await passkey_owner()
        store = Store()
        store.host_id = lambda: HOST
        try:
            with pytest.raises(AuthError, match='^browser_closed$'):
                await connect.connect_account(store, browser_owner=owner, client=object(),
                    login_username='person@example.test', login_credentials=AsyncMock(),
                    login_material=AsyncMock(side_effect=connect.SessionClosedError('private detail')))
            channel.send.assert_not_awaited()
        finally:
            await inspection.aclose()
            browser.close.assert_awaited_once()
    asyncio.run(scenario())



def test_restarted_authorization_rejects_the_previous_signed_nonce():
    initial = oauth.begin_authorization(HOST, URI)
    repeated = oauth.restart_authorization(initial, {'state': initial.state, 'client_id': 'issued'})
    with pytest.raises(AuthError, match='invalid_identity'):
        run(a=repeated, claims={'nonce': initial.nonce})
