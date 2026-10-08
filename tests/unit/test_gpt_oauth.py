import asyncio
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api_agent.llm_providers.gpt import oauth as o
from api_agent.llm_providers.gpt.contracts import AuthError

HOST = 'urn:uuid:12345678-1234-1234-1234-123456789abc'
URI = 'http://127.0.0.1:1455/auth/callback'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), kid='key')


def attempt():
    return o.begin_authorization(HOST, URI)


def run(a=None, claims=None, data=None, metadata=None, keys=None, callback=None, status=200, raw=None, error=False, refresh=None, now=1000, tracker=None):
    a = a or attempt()
    identity = dict(iss=o.ISSUER, aud='issued', sub='subject', exp=2000, email='a@example.com', nonce=a.nonce)
    identity.update(claims or {})
    signer = OTHER if error == 'signature' else KEY
    token = jwt.encode(identity, signer, algorithm='RS256', headers={'kid':'key'})
    payload = dict(access_token='access', refresh_token='refresh', id_token=token, token_type='Bearer', expires_in=100, scope=o.SCOPES)
    payload.update(data or {})
    if data and data.get('id_token') is None and 'id_token' in data:
        payload.pop('id_token')
    meta = dict(issuer=o.ISSUER, authorization_endpoint=o.AUTHORIZE, token_endpoint=o.TOKEN, jwks_uri=o.ISSUER+'/keys')
    meta.update(metadata or {})
    requests = [] if tracker is None else tracker
    def handler(request):
        requests.append(request)
        if error is True:
            raise httpx.ConnectError('secret', request=request)
        body = payload if str(request.url) == o.TOKEN else meta if str(request.url) == o.METADATA else keys if keys is not None else {'keys':[JWK]}
        return httpx.Response(status, content=raw) if raw is not None else httpx.Response(status, json=body)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if refresh:
                return await o.refresh_connection(refresh, client=client, now=now)
            return await o.finish_authorization(a, callback if callback is not None else dict(state=a.state, code='code', client_id='issued'), client=client, now=now)
    return asyncio.run(execute()), requests


def test_begin_and_returning():
    a = attempt()
    b = attempt()
    assert a.state != b.state and a.nonce != b.nonce and a.verifier != b.verifier
    query = parse_qs(urlsplit(a.authorization_url).query)
    assert query['agent_name_hint'] == ['api-agent']
    assert query['resource'] == [o.RESOURCE]
    assert a.verifier not in repr(a) and a.authorization_url not in repr(a)
    c, requests = run(a)
    assert c.expires_at == 1100 and 'access_token=' not in repr(c)
    assert parse_qs(requests[0].content.decode())['redirect_uri'] == [URI]
    returning = o.begin_authorization(HOST, URI, c)
    assert 'agent_name_hint' not in parse_qs(urlsplit(returning.authorization_url).query)
    assert run(returning, callback=dict(state=returning.state, code='code'))[0].subject == c.subject
    assert run(refresh=c)[0].refresh_token == 'refresh'
    form = parse_qs(run(refresh=c)[1][0].content.decode())
    assert 'scope' not in form and form['client_id'] == ['issued']
    assert run(data={'earliest_refresh_at':1050})[0].earliest_refresh_at == 1050


@pytest.mark.parametrize('host,uri', [('bad',URI), ('urn:uuid:bad',URI), (HOST,'http://localhost:1455/auth/callback'), (HOST,'http://127.0.0.1/auth/callback'), (HOST,'http://user@127.0.0.1:2/auth/callback'), (HOST,URI+'?x=1'), (HOST,URI+'#x'), (HOST,'http://127.0.0.1:bad/auth/callback')])
def test_invalid_begin(host, uri):
    with pytest.raises(AuthError, match='invalid_attempt'):
        o.begin_authorization(host, uri)


def test_registration_and_refresh_checks():
    c = run()[0]
    for changed in [replace(c, host_id='bad'), replace(c, client_id=o.INITIAL_CLIENT)]:
        with pytest.raises(AuthError):
            o.begin_authorization(HOST, URI, changed)
    a = o.begin_authorization(HOST, URI, c)
    with pytest.raises(AuthError, match='client_mismatch'):
        run(a, callback=dict(state=a.state, code='code', client_id='other'))
    with pytest.raises(AuthError, match='identity_mismatch'):
        run(a, claims={'sub':'other'})
    with pytest.raises(AuthError, match='identity_mismatch'):
        run(refresh=c, claims={'sub':'other'})
    with pytest.raises(AuthError, match='refresh_not_ready'):
        run(refresh=replace(c, earliest_refresh_at=1001))


@pytest.mark.parametrize('callback,code', [({},'invalid_state'), ({'error':'secret'},'authorization_denied'), ({'client_id':''},'incomplete_registration'), ({'client_id':o.INITIAL_CLIENT},'incomplete_registration'), ({'code':''},'invalid_credentials'), ({'code':1},'invalid_credentials')])
def test_callback(callback, code):
    a = attempt()
    params = dict(state=a.state, code='code', client_id='issued')
    params.update(callback)
    if not callback:
        params.pop('state')
    with pytest.raises(AuthError, match=code):
        run(a, callback=params)
    with pytest.raises(AuthError, match='incomplete_registration'):
        run(a, callback=dict(state=a.state, code='code'))


@pytest.mark.parametrize('claims', [{'exp':1000}, {'exp':True}, {'exp':'bad'}, {'exp':float('inf')}, {'nonce':'bad'}, {'iss':'other'}, {'aud':'other'}, {'sub':''}, {'sub':1}, {'nbf':1001}, {'email':''}])
def test_identity(claims):
    with pytest.raises(AuthError):
        run(claims=claims)


