"""SDK readiness and native recovery are exercised without AWS or network."""
import asyncio
import datetime
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.bedrock import ambient, aws_login, login_worker, session_providers
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial, SessionRequest
from api_agent.llm_providers.bedrock.sessions import AcquiredSession
from api_agent.providers.contracts import ConfigurationRequiredError, ContractError, InteractionRequiredError, SessionClosedError

REQUEST = SessionRequest('default', 'us-east-1', None, False)
IDENTITY = dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')
MATERIAL = dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken=None, expires_at=None, identity=IDENTITY, region='us-east-1')


@pytest.fixture(autouse=True)
def forbid_subprocess(monkeypatch):
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=AssertionError('No real AWS/SDK subprocess')))


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    # Synthetic SDK probes must never inspect the user's native snapshot.
    monkeypatch.setattr(login_worker, "capture_paths", lambda: dict(
        home=str(tmp_path), config=str(tmp_path / "config"),
        credentials=str(tmp_path / "credentials"), login=str(tmp_path / "login")))
    frozen = SimpleNamespace(access_key='key', secret_key='secret', token=None)
    credentials = SimpleNamespace(get_frozen_credentials=lambda:frozen)
    session = SimpleNamespace(profile_name='default', region_name='us-east-1',
        _session=SimpleNamespace(full_config={'profiles': {'default': {
            'sso_session': 'chosen', 'sso_account_id': '123456789012', 'sso_role_name': 'Example'}},
            'sso_sessions': {'chosen': {'sso_start_url':'https://example.com/start', 'sso_region':'us-east-1'}}}),
        get_credentials=lambda:credentials)
    calls = []
    def client(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(get_caller_identity=lambda:dict(IDENTITY), close=lambda:None)
    session.client=client
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(Session=lambda **kwargs:session, client=client))
    monkeypatch.setitem(sys.modules, 'botocore.config', SimpleNamespace(Config=lambda **kwargs:kwargs))
    return session, credentials, frozen, calls


def test_probe_freezes_and_verifies_exact_static_tuple(sdk):
    _, _, _, calls = sdk
    result = login_worker.ambient(dict(profile='default', region='us-east-1'))
    assert result['AccessKeyId'] == 'key' and result['SessionToken'] is None
    assert result['identity'] == IDENTITY and result['expires_at'] is None
    assert calls[0]['aws_access_key_id'] == 'key' and calls[0]['aws_secret_access_key'] == 'secret'
    assert calls[0]['aws_session_token'] is None
    assert calls[0]['config']['ignore_configured_endpoint_urls'] is True
    assert calls[0]['config']['proxies'] == {}


def test_probe_preserves_real_expiry_and_region_discovery(sdk):
    _, credentials, frozen, _ = sdk
    frozen.token = 'token'
    credentials._expiry_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    result = login_worker.ambient(dict(profile=None, region=None))
    assert result['expires_at'] == credentials._expiry_time.timestamp()
    assert result['region'] == 'us-east-1'


@pytest.mark.parametrize('kind', ['NoCredentialsError','PartialCredentialsError','UnauthorizedSSOTokenError','LoginRefreshRequired',
    'ExpiredToken','ExpiredTokenException','InvalidClientTokenId','UnrecognizedClientException','InvalidToken','none'])
def test_only_credential_unavailability_permits_recovery(sdk, kind):
    session, _, _, _ = sdk
    if kind == 'none':
        session.get_credentials = lambda:None
    else:
        error = type(kind if kind.endswith('Error') or kind == 'LoginRefreshRequired' else 'ClientError', (Exception,), {})()
        error.response = {'Error': {'Code': kind}}
        def fail():
            raise error
        session.get_credentials = fail
    result = login_worker.ambient(dict(profile='default', region='us-east-1'))
    assert result['unavailable'] is True
    assert result['sso'] == dict(sso_start_url='https://example.com/start', sso_region='us-east-1', sso_account_id='123456789012', sso_role_name='Example')


