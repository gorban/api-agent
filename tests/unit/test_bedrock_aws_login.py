import asyncio
from dataclasses import replace
import os
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures('isolated_native_login_gate')

from api_agent.llm_providers.bedrock import aws_login as module
from api_agent.llm_providers.bedrock.acquisition import SessionRequest, SessionMaterial
from api_agent.providers.contracts import ContractError, InteractionRequiredError, SessionClosedError


@pytest.fixture
def config():
    return {'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID': '123456789012',
            'BEDROCK_LOGIN_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/example'}


@pytest.fixture
def session_request():
    return SessionRequest('chosen', 'us-east-1', None, True, None, object())


class Bridge:
    def __init__(self, session_request):
        self.opened = False
        self.closed = False
    async def open(self):
        self.opened = True
    async def aclose(self):
        self.closed = True


@pytest.fixture
def bridge(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'api_agent.llm_providers.bedrock.login_interaction', SimpleNamespace(LoginInteraction=Bridge))


@pytest.mark.asyncio
async def test_acquire_refresh_isolated_material(config, session_request, bridge, monkeypatch):
    adapter = module.factory(config, {})
    calls = []
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'ambient-secret')
    monkeypatch.setenv('HOME', '/unrelated/home')
    async def acquire(settings, environment, interaction):
        calls.append((settings, environment, interaction))
        assert interaction is None or interaction.opened
        return dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', expires_at=time.time()+300, identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(adapter._transport, 'acquire', acquire)
    first = await adapter.acquire(session_request)
    second = await adapter.acquire(session_request, previous=first)
    root = adapter._root
    assert type(first) is SessionMaterial and type(second) is SessionMaterial
    assert repr(adapter) == '<AwsLoginAcquirer>'
    assert calls[0][0]['login'] is True and calls[1][0]['login'] is False
    assert calls[0][2].closed and calls[1][2] is None
    assert 'AWS_ACCESS_KEY_ID' not in calls[0][1]
    assert calls[0][1]['HOME'] == str(root)
    assert (root / '.aws' / 'credentials').read_text() == ''
    assert (root / '.aws' / 'config').stat().st_mode & 0o777 == 0o600
    await adapter.aclose()
    await adapter.aclose()
    assert not root.exists()
    with pytest.raises(SessionClosedError):
        await adapter.acquire(session_request)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [dict(user_index=0), dict(interactive=False), dict(interaction=None)])
async def test_reject_before_helper_or_files(config, session_request, changes):
    adapter = module.AwsLoginAcquirer(config)
    with pytest.raises(InteractionRequiredError):
        await adapter.acquire(replace(session_request, **changes))
    assert adapter._root is None
    await adapter.aclose()


@pytest.mark.parametrize('changes', [
    {'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID':'123'},
    {'BEDROCK_LOGIN_EXPECTED_PRINCIPAL':'arn:aws:iam::999999999999:user/example'},
{'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID':None},
    {'BEDROCK_LOGIN_EXPECTED_PRINCIPAL':'bad\nvalue'},
])
def test_invalid_binding(config, changes):
    with pytest.raises(ContractError):
        module.AwsLoginAcquirer(dict(config, **changes))


@pytest.mark.parametrize('url', ['http://example.com', 'https://user@example.com', 'https://user:pass@example.com', 'https://example.com/#fragment', 'https://'])
def test_invalid_sso_url(config, url):
    adapter = module.AwsLoginAcquirer(dict(config, BEDROCK_LOGIN_SSO_START_URL=url, BEDROCK_LOGIN_SSO_REGION='us-east-1', BEDROCK_LOGIN_SSO_ACCOUNT_ID='123456789012', BEDROCK_LOGIN_SSO_ROLE_NAME='Example'), method='sso')
    with pytest.raises(ContractError):
        adapter._sso_route({})


@pytest.mark.parametrize('region,role', [('bad region','role'), ('us-east-1','bad role')])
def test_invalid_sso_fields(config, region, role):
    adapter = module.AwsLoginAcquirer(dict(config, BEDROCK_LOGIN_SSO_START_URL='https://example.com/start', BEDROCK_LOGIN_SSO_REGION=region, BEDROCK_LOGIN_SSO_ACCOUNT_ID='123456789012', BEDROCK_LOGIN_SSO_ROLE_NAME=role), method='sso')
    with pytest.raises(ContractError):
        adapter._sso_route({})