def test_valid_nbf_and_bad_signature():
    assert run(claims={'nbf':999})[0].subject == 'subject'
    with pytest.raises(AuthError, match='invalid_identity'):
        run(error='signature')


@pytest.mark.parametrize('data', [{'scope':'openid'}, {'scope':''}, {'token_type':'other'}, {'expires_in':0}, {'expires_in':False}, {'earliest_refresh_at':-1}, {'access_token':''}, {'refresh_token':''}, {'id_token':'bad'}, {'id_token':jwt.encode({'exp':2000},'s'*32,algorithm='HS256')}])
def test_credentials(data):
    with pytest.raises(AuthError):
        run(data=data)


@pytest.mark.parametrize('metadata', [{'issuer':'bad'}, {'authorization_endpoint':'bad'}, {'token_endpoint':'bad'}, {'jwks_uri':'http://auth.openai.com/keys'}, {'jwks_uri':'https://evil.test/keys'}, {'jwks_uri':'https://auth.openai.com/keys?q=1'}, {'jwks_uri':'https://auth.openai.com/keys#f'}, {'jwks_uri':''}])
def test_metadata(metadata):
    with pytest.raises(AuthError):
        run(metadata=metadata)


@pytest.mark.parametrize('keys', [{}, {'keys':[]}, {'keys':[JWK,JWK]}, {'keys':[{'kid':'other'}]}, {'keys':[{'kid':'key','kty':'bad'}]}, {'keys':None}])
def test_keys(keys):
    with pytest.raises(AuthError, match='invalid_identity'):
        run(keys=keys)


@pytest.mark.parametrize('kwargs', [{'status':302}, {'status':400}, {'raw':b'bad'}, {'raw':b'[]'}, {'raw':b'x'*(o.LIMIT+1)}, {'error':True}])
def test_transport(kwargs):
    with pytest.raises(AuthError, match='authorization_failed') as exc:
        run(**kwargs)
    assert 'secret' not in str(exc.value)


def test_refresh_retains_verified_identity_without_new_id():
    c = run()[0]
    updated, requests = run(refresh=c, data={'id_token':None, 'access_token':'next', 'refresh_token':'rotated'})
    assert updated.id_token == c.id_token and updated.subject == c.subject
    assert updated.access_token == 'next' and updated.refresh_token == 'rotated'
    assert len(requests) == 1
    with pytest.raises(AuthError):
        run(data={'id_token':None})


@pytest.mark.parametrize('now', [float('nan'), float('-inf'), True, 'secret', 10**400])
def test_time_rejected_before_request(now):
    for refresh in [None, run()[0]]:
        tracker = []
        with pytest.raises(AuthError) as caught:
            run(now=now, refresh=refresh, tracker=tracker)
        assert caught.value.__context__ is None
        assert tracker == []


@pytest.mark.parametrize('data', [{'expires_in':10**400}, {'earliest_refresh_at':10**400}, {'expires_in':1e308}])
def test_invalid_expiry_numbers(data):
    with pytest.raises(AuthError):
        run(now=1e308, claims={'exp':1.1e308}, data=data)


@pytest.mark.parametrize('claims', [{'aud':['issued','other']}, {'aud':['issued','other'],'azp':'other'}, {'azp':'other'}, {'exp':10**400}, {'nbf':float('nan')}])
def test_signed_claim_binding(claims):
    with pytest.raises(AuthError) as caught:
        run(claims=claims)
    assert caught.value.__context__ is None


def test_authorized_party_and_key_permissions():
    assert run(claims={'aud':['issued','other'],'azp':'issued'})[0].subject == 'subject'
    assert run(claims={'azp':'issued'}, keys={'keys':[dict(JWK, alg='RS256', use='sig', key_ops=['verify'])]})[0].subject == 'subject'


@pytest.mark.parametrize('extra', [{'use':'enc'}, {'alg':'HS256'}, {'key_ops':['encrypt']}, {'key_ops':'verify'}, {'kty':'EC'}, {'kid':None}, {'kid':''}, {'n':12}, {'e':12}, {'e':'AA'}])
def test_key_constraints(extra):
    with pytest.raises(AuthError) as caught:
        run(keys={'keys':[dict(JWK, **extra)]})
    assert caught.value.__context__ is None


@pytest.mark.parametrize('client_id', [[], ['issued'], {}, 123, None])
def test_client_id_type(client_id):
    a = attempt()
    with pytest.raises(AuthError, match='incomplete_registration'):
        run(a, callback=dict(state=a.state, code='code', client_id=client_id))


@pytest.mark.parametrize('host,uri', [(None,URI), (123,URI), (HOST,None), (HOST,123), (HOST,'http://127.0.0.1：secret/auth/callback')])
def test_begin_types(host, uri):
    with pytest.raises(AuthError, match='invalid_attempt') as caught:
        o.begin_authorization(host, uri)
    assert caught.value.__context__ is None


@pytest.mark.parametrize('uri', ['https://auth.openai.com：secret/keys','https://[secret/keys'])
def test_discovery_parse_errors(uri):
    with pytest.raises(AuthError, match='invalid_issuer') as caught:
        run(metadata={'jwks_uri':uri})
    assert caught.value.__context__ is None


def test_transport_context_is_discarded():
    with pytest.raises(AuthError) as caught:
        run(error=True)
    assert caught.value.__context__ is None


def test_header_requires_nonempty_key_id():
    for kid in ['', None]:
        headers = {'kid':kid} if kid is not None else {}
        token = jwt.encode(dict(iss=o.ISSUER, aud='issued', sub='subject', exp=2000), KEY, algorithm='RS256', headers=headers)
        with pytest.raises(AuthError, match='invalid_identity'):
            run(data={'id_token':token})