@pytest.mark.parametrize('name', ['EndpointConnectionError','AccessDenied','ProfileNotFound','InvalidConfigError','ImportError','ValueError','CredentialRetrievalError','LoginInsufficientPermissions'])
def test_arbitrary_or_configuration_or_network_errors_cannot_trigger_login(sdk, name):
    session, _, _, _ = sdk
    error = type(name, (Exception,), {})()
    error.response = {'Error': {'Code': name}}
    def fail():
        raise error
    session.get_credentials = fail
    with pytest.raises(type(error)):
        login_worker.ambient(dict(profile='default', region='us-east-1'))


@pytest.mark.parametrize('key,value', [('access_key',None),('secret_key',' ')])
def test_invalid_frozen_tuple_stops_recovery(sdk,key,value):
    setattr(sdk[2], key, value)
    with pytest.raises(ValueError):
        login_worker.ambient(dict(profile='default', region='us-east-1'))


@pytest.mark.parametrize('assertions', [dict(account='999999999999'), dict(principal='arn:aws:iam::123456789012:user/other')])
def test_optional_assertions_independently_reject_wrong_identity(sdk, assertions):
    with pytest.raises(login_worker.AcquisitionFailure, match='identity_verification'):
        login_worker.ambient(dict(profile='default', region='us-east-1', **assertions))


@pytest.mark.parametrize('identity', [{},dict(Account='invalid',Arn='arn:x'),dict(Account='123456789012',Arn=None),dict(Account='123456789012',Arn='bad')])
def test_sts_malformed_identity_is_not_readiness(sdk, monkeypatch, identity):
    import boto3
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs:SimpleNamespace(get_caller_identity=lambda:identity, close=lambda:None))
    with pytest.raises(ValueError):
        login_worker.verify_identity(MATERIAL, dict(region='us-east-1'))


def test_worker_dispatches_probe_without_cli(monkeypatch):
    frames=[]
    monkeypatch.setattr(login_worker, 'ambient', lambda config:{'unavailable':True})
    monkeypatch.setattr(login_worker, 'emit', frames.append)
    monkeypatch.setattr(login_worker.shutil, 'which', lambda name:pytest.fail('No CLI during probe'))
    login_worker.run(dict(method='ambient'))
    assert frames == [{'result': {'unavailable':True}}]


@pytest.mark.asyncio
@pytest.mark.parametrize('token,expiry', [(None,None),('token',None),('token','future')],
                         ids=['permanent', 'refreshable', 'expiring'])
async def test_ambient_owner_honest_expiry_refresh_and_identity_pin(monkeypatch, token, expiry):
    expiry = time.time() + 300 if expiry == 'future' else expiry
    owner=ambient.AmbientSession()
    acquire=AsyncMock(return_value=dict(MATERIAL, SessionToken=token, expires_at=expiry))
    monkeypatch.setattr(owner.transport, 'acquire', acquire)
    first,_=await owner.acquire(REQUEST)
    assert first.expires_at == expiry
    assert (first.refresh_after is not None) == (token is not None and expiry is None)
    await owner.acquire(REQUEST)
    assert acquire.await_args.args[0]['account'] == IDENTITY['Account']
    assert acquire.await_args.args[0]['principal'] == IDENTITY['Arn']
    assert first.worker_config(REQUEST,time.time())['access_key'] == 'key'
    await owner.aclose()


@pytest.mark.asyncio
async def test_ambient_pins_assumed_role_prefix(monkeypatch):
    owner=ambient.AmbientSession()
    result=dict(MATERIAL, identity=dict(Account='123456789012',Arn='arn:aws:sts::123456789012:assumed-role/Example/session'))
    monkeypatch.setattr(owner.transport,'acquire',AsyncMock(return_value=result))
    await owner.acquire(REQUEST)
    assert owner._identity[1] == 'arn:aws:sts::123456789012:assumed-role/Example'
    await owner.aclose()


@pytest.mark.asyncio
async def test_ambient_unavailable_route_preserved(monkeypatch):
    owner=ambient.AmbientSession()
    monkeypatch.setattr(owner.transport,'acquire',AsyncMock(return_value={'unavailable':True,'sso':{'sso_region':'us-east-1'}}))
    assert await owner.acquire(REQUEST) == (None,{'sso_region':'us-east-1'})
    await owner.aclose()


