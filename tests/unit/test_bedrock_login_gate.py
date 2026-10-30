"""Native profile admission is shared across independent owners and processes."""
import asyncio
from contextlib import asynccontextmanager
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.bedrock import login_gate
from api_agent.llm_providers.bedrock.acquisition import SessionRequest, SessionMaterial
from api_agent.llm_providers.bedrock.aws_login import AwsLoginAcquirer
from api_agent.providers.contracts import ContractError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.progress import component_timeout


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
async def test_waiting_profile_gate_releases_after_every_owner_outcome(tmp_path, outcome):
    paths = {'credentials': str(tmp_path / 'credentials')}
    entered, finish, peer_entered, peer_attempted = (asyncio.Event() for _ in range(4))
    async def first():
        async with login_gate.profile_login_gate(paths, 'default'):
            entered.set()
            await finish.wait()
            if outcome == 'failure':
                raise ValueError('Synthetic acquisition failure')
    async def peer():
        peer_attempted.set()
        async with login_gate.profile_login_gate(paths, 'default'):
            peer_entered.set()
    owner = asyncio.create_task(first())
    await entered.wait()
    waiter = asyncio.create_task(peer())
    await peer_attempted.wait()
    assert not peer_entered.is_set()
    if outcome == 'cancel':
        owner.cancel()
    else:
        finish.set()
    result = await asyncio.gather(owner, return_exceptions=True)
    if outcome == 'failure':
        assert isinstance(result[0], ValueError)
    if outcome == 'cancel':
        assert isinstance(result[0], asyncio.CancelledError)
    await asyncio.wait_for(waiter, 2)
    assert peer_entered.is_set()
    assert next(tmp_path.glob('.api-agent-session-*.lock')).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_profile_and_credential_files_are_distinct_admission_keys(tmp_path):
    first = {'credentials': str(tmp_path / 'credentials')}
    other = {'credentials': str(tmp_path / 'other-credentials')}
    async with login_gate.profile_login_gate(first, 'default'):
        async with component_timeout('Independent native sessions', 1):
            async with login_gate.profile_login_gate(first, 'other-profile'):
                async with login_gate.profile_login_gate(other, 'default'):
                    pass


@pytest.mark.asyncio
async def test_waiting_gate_obeys_component_deadline_and_does_not_retain_descriptor(tmp_path):
    paths = {'credentials': str(tmp_path / 'credentials')}
    async with login_gate.profile_login_gate(paths, None):
        with pytest.raises(TimeoutError):
            async with component_timeout('Native admission deadline', .02):
                async with login_gate.profile_login_gate(paths, None):
                    pytest.fail('Conflicting gate was admitted')
    async with component_timeout('Reacquire after deadline', 1):
        async with login_gate.profile_login_gate(paths, None):
            pass


@pytest.mark.asyncio
async def test_process_exit_releases_native_admission(tmp_path):
    paths = {'credentials': str(tmp_path / 'credentials')}
    script = '''
import asyncio,sys
from api_agent.llm_providers.bedrock.login_gate import profile_login_gate
async def run():
    async with profile_login_gate({'credentials':sys.argv[1]}, 'default'):
        print('admitted',flush=True)
        await asyncio.Event().wait()
asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, paths['credentials'],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b'admitted\n'
        with pytest.raises(TimeoutError):
            async with component_timeout('Cross-process contention', .05):
                async with login_gate.profile_login_gate(paths, 'default'):
                    pytest.fail('A second process bypassed native admission')
    finally:
        process.terminate()
        await asyncio.wait_for(process.communicate(), 5)
    async with component_timeout('Reacquire exited process lock', 1):
        async with login_gate.profile_login_gate(paths, 'default'):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize('paths,profile', [({'credentials': 'relative'}, 'default'),
                                          ({'credentials': '/absolute'}, 3)])
async def test_invalid_gate_binding_is_rejected(paths, profile):
    with pytest.raises(ContractError, match='coordination binding'):
        async with login_gate.profile_login_gate(paths, profile):
            pytest.fail('Invalid binding was admitted')


@pytest.mark.asyncio
async def test_non_posix_gate_rejects_before_acquisition(monkeypatch):
    monkeypatch.setattr(login_gate, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(SetupPrerequisiteError, match='POSIX'):
        async with login_gate.profile_login_gate({}, None):
            pytest.fail('Unsupported platform was admitted')


@pytest.mark.asyncio
async def test_non_regular_coordination_file_is_rejected(tmp_path):
    paths = {'credentials': str(tmp_path / 'credentials')}
    async with login_gate.profile_login_gate(paths, 'default'):
        pass
    target = next(tmp_path.glob('.api-agent-session-*.lock'))
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ContractError, match='Unsafe'):
        async with login_gate.profile_login_gate(paths, 'default'):
            pytest.fail('FIFO was admitted')


@pytest.mark.asyncio
async def test_parallel_cold_owners_recheck_saved_session_before_another_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'credentials'))
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport
    from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
    persisted = None
    started, finish = asyncio.Event(), asyncio.Event()
    requests, prompts = [], []
    async def ambient(self, request, **kwargs):
        requests.append(request)
        return persisted, {}
    async def opened(self):
        prompts.append(self)
    async def acquire(self, settings, environment, interaction):
        nonlocal persisted
        started.set()
        await finish.wait()
        persisted = SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret',
            session_token='synthetic-token', expires_at=time.time() + 300)
        return dict(AccessKeyId='synthetic-key', SecretAccessKey='synthetic-secret',
            SessionToken='synthetic-token', expires_at=persisted.expires_at,
            identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(AmbientSession, 'acquire', ambient)
    monkeypatch.setattr(LoginTransport, 'acquire', acquire)
    monkeypatch.setattr(LoginInteraction, 'open', opened)
    monkeypatch.setattr(LoginInteraction, 'aclose', AsyncMock())
    ordinary = AwsLoginAcquirer({'BEDROCK_API_MODE': 'responses'})
    tools = AwsLoginAcquirer({'BEDROCK_API_MODE': 'completions'})
    request = SessionRequest('default', 'us-east-1', None, True, interaction=object())
    first = asyncio.create_task(ordinary.acquire(request))
    await started.wait()
    peer_attempted = asyncio.Event()
    async def acquire_peer():
        peer_attempted.set()
        return await tools.acquire(request)
    second = asyncio.create_task(acquire_peer())
    try:
        await peer_attempted.wait()
        assert len(requests) == len(prompts) == 1
        finish.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert [result.kind for result in results] == ['temporary', 'frozen']
        assert len(requests) == 2 and len(prompts) == 1
        assert tools._root is None and tools._ambient_selected
    finally:
        finish.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await ordinary.aclose()
        await tools.aclose()


@pytest.mark.asyncio
async def test_owner_closed_while_waiting_cannot_admit_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'credentials'))
    owner = AwsLoginAcquirer({})
    from api_agent.llm_providers.bedrock import aws_login
    waiting = asyncio.Event()
    @asynccontextmanager
    async def observed_gate(*args):
        waiting.set()
        async with login_gate.profile_login_gate(*args):
            yield
    monkeypatch.setattr(aws_login, 'profile_login_gate', observed_gate)
    monkeypatch.setattr(owner._ambient, 'acquire', AsyncMock(side_effect=AssertionError('Closed owner probed credentials')))
    async with login_gate.profile_login_gate(owner._persistence, 'default'):
        pending = asyncio.create_task(owner.acquire(SessionRequest('default', 'us-east-1', None, True)))
        await waiting.wait()
        owner._closed = True
    with pytest.raises(SessionClosedError):
        await pending
    await owner.aclose()
