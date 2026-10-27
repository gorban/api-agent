"""Fixed acquisition evidence survives ordinary setup without upstream text."""
import io
import sys
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import login_worker as worker
from api_agent.llm_providers.bedrock.login_transport import acquisition_error
from api_agent.llm_providers.bedrock.session_providers import _AcquirerOwner
from api_agent.llm_providers.bedrock.sessions import AcquiredSession
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.setup_transport import _error, _raise_error


def failure_frame(**changes):
    return dict(error='acquisition', stage='identity_verification', kind='unknown', reason='unknown', **changes)


@pytest.mark.parametrize('name,code,kind,reason', [
    ('SSLError', None, 'unavailable', 'tls'),
    ('EndpointConnectionError', None, 'unavailable', 'connectivity'),
    ('ClientError', 'AccessDeniedException', 'authorization', 'rejected'),
    ('ClientError', 'ExpiredToken', 'authentication', 'rejected'),
    ('ValueError', None, 'invalid_request', 'malformed'),
    ('RuntimeError', None, 'unknown', 'unknown'),
])
def test_worker_emits_only_fixed_evidence(monkeypatch, name, code, kind, reason):
    error_type = {'ValueError': ValueError, 'RuntimeError': RuntimeError}.get(name) or type(name, (Exception,), {})
    error = error_type('private-canary')
    error.response = {'Error': {'Code': code, 'Message': 'private-canary'}}
    def fail():
        raise error
    monkeypatch.setattr(worker, 'run', lambda config: worker.diagnosed('identity_verification', fail))
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{}\n')))
    frames = []
    monkeypatch.setattr(worker, 'emit', frames.append)

    worker.main()

    assert frames == [dict(error='acquisition', stage='identity_verification', kind=kind, reason=reason)]
    assert 'private-canary' not in repr(frames)


@pytest.mark.parametrize('failure', [ImportError('private-canary'), worker.AcquisitionFailure('sign_in', 'unknown', 'unknown')])
def test_worker_preserves_prerequisite_and_nested_stage(failure):
    def fail():
        raise failure
    with pytest.raises(type(failure)) as caught:
        worker.diagnosed('credential_resolution', fail)
    assert caught.value is failure


def test_diagnosed_success():
    assert worker.diagnosed('result_validation', lambda value: value, 42) == 42


@pytest.mark.parametrize('stage', ['credential_resolution', 'sign_in', 'credential_export', 'identity_verification', 'result_validation', 'worker_protocol'])
def test_public_setup_roundtrip_preserves_stage_and_preinference_execution(stage):
    frame = failure_frame()
    frame['stage'] = stage
    error = acquisition_error(frame)

    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(_error(error))

    assert caught.value.kind == 'unknown'
    assert caught.value.execution == 'not_started'
    assert caught.value.code == 'unknown'
    assert str(caught.value) == 'AWS sign-in failed. Stage: ' + stage + '; category: unknown; reason: unknown.\ncode: unknown'


@pytest.mark.parametrize('change', [
    {'extra': 'private-canary'}, {'stage': 'private-canary'}, {'stage': []},
    {'error': 'other'}, {'reason': []}, {'kind': []},
    {'reason': 'private-canary'}, {'kind': 'authentication'},
    {'reason': 'rejected'}, {'reason': 'tls', 'kind': 'authorization'},
])
def test_invalid_failure_frames_are_rejected_without_echo(change):
    frame = failure_frame()
    frame.update(change)
    with pytest.raises(ContractError, match='^Invalid AWS sign-in failure response$'):
        acquisition_error(frame)


@pytest.mark.parametrize('kind,reason', [('unavailable', 'tls'), ('unavailable', 'connectivity'), ('invalid_request', 'malformed'), ('authentication', 'rejected'), ('authorization', 'rejected')])
def test_valid_fixed_classifications(kind, reason):
    frame = failure_frame()
    frame.update(kind=kind, reason=reason)
    assert acquisition_error(frame).kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['adapter', 'session'])
async def test_acquisition_wrappers_preserve_safe_error(wrapper):
    error = acquisition_error(failure_frame())
    class Adapter:
        async def acquire(self, request, *, previous=None):
            raise error
        async def aclose(self):
            pass
    if wrapper == 'adapter':
        owner = _AcquirerOwner(Adapter(), '_synthetic_diagnostic_adapter')
        action = lambda: owner.acquire(None)
    else:
        owner = AcquiredSession(Adapter(), None)
        action = owner

    with pytest.raises(ProviderAccessError) as caught:
        await action()

    assert caught.value is error
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('login', [False, True])
async def test_native_handoff_preserves_structured_failure_and_stops_once(monkeypatch, login):
    from test_bedrock_login_transport import Bridge, Process, frame
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport
    frames = ([frame({'auth_done': True})] if login else []) + [frame(failure_frame())]
    process = Process(frames)
    stopped = []
    async def launch(*args, **kwargs):
        return process
    async def stop(child):
        stopped.append(child)
        transport._processes.discard(child)
    transport = LoginTransport()
    monkeypatch.setattr('api_agent.llm_providers.bedrock.login_transport.asyncio.create_subprocess_exec', launch)
    monkeypatch.setattr(transport, '_stop', stop)

    with pytest.raises(ProviderAccessError) as caught:
        await transport.acquire(dict(login=login, method='direct'), {}, Bridge() if login else None)

    assert caught.value.kind == 'unknown'
    assert 'identity_verification' in str(caught.value)
    assert caught.value.execution == 'not_started'
    assert stopped == [process]
    await transport.aclose()


@pytest.mark.parametrize('stage', ['sign_in', 'credential_export', 'result_validation', 'identity_verification'])
def test_native_run_classifies_postlogin_stages(monkeypatch, stage):
    monkeypatch.setattr(worker.shutil, 'which', lambda name: '/synthetic/aws')
    def cli(command, environment, **kwargs):
        if '--version' in command:
            return b'aws-cli/2.32.0'
        actual_stage = 'sign_in' if 'login' in command else 'credential_export'
        if actual_stage == stage:
            raise RuntimeError('private-canary')
        return b'{}'
    def credentials(raw):
        if stage == 'result_validation':
            raise ValueError('private-canary')
        return {'SessionToken': 'private-canary'}, 42
    def identity(material, config):
        if stage == 'identity_verification':
            raise RuntimeError('private-canary')
    monkeypatch.setattr(worker, 'cli', cli)
    monkeypatch.setattr(worker, 'credentials', credentials)
    monkeypatch.setattr(worker, 'verify_identity', identity)
    monkeypatch.setattr(worker, 'emit', lambda value: None)

    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker.run(dict(profile='synthetic', region='us-east-1', method='direct', login=True))

    assert caught.value.stage == stage
    assert 'private-canary' not in str(caught.value)


@pytest.mark.parametrize('response', [None, [], {'Error': None}, {'Error': []}, {'Error': {'Code': None}}, {'Error': {'Code': []}}])
def test_unstructured_sdk_response_keeps_stage_without_private_details(response):
    error = RuntimeError('private-canary')
    error.response = response
    def fail():
        raise error

    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker.diagnosed('credential_export', fail)

    assert (caught.value.stage, caught.value.kind, caught.value.reason) == ('credential_export', 'unknown', 'unknown')


def test_native_cli_failure_does_not_guess_authentication_or_malformed_result():
    def fail():
        raise worker.CLIExecutionFailure('private-canary')
    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker.diagnosed('credential_export', fail)
    assert (caught.value.stage, caught.value.kind, caught.value.reason) == ('credential_export', 'unknown', 'unknown')