@pytest.mark.asyncio
async def test_native_reuses_valid_ambient_without_any_assertion_or_interaction(monkeypatch):
    owner=aws_login.AwsLoginAcquirer({})
    material=SessionMaterial('frozen',access_key='key',secret_key='secret')
    monkeypatch.setattr(owner._ambient,'acquire',AsyncMock(return_value=(material,{})))
    login=AsyncMock(side_effect=AssertionError('Do not launch browser login'))
    monkeypatch.setattr(owner._transport,'acquire',login)
    assert await owner.acquire(REQUEST) is material
    assert owner._root is None
    login.assert_not_awaited()
    owner._ambient.acquire.return_value=(None,{})
    with pytest.raises(InteractionRequiredError,match='restart'):
        await owner.acquire(REQUEST,previous=material)
    login.assert_not_awaited()
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[ContractError('safe'),RuntimeError('private'),ConfigurationRequiredError(('BEDROCK_LOGIN_SSO_ACCOUNT_ID',))])
async def test_probe_failure_never_tries_native_fallback(monkeypatch,failure):
    owner=aws_login.AwsLoginAcquirer({})
    monkeypatch.setattr(owner._ambient,'acquire',AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await owner.acquire(REQUEST)
    assert owner._root is None
    await owner.aclose()


@pytest.mark.asyncio
async def test_native_late_ambient_does_not_publish(monkeypatch):
    owner=aws_login.AwsLoginAcquirer({})
    async def late(*args,**kwargs):
        owner._closed=True
        return SessionMaterial('frozen',access_key='key',secret_key='secret'),{}
    monkeypatch.setattr(owner._ambient,'acquire',late)
    with pytest.raises(SessionClosedError):
        await owner.acquire(REQUEST)
    await owner.aclose()


@pytest.mark.asyncio
async def test_sso_missing_routes_are_runtime_configuration_facts(monkeypatch):
    owner=aws_login.AwsLoginAcquirer({}, method='sso')
    monkeypatch.setattr(owner._ambient,'acquire',AsyncMock(return_value=(None,{'sso_region':'us-east-1'})))
    with pytest.raises(ConfigurationRequiredError) as raised:
        await owner.acquire(REQUEST)
    assert raised.value.fields == ('BEDROCK_LOGIN_SSO_START_URL','BEDROCK_LOGIN_SSO_ACCOUNT_ID','BEDROCK_LOGIN_SSO_ROLE_NAME')
    assert owner._root is None
    await owner.aclose()


@pytest.mark.asyncio
async def test_sso_selected_route_is_read_without_account_assertion(monkeypatch):
    owner=aws_login.AwsLoginAcquirer({}, method='sso')
    route=dict(sso_start_url='https://example.com/start',sso_region='us-east-1',sso_account_id='123456789012',sso_role_name='Example')
    owner._sso_route(route)
    settings,_=owner._prepare(REQUEST)
    assert settings['account'] == ''
    assert 'sso_account_id = 123456789012' in (owner._root/'.aws/config').read_text()
    await owner.aclose()


@pytest.mark.parametrize('value',[None,1,'bad\nvalue'])
def test_sso_routes_validate_before_files(value):
    owner=aws_login.AwsLoginAcquirer({}, method='sso')
    route=dict(sso_start_url='https://example.com/start',sso_region='us-east-1',sso_account_id='123456789012',sso_role_name=value)
    with pytest.raises((ConfigurationRequiredError,ContractError)):
        owner._sso_route(route)
    assert owner._root is None


@pytest.mark.asyncio
async def test_builtin_unavailable_has_no_cli_fallback(monkeypatch):
    owner=session_providers.SDKProfileAcquirer()
    monkeypatch.setattr(owner._ambient,'acquire',AsyncMock(return_value=(None,{})))
    with pytest.raises(InteractionRequiredError):
        await owner.acquire(REQUEST)
    await owner.aclose()


@pytest.mark.asyncio
async def test_builtin_late_ambient_does_not_publish(monkeypatch):
    owner=session_providers.SDKProfileAcquirer()
    async def late(*args):
        owner._closed=True
        return SessionMaterial('frozen',access_key='key',secret_key='secret'),{}
    monkeypatch.setattr(owner._ambient,'acquire',late)
    with pytest.raises(SessionClosedError):
        await owner.acquire(REQUEST)
    await owner.aclose()


@pytest.mark.asyncio
async def test_configuration_required_preserved_through_session_and_dynamic_owner(tmp_path):
    path=tmp_path/'adapter.py'
    path.write_text('''from api_agent.providers.contracts import ConfigurationRequiredError
class Adapter:
 version=1
 async def acquire(self, request, *, previous=None):
  raise ConfigurationRequiredError(('BEDROCK_LOGIN_SSO_ACCOUNT_ID',))
 async def aclose(self): pass
def factory(config, services): return Adapter()
''')
    adapter=await session_providers.load_acquirer(str(path)+':factory',{}, {})
    owner=AcquiredSession(adapter,REQUEST)
    with pytest.raises(ConfigurationRequiredError) as raised:
        await owner()
    assert raised.value.fields == ('BEDROCK_LOGIN_SSO_ACCOUNT_ID',)
    await owner.aclose()


@pytest.mark.asyncio
async def test_unknown_expiry_refresh_policy_reprobes_without_fabricating_expiry():
    material=SessionMaterial('frozen',access_key='key',secret_key='secret',session_token='token',refresh_after=60)
    adapter=SimpleNamespace(acquire=AsyncMock(return_value=material),aclose=AsyncMock())
    clock=[0]
    owner=AcquiredSession(adapter,REQUEST,clock=lambda:clock[0])
    await owner()
    clock[0]=59
    await owner()
    assert adapter.acquire.await_count == 1 and material.expires_at is None
    clock[0]=60
    adapter.acquire.return_value=replace(material,refresh_after=120)
    await owner()
    assert adapter.acquire.await_count == 2
    await owner.aclose()


@pytest.mark.asyncio
async def test_ambient_cannot_change_profile_during_owned_lifetime(monkeypatch):
    owner=ambient.AmbientSession()
    monkeypatch.setattr(owner.transport,'acquire',AsyncMock(return_value=MATERIAL))
    await owner.acquire(REQUEST)
    with pytest.raises(ContractError,match='binding changed'):
        await owner.acquire(replace(REQUEST,profile='different'))
    assert owner.transport.acquire.await_count == 1
    await owner.aclose()


@pytest.mark.parametrize('config', [dict(BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID='123456789012'),
    dict(BEDROCK_LOGIN_EXPECTED_PRINCIPAL=IDENTITY['Arn']),{}])
def test_native_independent_optional_assertions_construct_without_other_fields(config):
    assert aws_login.AwsLoginAcquirer(config)._root is None


@pytest.mark.asyncio
async def test_selected_profile_region_is_frozen_when_region_override_is_blank(monkeypatch):
    owner=ambient.AmbientSession()
    monkeypatch.setattr(owner.transport,'acquire',AsyncMock(return_value=dict(MATERIAL,region='eu-west-1')))
    request=replace(REQUEST,region=None)
    material,_=await owner.acquire(request)
    assert material.worker_config(request,time.time())['region'] == 'eu-west-1'
    await owner.aclose()


@pytest.mark.asyncio
async def test_native_without_assertions_pins_fresh_identity_and_never_reprobes_ambient(monkeypatch):
    owner=aws_login.AwsLoginAcquirer({})
    probe=AsyncMock(return_value=(None,{}))
    monkeypatch.setattr(owner._ambient,'acquire',probe)
    bridge=SimpleNamespace(open=AsyncMock(),aclose=AsyncMock())
    owner._interaction=bridge
    identity=dict(Account='123456789012',Arn='arn:aws:sts::123456789012:assumed-role/Example/session')
    native_result=dict(MATERIAL,SessionToken='token',expires_at=time.time()+300,identity=identity)
    native_result.pop('region')
    login=AsyncMock(return_value=native_result)
    monkeypatch.setattr(owner._transport,'acquire',login)
    request=replace(REQUEST,interactive=True,interaction=object())
    first=await owner.acquire(request)
    await owner.acquire(request,previous=first)
    assert login.await_args_list[0].args[0]['account'] == ''
    assert login.await_args_list[1].args[0]['account'] == '123456789012'
    assert login.await_args_list[1].args[0]['principal'] == 'arn:aws:sts::123456789012:assumed-role/Example'
    assert login.await_args_list[1].args[0]['login'] is False
    assert probe.await_count == 1
    await owner.aclose()


@pytest.mark.parametrize('region',[None,'bad-region'])
def test_ambient_no_resolved_region_stops_before_sts(sdk,region):
    sdk[0].region_name=region
    with pytest.raises(ValueError):
        login_worker.ambient(dict(region=None))
    assert sdk[3] == []


@pytest.mark.parametrize('assertions',[dict(account=IDENTITY['Account']),dict(principal=IDENTITY['Arn'])])
def test_matching_optional_assertions_independently_verify(sdk,assertions):
    assert login_worker.ambient(dict(profile='default',region='us-east-1',**assertions))['identity'] == IDENTITY


@pytest.mark.asyncio
@pytest.mark.parametrize('arn', ['arn:aws:sts::123456789012:federated-user/example',
    'arn:aws:iam::123456789012:root'])
async def test_ambient_exact_identity_pin_refreshes_non_assumed_role_principals(sdk,monkeypatch,arn):
    session=sdk[0]
    identity=dict(Account='123456789012',Arn=arn)
    session.client=lambda *args,**kwargs:SimpleNamespace(get_caller_identity=lambda:dict(identity),close=lambda:None)
    owner=ambient.AmbientSession()
    async def probe(settings,environment):
        return login_worker.ambient(settings)
    monkeypatch.setattr(owner.transport,'acquire',probe)
    first,_=await owner.acquire(REQUEST)
    assert owner._identity == ('123456789012',arn)
    refreshed,_=await owner.acquire(REQUEST)
    assert refreshed.worker_config(REQUEST,time.time()) == first.worker_config(REQUEST,time.time())
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('result',[None,[],{},dict(MATERIAL,region=None),
    dict(unavailable=True),dict(unavailable=False,sso={}),dict(unavailable=True,sso=[]),
    dict(unavailable=True,sso={'unknown':'value'}),dict(unavailable=True,sso={'sso_region':1}),
    dict(MATERIAL,unavailable=True),dict(MATERIAL,identity=dict(Account=True,Arn=IDENTITY['Arn']))])
async def test_ambient_rejects_malformed_or_ambiguous_results_before_recovery_or_pin(monkeypatch,result):
    owner=ambient.AmbientSession()
    monkeypatch.setattr(owner.transport,'acquire',AsyncMock(return_value=result))
    with pytest.raises(ContractError):
        await owner.acquire(REQUEST)
    assert owner._identity is None
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('result',[None,{},dict(MATERIAL),
    dict(AccessKeyId='key',SecretAccessKey='secret',SessionToken='token',expires_at=time.time()+300,
         identity=dict(Account='123456789012',Arn='arn:aws:iam::999999999999:user/example'))])
async def test_native_rejects_malformed_identity_or_result_before_publication(monkeypatch,result):
    owner=aws_login.AwsLoginAcquirer({})
    monkeypatch.setattr(owner._ambient,'acquire',AsyncMock(return_value=(None,{})))
    owner._interaction=SimpleNamespace(open=AsyncMock(),aclose=AsyncMock())
    monkeypatch.setattr(owner._transport,'acquire',AsyncMock(return_value=result))
    with pytest.raises(ContractError):
        await owner.acquire(replace(REQUEST,interactive=True,interaction=object()))
    assert owner._verified_identity is None and not owner._logged_in
    await owner.aclose()


@pytest.mark.asyncio
async def test_pre_cancelled_ambient_never_admits_worker(monkeypatch):
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport
    owner=LoginTransport()
    launch=AsyncMock(side_effect=AssertionError('No worker after cancellation'))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    async def cancelled():
        asyncio.current_task().cancel()
        await owner.acquire(dict(method='ambient',login=False),{})
    task=asyncio.create_task(cancelled())
    with pytest.raises(asyncio.CancelledError):
        await task
    launch.assert_not_awaited()
    assert not owner._processes
    await owner.aclose()


@pytest.mark.asyncio
async def test_close_during_admission_never_starts_ambient_worker(monkeypatch):
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport
    owner=LoginTransport()
    launch=AsyncMock(side_effect=AssertionError('No worker after close'))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    original=asyncio.create_task
    def close_before_launch(work,*args,**kwargs):
        if work.cr_code.co_name == 'launch':
            owner._closed=True
        return original(work,*args,**kwargs)
    monkeypatch.setattr(asyncio,'create_task',close_before_launch)
    with pytest.raises(SessionClosedError):
        await owner.acquire(dict(method='ambient',login=False),{})
    launch.assert_not_awaited()
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix',['arn:aws:iam::123456789012:user/team:assumed-role/group/',
    'arn:aws:sts::123456789012:federated-user/team:assumed-role/group/'])
