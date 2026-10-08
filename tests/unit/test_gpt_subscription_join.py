"""Prove callback, signature validation, persistence and refresh together."""
import asyncio
from dataclasses import replace
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api_agent.llm_providers.gpt.connect import connect_account
from api_agent.llm_providers.gpt.connections import ConnectionStore, SubscriptionSession
from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.llm_providers.gpt import oauth


@pytest.mark.parametrize('grant_permission', [True, False])
def test_real_components_join_without_external_traffic(tmp_path, grant_permission):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), kid='signer')

    async def scenario():
        store = ConnectionStore(tmp_path / 'state')
        pending = []
        query = {}
        calls = []
        clock = [1000.0]

        async def callback(uri, parameters):
            endpoint = urlsplit(uri)
            reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
            writer.write(('GET ' + endpoint.path + '?' + urlencode(parameters)
                          + f' HTTP/1.1\r\nHost: 127.0.0.1:{endpoint.port}\r\n\r\n').encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert b'200 OK' in response

        def browser(url):
            query.clear()
            query.update({name: values[0] for name, values in parse_qs(urlsplit(url).query).items()})
            pending.append(asyncio.create_task(callback(query['redirect_uri'], {
                'state': query['state'], 'code': 'synthetic-code',
                'client_id': 'issued-client', 'scope': oauth.SCOPES})))
            return True

        def transport(request):
            calls.append(request)
            assert str(request.url) in {oauth.TOKEN, oauth.METADATA, oauth.ISSUER + '/keys'}
            if str(request.url) == oauth.METADATA:
                return httpx.Response(200, json={'issuer': oauth.ISSUER,
                    'authorization_endpoint': oauth.AUTHORIZE,
                    'token_endpoint': oauth.TOKEN, 'jwks_uri': oauth.ISSUER + '/keys'})
            if str(request.url).endswith('/keys'):
                return httpx.Response(200, json={'keys': [public]})
            form = parse_qs(request.content.decode())
            assert form['client_id'] == ['issued-client']
            assert form['resource'] == [oauth.RESOURCE]
            if form['grant_type'] == ['refresh_token']:
                assert form['refresh_token'] == ['refresh-first'] and 'scope' not in form
                return httpx.Response(200, json={'access_token': 'access-rotated',
                    'refresh_token': 'refresh-rotated', 'token_type': 'Bearer',
                    'expires_in': 3600, 'scope': oauth.SCOPES})
            assert form['redirect_uri'] == [query['redirect_uri']]
            identity = jwt.encode({'iss': oauth.ISSUER, 'aud': 'issued-client',
                'sub': 'account', 'email': 'personal@example.test', 'exp': clock[0] + 3600,
                'nonce': query['nonce']}, key, algorithm='RS256', headers={'kid': 'signer'})
            return httpx.Response(200, json={'access_token': 'access-first',
                'refresh_token': 'refresh-first', 'id_token': identity, 'token_type': 'Bearer',
                'expires_in': 3600, 'scope': oauth.SCOPES if grant_permission else 'openid'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            if not grant_permission:
                with pytest.raises(AuthError, match='plan_permission_required'):
                    await connect_account(store, open_browser=browser, client=client, clock=lambda: clock[0])
                assert store.list() == ()
            else:
                profile = await connect_account(store, open_browser=browser, client=client, clock=lambda: clock[0])
                first = store.load(profile)
                assert first.host_id == store.host_id()
                assert first.subject == 'account' and first.client_id == 'issued-client'
                assert 'access-first' not in repr(first) and 'refresh-first' not in repr(first)
                # The same account reconnects with its issued client and retains its profile.
                second_profile = await connect_account(store, profile=profile, open_browser=browser,
                                                       client=client, clock=lambda: clock[0])
                assert second_profile == profile and len(store.list()) == 1
                assert query['client_id'] == 'issued-client' and 'agent_name_hint' not in query
                store.save(replace(store.load(profile), expires_at=1001))
                session = SubscriptionSession(store, profile,
                    lambda connection, now: oauth.refresh_connection(connection, client=client, now=now),
                    clock=lambda: clock[0])
                assert await session() == 'access-rotated'
                assert store.load(profile).refresh_token == 'refresh-rotated'
                count = len(calls)
                assert await session() == 'access-rotated' and len(calls) == count
                await session.aclose()
                with pytest.raises(AuthError, match='session_closed'):
                    await session()
            await asyncio.gather(*pending)

    asyncio.run(scenario())
