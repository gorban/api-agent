import asyncio
import json
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import login_transport as module
from api_agent.providers.contracts import ContractError, InteractionRequiredError, SessionClosedError, SetupPrerequisiteError


class Input:
    def __init__(self):
        self.data = bytearray()
    def write(self, raw):
        self.data.extend(raw)
    async def drain(self):
        pass


class Output:
    def __init__(self, frames):
        self.frames = iter(frames)
    async def readline(self):
        await asyncio.sleep(0)
        return next(self.frames, b'')


class Process:
    def __init__(self, frames, code=0):
        self.stdin = Input()
        self.stdout = Output(frames)
        self.code = code
    async def wait(self):
        return self.code


class Bridge:
    async def check_ready(self):
        pass
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.output = []
        self.sent = asyncio.Event()
    async def read(self):
        await self.sent.wait()
        value = next(self.replies, None)
        await asyncio.sleep(0)
        if isinstance(value, BaseException):
            raise value
        return value
    async def write(self, text):
        self.output.append(text)
        self.sent.set()


def frame(value):
    return json.dumps(value).encode()+b'\n'


@pytest.fixture
def setup(monkeypatch):
    transport = module.LoginTransport()
    processes, launches, stopped = [], [], []
    async def launch(*args, **kwargs):
        launches.append((args,kwargs))
        return processes.pop(0)
    async def stop(process):
        stopped.append(process)
        transport._processes.discard(process)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    monkeypatch.setattr(transport,'_stop',stop)
    return transport, processes, launches, stopped


@pytest.mark.asyncio
async def test_native_auth_output_private_then_secret_result(setup):
    transport, processes, launches, stopped = setup
    process = Process([frame({'auth':'open visible URL'}),frame({'auth':'paste code'}),frame({'auth_done':True}),frame({'result':{'SessionToken':'secret'}})])
    processes.append(process)
    bridge = Bridge(['native-code'])
    result = await transport.acquire(dict(login=True,method='direct'),{'HOME':'/isolated'},bridge)
    assert result == {'SessionToken':'secret'}
    assert bridge.output == ['open visible URL','paste code']
    assert json.loads(bytes(process.stdin.data).splitlines()[0]) == {'login':True,'method':'direct'}
    assert launches[0][1]['start_new_session'] is True
    assert launches[0][1]['env'] == {'HOME':'/isolated'}
    assert stopped == [process]
    await transport.aclose()


@pytest.mark.asyncio
async def test_export_only_no_interaction(setup):
    transport, processes, _, stopped = setup
    process = Process([frame({'result':{'key':'value'}})])
    processes.append(process)
    assert await transport.acquire(dict(login=False),{}) == {'key':'value'}
    assert stopped == [process]


@pytest.mark.asyncio
@pytest.mark.parametrize('frames,error', [
    ([frame([])],ContractError), ([b'x'*(module.MAX_FRAME+1)],ContractError),
    ([frame({'auth':'not login'})],ContractError), ([frame({'auth_done':True})],ContractError),
    ([frame({'result':{}}),frame({'result':{}})],ContractError),
    ([frame({'result':[]})],ContractError), ([frame({'error':'setup'})],SetupPrerequisiteError),
    ([frame({'error':'authentication'})],ContractError), ([],ContractError),
])
async def test_invalid_worker_protocol_disposes(setup, frames, error):
    transport, processes, _, stopped = setup
    process = Process(frames)
    processes.append(process)
    with pytest.raises(error):
        await transport.acquire(dict(login=False),{})
    assert stopped == [process]


@pytest.mark.asyncio
async def test_child_failure_after_result(setup):
    transport, processes, _, stopped = setup
    process = Process([frame({'result':{}})],code=1)
    processes.append(process)
    with pytest.raises(ContractError):
        await transport.acquire(dict(login=False),{})
    assert stopped == [process]


@pytest.mark.asyncio
async def test_interaction_cancel_stops_worker(setup):
    transport, processes, _, stopped = setup
    process = Process([frame({'auth':'visible URL'})])
    async def blocked():
        await asyncio.Future()
    original = process.stdout.readline
    count = 0
    async def output():
        nonlocal count
        count += 1
        return await original() if count == 1 else await blocked()
    process.stdout.readline = output
    processes.append(process)
    with pytest.raises(InteractionRequiredError):
        await transport.acquire(dict(login=True,method='direct'),{},Bridge([InteractionRequiredError('cancel')]))
    assert stopped == [process]


@pytest.mark.asyncio
async def test_sso_never_sends_terminal_reply(setup):
    transport, processes, _, stopped = setup
    process = Process([frame({'auth':'device code'})])
    original = process.stdout.readline
    count = 0
    async def output():
        nonlocal count
        count += 1
        if count == 1:
            return await original()
        await asyncio.Future()
    process.stdout.readline = output
    processes.append(process)
    with pytest.raises(ContractError,match='does not accept'):
        await transport.acquire(dict(login=True,method='sso'),{},Bridge(['unexpected']))
    assert bytes(process.stdin.data) == frame(dict(login=True,method='sso'))
    assert stopped == [process]