async def test_role_marker_inside_principal_path_never_allows_identity_switch(sdk,monkeypatch,prefix):
    identity=dict(Account='123456789012',Arn=prefix+'alice')
    sdk[0].client=lambda *args,**kwargs:SimpleNamespace(get_caller_identity=lambda:dict(identity),close=lambda:None)
    owner=ambient.AmbientSession()
    async def probe(settings,environment):
        return login_worker.ambient(settings)
    monkeypatch.setattr(owner.transport,'acquire',probe)
    await owner.acquire(REQUEST)
    assert owner._identity == ('123456789012',prefix+'alice')
    await owner.acquire(REQUEST)
    identity['Arn']=prefix+'bob'
    with pytest.raises(login_worker.AcquisitionFailure, match='identity_verification'):
        await owner.acquire(REQUEST)
    await owner.aclose()


@pytest.mark.parametrize('arn',['arn:aws:iam::123456789012:user/team:assumed-role/group/alice',
    'arn:aws:sts::123456789012:federated-user/team:assumed-role/group/alice'])
def test_optional_exact_principal_assertion_preserves_role_marker_inside_path(sdk,arn):
    sdk[0].client=lambda *args,**kwargs:SimpleNamespace(get_caller_identity=lambda:dict(Account='123456789012',Arn=arn),close=lambda:None)
    assert login_worker.ambient(dict(profile='default',region='us-east-1',principal=arn))['identity']['Arn'] == arn


