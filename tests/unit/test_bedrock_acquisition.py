import math
import pytest
from api_agent.llm_providers.bedrock.acquisition import SessionRequest, SessionMaterial
from api_agent.providers.contracts import ContractError


def request(index=None):
    return SessionRequest('chosen', 'region', index, False, object(), object())


def temporary(**changes):
    return SessionMaterial(**dict(kind='temporary', access_key='key', secret_key='secret', session_token='token', expires_at=100, **changes))


def test_opaque_frozen_contracts():
    from dataclasses import FrozenInstanceError
    item = request()
    assert repr(item) == '<SessionRequest>'
    assert repr(temporary()) == '<SessionMaterial>'
    with pytest.raises(FrozenInstanceError):
        item.profile = 'new'


def test_profile_snapshot_is_fresh():
    material = SessionMaterial('profile')
    result = material.worker_config(request(), 0)
    assert result == {'mode': 'session', 'profile': 'chosen', 'region': 'region'}
    result.clear()
    assert material.worker_config(request(), 0)['mode'] == 'session'


@pytest.mark.parametrize('field,value', [('user_index', 1), ('access_key', 'key'), ('secret_key', 'secret'), ('session_token', 'token'), ('expires_at', 100)])
def test_profile_rejects_temporary_fields(field, value):
    with pytest.raises(ContractError, match='profile'):
        SessionMaterial('profile', **{field:value}).worker_config(request(), 0)


def test_profile_rejects_index():
    with pytest.raises(ContractError, match='profile'):
        SessionMaterial('profile').worker_config(request(0), 0)


@pytest.mark.parametrize('index,selected', [(1, 2), (True, 1), (-1, -1)])
def test_binding_rejected(index, selected):
    with pytest.raises(ContractError, match='binding'):
        temporary(user_index=index).worker_config(request(selected), 0)


@pytest.mark.parametrize('field', ['access_key', 'secret_key', 'session_token'])
@pytest.mark.parametrize('value', [None, '', ' ', 1])
def test_missing_temporary_tuple(field, value):
    values = dict(kind='temporary', access_key='key', secret_key='secret', session_token='token', expires_at=100)
    values[field] = value
    with pytest.raises(ContractError, match='Incomplete'):
        SessionMaterial(**values).worker_config(request(), 0)


@pytest.mark.parametrize('expiry', [None, True, math.nan, math.inf, 0, -1])
def test_invalid_expiry(expiry):
    with pytest.raises(ContractError, match='Expired'):
        SessionMaterial('temporary', access_key='key', secret_key='secret', session_token='token', expires_at=expiry).worker_config(request(), 0)


def test_temporary_exact_snapshot():
    assert temporary(user_index=3).worker_config(request(3), 0) == dict(mode='temporary', profile='chosen', region='region', access_key='key', secret_key='secret', session_token='token')
    with pytest.raises(ContractError, match='kind'):
        SessionMaterial('unknown').worker_config(request(), 0)


def test_material_kind_rejects_reflection_without_invoking_it():
    class Hostile:
        def __eq__(self, other):
            raise RuntimeError('secret reflection payload')

    with pytest.raises(ContractError, match='^Invalid Bedrock session material kind$'):
        SessionMaterial(Hostile()).worker_config(request(), 0)


@pytest.mark.parametrize('selected', [True, 1.0, -1, '1', object()])
def test_request_binding_requires_exact_nonnegative_integer(selected):
    with pytest.raises(ContractError, match='binding'):
        temporary(user_index=1).worker_config(request(selected), 0)


@pytest.mark.parametrize('changes', [dict(user_index=0),dict(access_key=None),dict(secret_key=' '),
    dict(session_token=1,refresh_after=100),dict(session_token=' ',refresh_after=100),dict(expires_at=True),dict(expires_at=math.inf),
    dict(expires_at=0),dict(refresh_after='bad'),dict(refresh_after=math.nan),dict(refresh_after=-1),
    dict(session_token='token')])
def test_frozen_material_rejects_invalid_tuple_or_deadline(changes):
    values=dict(kind='frozen',access_key='key',secret_key='secret')
    values.update(changes)
    with pytest.raises(ContractError):
        SessionMaterial(**values).worker_config(request(),0)


def test_frozen_material_cannot_claim_requested_index():
    with pytest.raises(ContractError):
        SessionMaterial('frozen',user_index=0,access_key='key',secret_key='secret').worker_config(request(0),0)


def test_temporary_material_cannot_claim_frozen_refresh_policy():
    with pytest.raises(ContractError,match='refresh policy'):
        temporary(refresh_after=20).worker_config(request(),0)


@pytest.mark.parametrize('kind,region,selected',[('profile','us-east-1',None),('frozen',1,None),('frozen','bad',None),('frozen','eu-west-1','us-east-1')])
def test_resolved_region_rejects_invalid_or_changed_binding(kind,region,selected):
    with pytest.raises(ContractError,match='resolved region'):
        SessionMaterial(kind,resolved_region=region).worker_config(SessionRequest('default',selected,None,False),0)


@pytest.mark.parametrize('identity',[None,[],{},dict(Account='123456789012',Arn='arn:aws:iam::123456789012:root',extra=True),
    dict(Account=123456789012,Arn='arn:aws:iam::123456789012:root'),
    dict(Account=True,Arn='arn:aws:iam::123456789012:root'),
    dict(Account='bad',Arn='arn:aws:iam::123456789012:root'),
    dict(Account='123456789012',Arn=None),dict(Account='123456789012',Arn='arn:'),
    dict(Account='123456789012',Arn='arn:aws:iam::999999999999:root')])
def test_identity_binding_rejects_malformed_or_inconsistent_worker_metadata(identity):
    from api_agent.llm_providers.bedrock.acquisition import identity_binding
    with pytest.raises(ContractError,match='verified identity'):
        identity_binding(identity)
