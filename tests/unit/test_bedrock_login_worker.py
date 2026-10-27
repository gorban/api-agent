import io
import json
import sys
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock import login_worker as module


def tuple_json(**changes):
    return json.dumps(dict(Version=1, AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', Expiration='2999-01-01T00:00:00Z', **changes)).encode()


@pytest.mark.parametrize('raw', [b'{}', b'[]\n', b'x\n', b'x'*(module.MAX_FRAME+1)])
def test_bad_request(raw):
    with pytest.raises(ValueError):
        module.read_frame(io.BytesIO(raw))


def test_request_and_emit(monkeypatch):
    assert module.read_frame(io.BytesIO(b'{"key":1}\n')) == {'key':1}
    out = SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(sys, 'stdout', out)
    module.emit({'safe':'value'})
    assert json.loads(out.buffer.getvalue()) == {'safe':'value'}
    with pytest.raises(ValueError):
        module.emit({'value':'x'*module.MAX_FRAME})


def test_temporary_tuple():
    value, expiry = module.credentials(tuple_json())
    assert value == dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token')
    assert expiry > 30000000000


@pytest.mark.parametrize('raw', [b'[]', b'{}', b'{"Version":true}', b'{"Version":2}',
    b'{"Version":1,"AccessKeyId":" "}',
    b'{"Version":1,"AccessKeyId":"key","SecretAccessKey":"secret"}',
    b'{"Version":1,"AccessKeyId":"key","SecretAccessKey":"secret","SessionToken":"token","Expiration":"2999-01-01"}',
    b'{"Version":1,"AccessKeyId":"key","SecretAccessKey":"secret","SessionToken":"token","Expiration":"2000-01-01T00:00:00Z"}'])
def test_invalid_material(raw):
    with pytest.raises((ValueError, KeyError)):
        module.credentials(raw)


@pytest.mark.parametrize('principal,arn,account,ok', [
    ('arn:aws:iam::123456789012:user/example','arn:aws:iam::123456789012:user/example','123456789012',True),
    ('arn:aws:sts::123456789012:assumed-role/Example','arn:aws:sts::123456789012:assumed-role/Example/session','123456789012',True),
    ('arn:aws:sts::123456789012:assumed-role/Example','arn:aws:sts::123456789012:assumed-role/Other/session','123456789012',False),
    ('arn:aws:iam::123456789012:user/example','arn:aws:iam::123456789012:user/example','999999999999',False),
])
def test_exact_tuple_identity(monkeypatch, principal, arn, account, ok):
    calls = []
    closed = []
    def client(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(get_caller_identity=lambda:dict(Account=account, Arn=arn), close=lambda:closed.append(True))
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, 'botocore.config', SimpleNamespace(Config=lambda **kwargs:kwargs))
    action = lambda:module.verify_identity(dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token'), dict(region='us-east-1', account='123456789012', principal=principal))
    if ok:
        action()
    else:
        with pytest.raises(ValueError):
            action()
    assert calls[0][1]['aws_session_token'] == 'token'
    assert closed == [True]


@pytest.mark.parametrize('method,login', [('direct',True),('sso',True),('direct',False)])
def test_commands_and_export_separation(monkeypatch, method, login):
    calls, frames, verified = [], [], []
    monkeypatch.setattr(module.shutil, 'which', lambda name:'/synthetic/aws')
    def cli(argv, env, *, interactive=False, accept_input=True):
        calls.append((argv, interactive))
        if '--version' in argv:
            return b'aws-cli/2.32.0 Python/3'
        return b'authorization stream' if interactive else tuple_json()
    monkeypatch.setattr(module, 'cli', cli)
    monkeypatch.setattr(module, 'emit', frames.append)
    monkeypatch.setattr(module, 'verify_identity', lambda material,config:verified.append(material))
    module.run(dict(profile='chosen',region='us-east-1',method=method,login=login))
    assert calls[-1][0][-4:] == ['configure','export-credentials','--format','process']
    if login:
        assert frames[0] == {'auth_done':True}
        assert calls[1][1]
        assert ('--remote' if method == 'direct' else '--use-device-code') in calls[1][0]
    assert frames[-1]['result']['SessionToken'] == 'token'
    assert verified[0]['SecretAccessKey'] == 'secret'


@pytest.mark.parametrize('version', [None,b'aws-cli/2.31.9',b'unknown',b'aws-cli/3.0.0'])
def test_prerequisite(monkeypatch, version):
    monkeypatch.setattr(module.shutil, 'which', lambda name:None if version is None else '/synthetic/aws')
    monkeypatch.setattr(module, 'cli', lambda *args:version)
    with pytest.raises(ImportError):
        module.run({})


@pytest.mark.parametrize('failure,error', [(ImportError,'setup'),(ValueError,'authentication')])
def test_main_safe_errors(monkeypatch, failure, error):
    frames = []
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'{}\n')))
    def run(config):
        raise failure('private detail')
    monkeypatch.setattr(module,'run',run)
    monkeypatch.setattr(module,'emit',frames.append)
    module.main()
    expected = {'error': 'setup'} if error == 'setup' else dict(error='acquisition', stage='worker_protocol', kind='unknown', reason='unknown')
    assert frames == [expected]