@pytest.fixture
def empty_sdk_profile_files(tmp_path,monkeypatch):
    import boto3
    config=tmp_path/'config'
    credentials=tmp_path/'credentials'
    config.write_text('')
    credentials.write_text('')
    monkeypatch.setenv('AWS_CONFIG_FILE',str(config))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE',str(credentials))
    monkeypatch.setenv('AWS_ACCESS_KEY_ID','unused-synthetic-key')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY','unused-synthetic-secret')
    def forbidden(self):
        pytest.fail('Missing explicit profile must never resolve another credential chain')
    monkeypatch.setattr(boto3.Session,'get_credentials',forbidden)
    return config,credentials


@pytest.mark.parametrize('profile',['default','new-profile'])
def test_actual_missing_selected_profile_is_unavailable_without_chain_fallback(empty_sdk_profile_files,profile):
    assert login_worker.ambient(dict(profile=profile,region='us-east-1')) == {'unavailable':True,'sso':{}}


@pytest.mark.asyncio
@pytest.mark.parametrize('profile',['default','new-profile'])
async def test_native_first_login_recovers_missing_profile_into_same_isolated_name(empty_sdk_profile_files,monkeypatch,profile):
    owner=aws_login.AwsLoginAcquirer({})
    async def probe(settings,environment):
        return login_worker.ambient(settings)
    monkeypatch.setattr(owner._ambient.transport,'acquire',probe)
    owner._interaction=SimpleNamespace(open=AsyncMock(),aclose=AsyncMock())
    native_result=dict(AccessKeyId='key',SecretAccessKey='secret',SessionToken='token',expires_at=time.time()+300,identity=IDENTITY)
    login=AsyncMock(return_value=native_result)
    monkeypatch.setattr(owner._transport,'acquire',login)
    request=replace(REQUEST,profile=profile,interactive=True,interaction=object())
    result=await owner.acquire(request)
    assert result.worker_config(request,time.time())['profile'] == profile
    assert login.await_args.args[0]['profile'] == profile
    section='default' if profile=='default' else 'profile '+profile
    assert (owner._root/'.aws/config').read_text().startswith('['+section+']\n')
    assert all(path.read_text()=='' for path in empty_sdk_profile_files)
    await owner.aclose()