@pytest.mark.asyncio
async def test_sso_profile_default_and_binding_immutable(config, session_request):
    adapter = module.AwsLoginAcquirer(dict(config, BEDROCK_LOGIN_SSO_START_URL='https://example.com/start', BEDROCK_LOGIN_SSO_REGION='us-east-1', BEDROCK_LOGIN_SSO_ROLE_NAME='Example', BEDROCK_LOGIN_SSO_ACCOUNT_ID='123456789012'), method='sso')
    adapter._sso_route({})
    settings, _ = adapter._prepare(replace(session_request, profile='default', region='us-east-1'))
    text = (adapter._root / '.aws' / 'config').read_text()
    assert text.startswith('[default]\n')
    assert 'sso_account_id = 123456789012' in text
    assert settings['profile'] == 'default'
    with pytest.raises(ContractError, match='changed'):
        adapter._prepare(session_request)
    await adapter.aclose()


@pytest.mark.parametrize('changes', [dict(profile='bad\nprofile'), dict(region='bad region')])
def test_bad_request_configuration(config, session_request, changes):
    adapter = module.AwsLoginAcquirer(config)
    with pytest.raises(ContractError):
        adapter._prepare(replace(session_request, **changes))


@pytest.mark.asyncio
async def test_cleanup_failure_retains_private_state(config, session_request, bridge, monkeypatch):
    adapter = module.AwsLoginAcquirer(config)
    adapter._prepare(session_request)
    adapter._interaction = Bridge(session_request)
    root = adapter._root
    original = module.shutil.rmtree
    def fail(path):
        raise OSError('private-detail')
    monkeypatch.setattr(module.shutil, 'rmtree', fail)
    with pytest.raises(Exception):
        await adapter.aclose()
    assert adapter._root == root
    monkeypatch.setattr(module.shutil, 'rmtree', original)
    await adapter.aclose()
    assert not root.exists()


@pytest.mark.asyncio
async def test_reject_late_material(config, session_request, bridge, monkeypatch):
    adapter = module.AwsLoginAcquirer(config)
    async def acquire(*args):
        adapter._closed = True
        return dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', expires_at=time.time()+300, identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(adapter._transport, 'acquire', acquire)
    with pytest.raises(SessionClosedError):
        await adapter.acquire(session_request)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_existing_bridge_retry_and_invalid_tuple(config, session_request, bridge, monkeypatch):
    adapter = module.AwsLoginAcquirer(config)
    adapter._interaction = Bridge(session_request)
    async def acquire(*args):
        return {}
    monkeypatch.setattr(adapter._transport, 'acquire', acquire)
    with pytest.raises(ContractError):
        await adapter.acquire(session_request)
    assert adapter._interaction is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_no_stdin_allows_explicit_owned_sign_in(config, session_request, bridge, monkeypatch):
    adapter = module.AwsLoginAcquirer(dict(config, NO_STD_IN=True))
    async def acquire(settings, environment, interaction):
        assert interaction.opened
        return dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token',
            expires_at=time.time()+300, identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(adapter._transport, 'acquire', acquire)
    try:
        material = await adapter.acquire(session_request)
        assert material.kind == 'temporary' and adapter._logged_in
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('profile',''),('region',''),('profile',None),('region',None)])
async def test_native_missing_binding_rejects_before_files(config, session_request, field, value):
    adapter = module.AwsLoginAcquirer(config)
    with pytest.raises(ContractError):
        adapter._prepare(replace(session_request, **{field:value}))
    assert adapter._root is None
    await adapter.aclose()


@pytest.fixture(autouse=True)
def unavailable_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    async def acquire(self, request, **kwargs):
        return None, {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)


@pytest.mark.parametrize('method', ['bad', None, 1])
def test_invalid_explicit_method(config, method):
    with pytest.raises(ContractError):
        module.AwsLoginAcquirer(config, method=method)


@pytest.mark.asyncio
async def test_factory_selects_method_independently_of_configuration(session_request):
    from api_agent.llm_providers.bedrock import aws_sso
    direct = module.factory({}, {})
    sso = aws_sso.factory({}, {})
    direct_settings, _ = direct._prepare(session_request)
    sso_settings, _ = sso._prepare(session_request)
    assert direct_settings['method'] == 'direct'
    assert sso_settings['method'] == 'sso'
    await direct.aclose()
    await sso.aclose()