def test_main_success(monkeypatch):
    received = []
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'{}\n')))
    monkeypatch.setattr(module,'run',received.append)
    module.main()
    assert received == [{}]


class Pipe:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
    def fileno(self):
        return 10
    def write(self, value):
        self.data.extend(value)
    def flush(self):
        pass
    def close(self):
        self.closed = True


@pytest.fixture
def native_cli(monkeypatch):
    def prepare(events, *, interactive=False, status=0):
        incoming, out = Pipe(), Pipe()
        stdin = Pipe() if interactive else None
        state = dict(alive=True)
        def wait():
            state['alive'] = False
            return status
        process = SimpleNamespace(stdout=out,stdin=stdin,wait=wait,poll=lambda:None if state['alive'] else status,kill=lambda:state.update(alive=False))
        monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:process)
        monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=incoming))
        reads=[]
        class Selector:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def register(self,*args):pass
            def select(self):
                kind, chunk = events.pop(0)
                reads.append(chunk)
                return [(SimpleNamespace(data=kind,fileobj=incoming),None)]
        monkeypatch.setattr(module.selectors,'DefaultSelector',Selector)
        monkeypatch.setattr(module.os,'read',lambda *args:reads.pop(0))
        frames=[]
        monkeypatch.setattr(module,'emit',frames.append)
        return process, frames
    return prepare


def test_cli_secret_export_is_never_forwarded(native_cli):
    process, frames = native_cli([('output',b'secret-export'),('output',b'')])
    assert module.cli(['synthetic'],{}) == b'secret-export'
    assert frames == []
    assert process.stdout.closed


@pytest.mark.parametrize('chunks', [[b'{"input":"code"}\n'],[b'{"input":"code',b'"}\n']])
def test_cli_private_replies_chunked(native_cli, chunks):
    events = [('reply',chunk) for chunk in chunks]+[('output',b'visible URL'),('output',b'')]
    process, frames = native_cli(events,interactive=True)
    assert module.cli(['synthetic'],{},interactive=True) == b'visible URL'
    assert bytes(process.stdin.data) == b'code\n'
    assert frames == [{'auth':'visible URL'}]
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.parametrize('reply', [b'',b'{"input":2}\n',b'{"input":"bad\\nvalue"}\n',b'{"input":"bad\\rvalue"}\n',b'{"other":"value"}\n',b'x'*(module.MAX_FRAME+1)])
def test_cli_rejects_invalid_reply_and_reaps(native_cli,reply):
    process, _ = native_cli([('reply',reply)],interactive=True)
    with pytest.raises(ValueError):
        module.cli(['synthetic'],{},interactive=True)
    assert process.stdin.closed and process.stdout.closed
    assert process.poll() is not None


@pytest.mark.parametrize('output,status',[(b'x'*(module.MAX_OUTPUT+1),0),(b'',1)])
def test_cli_rejects_output_limit_or_child_failure(native_cli,output,status):
    process, frames = native_cli([('output',output)],status=status)
    with pytest.raises(module.CLIExecutionFailure if status else ValueError):
        module.cli(['synthetic'],{})
    assert frames == []
    assert process.stdout.closed
    assert process.poll() is not None


def test_script_entrypoint_without_native_execution(monkeypatch):
    import runpy
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{}\n')))
    output = SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setattr(module.shutil, 'which', lambda name: None)
    runpy.run_path(module.__file__, run_name='__main__')
    assert json.loads(output.buffer.getvalue()) == {'error':'setup'}


def test_device_handoff_closes_native_stdin(monkeypatch):
    out = Pipe()
    process = SimpleNamespace(stdout=out, stdin=None, wait=lambda:0, poll=lambda:0)
    calls=[]
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:(calls.append(kwargs) or process))
    chunks=iter([b'device URL',b''])
    monkeypatch.setattr(module.os,'read',lambda *args:next(chunks))
    class Selector:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def register(self,stream,event,kind):
            assert stream is out and kind == 'output'
        def select(self):return [(SimpleNamespace(data='output'),None)]
    monkeypatch.setattr(module.selectors,'DefaultSelector',Selector)
    frames=[]
    monkeypatch.setattr(module,'emit',frames.append)
    assert module.cli(['synthetic'],{},interactive=True,accept_input=False) == b'device URL'
    assert calls[0]['stdin'] == module.subprocess.DEVNULL
    assert frames == [{'auth':'device URL'}]


@pytest.mark.asyncio
async def test_coalesced_config_and_early_reply_are_not_prefetched():
    import asyncio
    script = '''import os,sys
from api_agent.llm_providers.bedrock import login_worker as worker
def run(config):
    native=[sys.executable,'-c',"print('visible',flush=True); value=input(); print('accepted:'+value,flush=True)"]
    worker.emit({'result':worker.cli(native,dict(os.environ),interactive=True).decode()})
worker.run=run
worker.main()
'''
    process = await asyncio.create_subprocess_exec(sys.executable,'-c',script,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        process.stdin.write(b'{}\n{"input":"synthetic-code"}\n')
        await process.stdin.drain()
        frames=[]
        async with asyncio.timeout(2):
            while True:
                value = json.loads(await process.stdout.readline())
                frames.append(value)
                if 'result' in value:
                    break
            assert await process.wait() == 0
        assert frames[-1]['result'] == 'visible\naccepted:synthetic-code\n'
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