@pytest.mark.asyncio
async def test_builtin_missing_profile_never_starts_native_recovery(empty_sdk_profile_files,monkeypatch):
    owner=session_providers.SDKProfileAcquirer()
    async def probe(settings,environment):
        return login_worker.ambient(settings)
    monkeypatch.setattr(owner._ambient.transport,'acquire',probe)
    with pytest.raises(InteractionRequiredError):
        await owner.acquire(REQUEST)
    await owner.aclose()


def test_actual_malformed_profile_configuration_never_becomes_login_recovery(empty_sdk_profile_files):
    from botocore.exceptions import ConfigParseError
    empty_sdk_profile_files[0].write_text('not a valid INI document')
    with pytest.raises(ConfigParseError):
        login_worker.ambient(dict(profile='default',region='us-east-1'))


@pytest.mark.parametrize('message,login,unavailable', [
    ("Unable to load a existing login session for session chosen. Please reauthenticate with 'aws login'.", 'chosen', True),
    ('Failed to load access token from token cache, missing required fields: accessToken.', 'chosen', False),
    ('Private key not found in cached token.', 'chosen', False),
    ("Unable to load a existing login session for session other. Please reauthenticate with 'aws login'.", 'chosen', False),
    ("Unable to load a existing login session for session chosen. Please reauthenticate with 'aws login'.", None, False),
])
def test_only_exact_missing_selected_login_cache_allows_recovery(sdk, message, login, unavailable):
    from botocore.exceptions import LoginTokenLoadError
    session, _, _, _ = sdk
    session._session.full_config['profiles']['default']['login_session'] = login
    error = LoginTokenLoadError(error_msg=message)
    def fail():
        raise error
    session.get_credentials = fail
    if unavailable:
        assert login_worker._ambient(dict(profile='default', region='us-east-1'))['unavailable'] is True
    else:
        with pytest.raises(LoginTokenLoadError):
            login_worker._ambient(dict(profile='default', region='us-east-1'))