@pytest.mark.asyncio
async def test_closed_and_platform_before_launch(setup, monkeypatch):
    transport, _, launches, _ = setup
    monkeypatch.setattr(module.os,'name','nt')
    with pytest.raises(SetupPrerequisiteError):
        await transport.acquire({}, {})
    monkeypatch.setattr(module.os,'name','posix')
    await transport.aclose()
    with pytest.raises(SessionClosedError):
        await transport.acquire({}, {})
    assert not launches


@pytest.mark.asyncio
async def test_close_wins_launch_registration(setup, monkeypatch):
    transport, _, _, stopped = setup
    process = Process([])
    async def launch(*args, **kwargs):
        transport._closed = True
        return process
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    with pytest.raises(SessionClosedError):
        await transport.acquire({}, {})
    assert stopped == [process]


@pytest.mark.asyncio
async def test_request_and_reply_limits(setup):
    transport, processes, _, stopped = setup
    process = Process([])
    processes.append(process)
    with pytest.raises(ContractError,match='limit'):
        await transport.acquire(dict(login=False,extra='x'*module.MAX_FRAME),{})
    assert stopped == [process]


@pytest.mark.asyncio
async def test_cancellation_reaps_owned_process(setup):
    transport, processes, _, stopped = setup
    process = Process([])
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Future()
    process.stdout.readline = blocked
    processes.append(process)
    task = asyncio.create_task(transport.acquire(dict(login=False),{}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [process]


@pytest.mark.asyncio
async def test_malformed_json_has_fixed_diagnostic(setup):
    transport, processes, _, stopped = setup
    process = Process([b'private-secret-invalid-json\n'])
    processes.append(process)
    with pytest.raises(ContractError) as caught:
        await transport.acquire(dict(login=False),{})
    assert 'private-secret' not in str(caught.value)
    assert stopped == [process]


@pytest.mark.asyncio
async def test_synthetic_real_worker_private_ipc_and_group_cleanup(monkeypatch):
    real_launch = asyncio.create_subprocess_exec
    launches = []
    code = '''import sys,json
config=json.loads(sys.stdin.buffer.readline())
print(json.dumps({'auth':'visible handoff'}),flush=True)
reply=json.loads(sys.stdin.buffer.readline())
assert reply == {'input':'private-code'}
print(json.dumps({'auth_done':True}),flush=True)
print(json.dumps({'result':{'SessionToken':'private-token'}}),flush=True)
'''
    async def launch(*args, **kwargs):
        process = await real_launch(module.sys.executable, '-c', code, **kwargs)
        launches.append(process)
        return process
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    transport = module.LoginTransport(timeout=5)
    bridge = Bridge(['private-code'])
    result = await transport.acquire(dict(login=True,method='direct'),{},bridge)
    assert result == {'SessionToken':'private-token'}
    assert bridge.output == ['visible handoff']
    assert launches[0].returncode is not None
    assert not transport._processes and not transport._stops and not transport._launches
    await transport.aclose()


@pytest.mark.asyncio
async def test_synthetic_worker_cancellation_group_reaped(monkeypatch):
    real_launch = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    processes=[]
    async def launch(*args,**kwargs):
        process = await real_launch(module.sys.executable,'-c','import time; time.sleep(60)',**kwargs)
        processes.append(process)
        launched.set()
        return process
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    transport = module.LoginTransport(timeout=5)
    task = asyncio.create_task(transport.acquire(dict(login=False),{}))
    await launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    assert not transport._processes
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'{"result":{}}',b'{"auth":"visible"}',b'{"auth_done":true}',b'{"error":"setup"}'])
async def test_worker_requires_newline_for_every_frame(setup,raw):
    transport, processes, _, stopped = setup
    process = Process([raw])
    processes.append(process)
    with pytest.raises(ContractError,match='Invalid AWS sign-in response'):
        await transport.acquire(dict(login=False),{})
    assert stopped == [process]


@pytest.mark.asyncio
async def test_native_cache_modes_and_descendant_inherit_private_umask(monkeypatch, tmp_path):
    import os
    real_launch = asyncio.create_subprocess_exec
    launches = []
    code = '''import json,os,pathlib,subprocess,sys
json.loads(sys.stdin.buffer.readline())
root=pathlib.Path(os.environ['HOME'])/'native-cache'
root.mkdir()
(root/'worker.json').write_text('synthetic cache')
subprocess.run([sys.executable,'-c',"from pathlib import Path; import sys; Path(sys.argv[1]).write_text('synthetic descendant cache')",str(root/'descendant.json')],check=True)
print(json.dumps({'result':{}}),flush=True)
'''
    async def launch(*args, **kwargs):
        launches.append(kwargs)
        return await real_launch(module.sys.executable,'-c',code,**kwargs)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    transport = module.LoginTransport(timeout=5)
    environment = {'HOME':str(tmp_path)}
    previous = os.umask(0o022)
    try:
        assert await transport.acquire(dict(login=False),environment) == {}
        parent_umask = os.umask(0o022)
        assert parent_umask == 0o022
        root = tmp_path/'native-cache'
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root/'worker.json').stat().st_mode & 0o777 == 0o600
        assert (root/'descendant.json').stat().st_mode & 0o777 == 0o600
        assert launches[0]['env'] == environment
    finally:
        os.umask(previous)
        await transport.aclose()
