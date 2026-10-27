"""Rejected selected credentials recover without loosening identity assertions."""
import sys
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import login_worker as worker

IDENTITY = dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')
CONFIG = dict(profile='chosen', region='us-east-1')
REJECTED = ('identity_verification', 'authentication', 'rejected')
OTHER = [
    ('credential_resolution', 'authentication', 'rejected'),
    ('identity_verification', 'authorization', 'rejected'),
    ('identity_verification', 'authentication', 'unknown'),
    ('identity_verification', 'unavailable', 'connectivity'),
    ('identity_verification', 'invalid_request', 'malformed'),
    ('identity_verification', 'unknown', 'unknown'),
]


def fail(error):
    def action(*args, **kwargs):
        raise error
    return action


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    credentials = tmp_path / 'credentials'
    credentials.write_bytes(b'[chosen]\naws_access_key_id=key\naws_secret_access_key=secret\naws_session_token=token\n')
    marker = dict(identity=IDENTITY, expires_at=9999999999,
                  fingerprint=worker.fingerprint(['key', 'secret', 'token']))
    lock_owner = worker.ProfileStore(dict(home=str(tmp_path), config=str(tmp_path / 'config'),
        credentials=str(credentials), login=str(tmp_path / 'login')), 'chosen')
    store = SimpleNamespace(credentials=credentials, profile='chosen', locked=lock_owner.locked,
        snapshot=lambda:marker, unmasked_credentials=lambda:b'[other]\n')
    monkeypatch.setattr(worker, 'capture_paths', lambda:{})
    monkeypatch.setattr(worker, 'ProfileStore', lambda paths, profile:store)
    return store


def test_rejected_owned_snapshot_uses_masked_native_route_and_binding(snapshot, monkeypatch):
    monkeypatch.setattr(worker, 'verify_identity', fail(worker.AcquisitionFailure(*REJECTED)))
    calls = []
    def native(config):
        calls.append(config)
        masked = worker.Path(worker.os.environ['AWS_SHARED_CREDENTIALS_FILE'])
        assert masked.read_bytes() == b'[other]\n'
        return dict(unavailable=True, sso={'sso_region':'us-east-1'})
    monkeypatch.setattr(worker, '_ambient', native)
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(snapshot.credentials))

    result = worker.ambient(CONFIG)

    assert calls == [dict(CONFIG, account=IDENTITY['Account'], principal=IDENTITY['Arn'])]
    assert result == dict(unavailable=True, sso={'sso_region':'us-east-1'}, identity=IDENTITY)
    assert worker.os.environ['AWS_SHARED_CREDENTIALS_FILE'] == str(snapshot.credentials)


@pytest.mark.parametrize('classification', OTHER)
def test_other_snapshot_failures_do_not_attempt_native(snapshot, monkeypatch, classification):
    error = worker.AcquisitionFailure(*classification)
    monkeypatch.setattr(worker, 'verify_identity', fail(error))
    monkeypatch.setattr(worker, '_ambient', lambda config:pytest.fail('Unexpected native recovery'))

    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker.ambient(CONFIG)

    assert caught.value is error


@pytest.fixture
def sdk(monkeypatch):
    credentials = SimpleNamespace(get_frozen_credentials=lambda:SimpleNamespace(access_key='key', secret_key='secret', token='token'))
    calls = []
    session = SimpleNamespace(profile_name='chosen', region_name='us-east-1', client=object(),
        get_credentials=lambda:credentials,
        _session=SimpleNamespace(full_config={'profiles':{'chosen':{'sso_region':'us-east-1'}}}))
    def selected(**kwargs):
        calls.append(kwargs)
        return session
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(Session=selected))
    return calls


@pytest.mark.parametrize('code', ['ExpiredToken', 'ExpiredTokenException', 'InvalidClientTokenId', 'UnrecognizedClientException', 'InvalidToken'])
def test_rejected_sts_material_returns_selected_route_for_attended_recovery(sdk, monkeypatch, code):
    error = type('ClientError', (Exception,), {})()
    error.response = {'Error':{'Code':code}}
    monkeypatch.setattr(worker, 'verify_identity', fail(error))

    result = worker._ambient(CONFIG)

    assert result == dict(unavailable=True, sso=dict(sso_start_url=None, sso_region='us-east-1', sso_account_id=None, sso_role_name=None))
    assert sdk == [dict(profile_name='chosen', region_name='us-east-1')]


@pytest.mark.parametrize('classification', OTHER)
def test_other_diagnosed_failures_still_propagate(sdk, monkeypatch, classification):
    error = worker.AcquisitionFailure(*classification)
    monkeypatch.setattr(worker, 'verify_identity', fail(error))

    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker._ambient(CONFIG)

    assert caught.value is error