@pytest.mark.parametrize('session_name,cache,unavailable', [
    ('chosen', {}, True),
    (None, {}, True),
    ('chosen', {'chosen': {}}, False),
    (None, {'https://example.com/start': {}}, False),
])
def test_sso_missing_and_malformed_sdk_cache_have_distinct_outcomes(sdk, session_name, cache, unavailable):
    import hashlib
    from botocore.exceptions import SSOTokenLoadError
    from botocore.utils import SSOTokenLoader
    session, _, _, _ = sdk
    session._session.full_config['profiles']['default']['sso_session'] = session_name
    session._session.full_config['profiles']['default']['sso_start_url'] = 'https://example.com/start'
    loader = SSOTokenLoader(cache={hashlib.sha1(key.encode()).hexdigest():value for key,value in cache.items()})
    def fail():
        return loader('https://example.com/start', session_name=session_name)
    session.get_credentials = fail
    if unavailable:
        assert login_worker._ambient(dict(profile='default', region='us-east-1'))['unavailable'] is True
    else:
        with pytest.raises(SSOTokenLoadError):
            login_worker._ambient(dict(profile='default', region='us-east-1'))


def test_missing_sso_error_without_matching_route_fails_closed(sdk):
    from botocore.exceptions import SSOTokenLoadError
    session, _, _, _ = sdk
    session._session.full_config = {'profiles': {'default': {}}}
    def fail():
        raise SSOTokenLoadError(error_msg='Token for other does not exist')
    session.get_credentials = fail
    with pytest.raises(SSOTokenLoadError):
        login_worker._ambient(dict(profile='default', region='us-east-1'))


def test_implicit_missing_sdk_profile_preserves_unavailable_outcome(sdk, monkeypatch):
    import boto3
    from botocore.exceptions import ProfileNotFound
    def missing(**kwargs):
        assert kwargs['profile_name'] is None
        raise ProfileNotFound(profile='missing')
    monkeypatch.setattr(boto3, 'Session', missing)
    assert login_worker.ambient(dict(profile=None, region='us-east-1')) == dict(unavailable=True, sso={})
