"""Real subprocess ownership and bounded worker protocol checks."""
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from api_agent.providers import bounded_schema as subject
from api_agent.providers.progress import report_timeout_sync


def worker(tmp_path,monkeypatch,body):
    path=tmp_path/'synthetic_worker.py'
    path.write_text(body)
    monkeypatch.setattr(subject,'_WORKER',path)
    real=subject.subprocess.Popen
    processes=[]
    def launch(*args,**kwargs):
        process=real(*args,**kwargs)
        processes.append((process,args,kwargs))
        return process
    monkeypatch.setattr(subject.subprocess,'Popen',launch)
    return processes


@pytest.mark.parametrize('body',[
    'import time;time.sleep(10)',
    'import sys,time;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();time.sleep(10)',
])
def test_startup_and_active_timeout_kill_and_reap_actual_worker(tmp_path,monkeypatch,body):
    processes=worker(tmp_path,monkeypatch,body)
    monkeypatch.setattr(subject,'_STARTUP_SECONDS',0.1)
    monkeypatch.setattr(subject,'_VALIDATION_SECONDS',0.1)
    assert not subject.isolated_valid_schema({}, {})
    process,args,kwargs=processes[0]
    assert process.returncode is not None
    assert process.stdin.closed and process.stdout.closed
    assert kwargs['env'] == {'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    assert args[0] == [sys.executable,'-I',str(subject._WORKER)]


@pytest.mark.parametrize('answer',[b'',b'X',b'R',b'R0',b'R11',b'R1extra'])
def test_malformed_missing_extra_or_false_worker_output_rejects(tmp_path,monkeypatch,answer):
    processes=worker(tmp_path,monkeypatch,'import sys;sys.stdout.buffer.write('+repr(answer)+');sys.stdout.buffer.flush()')
    assert not subject.isolated_valid_schema({}, {})
    assert processes[0][0].returncode is not None


def test_success_worker_never_inherits_invocation_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNTHETIC_INVOCATION_SECRET','never-inherit')
    body='import sys,os;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();sys.stdin.buffer.read();sys.stdout.buffer.write(b"1" if "SYNTHETIC_INVOCATION_SECRET" not in os.environ else b"0")'
    processes=worker(tmp_path,monkeypatch,body)
    assert subject.isolated_valid_schema({}, {})
    assert processes[0][0].returncode == 0


def test_parent_interruption_kills_and_reaps_actual_worker(tmp_path,monkeypatch):
    processes=worker(tmp_path,monkeypatch,'import time;time.sleep(10)')
    monkeypatch.setattr(subject.queue.Queue,'get',lambda *args,**kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        subject.isolated_valid_schema({}, {})
    assert processes[0][0].returncode is not None
    assert processes[0][0].stdin.closed and processes[0][0].stdout.closed


def test_enclosing_expired_budget_rejects_without_spawning(monkeypatch):
    monkeypatch.setattr(subject.subprocess,'Popen',lambda *args,**kwargs: pytest.fail('must not spawn'))
    with report_timeout_sync('Synthetic enclosing validation',0.01):
        time.sleep(0.02)
        assert not subject.isolated_valid_schema({}, {})


@pytest.mark.parametrize('value',[{'x':'x'*2097152},{'x':float('nan')},{'x':object()}])
def test_input_is_bounded_before_spawn(monkeypatch,value):
    monkeypatch.setattr(subject.subprocess,'Popen',lambda *args,**kwargs: pytest.fail('must not spawn'))
    assert not subject.isolated_valid_schema({},value)


def test_spawn_failure_is_safe_rejection(monkeypatch):
    def fail(*args,**kwargs):
        raise OSError('synthetic failure')
    monkeypatch.setattr(subject.subprocess,'Popen',fail)
    assert not subject.isolated_valid_schema({}, {})


@pytest.mark.parametrize('raw,expected',[
    (b'[{"type":"object"},{}]',b'R1'),
    (b'[{"type":"integer"},"bad"]',b'R0'),
    (b'bad',b'R0'),
    (b'[{"$ref":"https://example.invalid/schema"},{}]',b'R0'),
    (b'x'*2097153,b'R0'),
])
def test_standalone_worker_protocol_is_fixed_and_no_network(monkeypatch,raw,expected):
    import io
    import runpy
    import api_agent.providers.schema_validation_worker
    from types import SimpleNamespace
    output=io.BytesIO()
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys,'stdout',SimpleNamespace(buffer=output))
    runpy.run_path(str(Path(subject.__file__).with_name('schema_validation_worker.py')),run_name='__main__')
    assert output.getvalue() == expected


@pytest.mark.parametrize('fault',['read','write','kill_race','close'])
def test_pipe_failure_or_process_exit_race_still_reaps_actual_worker(tmp_path,monkeypatch,fault):
    processes=worker(tmp_path,monkeypatch,'import sys,time;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();sys.stdin.buffer.read();time.sleep(10)')
    previous=subject.subprocess.Popen
    class Pipe:
        def __init__(self,pipe):
            self.pipe=pipe
        def read(self,*args):
            if fault == 'read':
                raise OSError('synthetic read failure')
            return self.pipe.read(*args)
        def write(self,*args):
            if fault == 'write':
                raise OSError('synthetic write failure')
            return self.pipe.write(*args)
        def close(self):
            self.pipe.close()
            if fault == 'close':
                raise OSError('synthetic close failure')
        @property
        def closed(self):
            return self.pipe.closed
    def launch(*args,**kwargs):
        process=previous(*args,**kwargs)
        process.stdout=Pipe(process.stdout)
        process.stdin=Pipe(process.stdin)
        if fault == 'kill_race':
            original=process.kill
            def raced():
                original()
                raise ProcessLookupError()
            process.kill=raced
        return process
    monkeypatch.setattr(subject.subprocess,'Popen',launch)
    monkeypatch.setattr(subject,'_STARTUP_SECONDS',0.1)
    monkeypatch.setattr(subject,'_VALIDATION_SECONDS',0.1)
    assert not subject.isolated_valid_schema({}, {})
    assert processes[0][0].returncode is not None


def async_worker(tmp_path,monkeypatch,body):
    import asyncio
    path=tmp_path/'synthetic_async_worker.py'
    path.write_text(body)
    monkeypatch.setattr(subject,'_WORKER',path)
    real=asyncio.create_subprocess_exec
    processes=[]
    async def launch(*args,**kwargs):
        process=await real(*args,**kwargs)
        processes.append((process,args,kwargs))
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    return processes


@pytest.mark.parametrize('answer',[b'',b'X',b'R',b'R0',b'R11',b'R1extra',b'R1'])
def test_async_worker_protocol_has_bounded_output_and_reaps(tmp_path,monkeypatch,answer):
    import asyncio
    processes=async_worker(tmp_path,monkeypatch,'import sys;sys.stdout.buffer.write('+repr(answer[:1])+');sys.stdout.buffer.flush();sys.stdin.buffer.read();sys.stdout.buffer.write('+repr(answer[1:])+');sys.stdout.buffer.flush()')
    monkeypatch.setattr(subject,'_STARTUP_SECONDS',0.1)
    assert asyncio.run(subject.async_isolated_valid_schema({},{})) == (answer == b'R1')
    process,args,kwargs=processes[0]
    assert process.returncode is not None
    assert kwargs['env'] == {'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    assert kwargs['limit'] == 16


@pytest.mark.parametrize('ready',[False,True])
def test_async_worker_startup_and_active_timeout_kill_and_reap(tmp_path,monkeypatch,ready):
    import asyncio
    body='import time;time.sleep(10)'
    if ready:
        body='import sys,time;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();sys.stdin.buffer.read();time.sleep(10)'
    processes=async_worker(tmp_path,monkeypatch,body)
    monkeypatch.setattr(subject,'_STARTUP_SECONDS',0.1)
    monkeypatch.setattr(subject,'_VALIDATION_SECONDS',0.1)
    assert not asyncio.run(subject.async_isolated_valid_schema({},{}))
    assert processes[0][0].returncode is not None


@pytest.mark.parametrize('ready',[False,True])
def test_async_cancellation_reaps_actual_worker_and_keeps_sibling_tasks_responsive(tmp_path,monkeypatch,ready):
    import asyncio
    body='import time;time.sleep(10)'
    if ready:
        body='import sys,time;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();sys.stdin.buffer.read();time.sleep(10)'
    processes=async_worker(tmp_path,monkeypatch,body)
    async def scenario():
        task=asyncio.create_task(subject.async_isolated_valid_schema({},{}))
        while not processes:
            await asyncio.sleep(.001)
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0][0].returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('value',[{'x':'x'*2097152},{'x':float('nan')},{'x':object()}])
def test_async_worker_input_is_bounded_before_spawn(monkeypatch,value):
    import asyncio
    monkeypatch.setattr(asyncio,'create_subprocess_exec',lambda *args,**kwargs: pytest.fail('must not spawn'))
    assert not asyncio.run(subject.async_isolated_valid_schema({},value))


def test_async_worker_expired_parent_does_not_spawn(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio,'create_subprocess_exec',lambda *args,**kwargs: pytest.fail('must not spawn'))
    async def scenario():
        with report_timeout_sync('Synthetic enclosing validation',0.01):
            await asyncio.sleep(.02)
            assert not await subject.async_isolated_valid_schema({}, {})
    asyncio.run(scenario())


def test_async_worker_spawn_failure_rejects(monkeypatch):
    import asyncio
    async def fail(*args,**kwargs):
        raise OSError('synthetic spawn error')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',fail)
    assert not asyncio.run(subject.async_isolated_valid_schema({}, {}))


@pytest.mark.parametrize('kind',['shared','shared_settle','loaded_complete','loaded_stream'])
def test_provider_close_owns_and_reaps_request_admission_worker(tmp_path,monkeypatch,kind):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    from api_agent.providers.loading import LoadedProvider
    from api_agent.providers.contracts import ProviderCapabilities
    from test_bounded_inference_contract import request
    processes=async_worker(tmp_path,monkeypatch,'import sys,time;sys.stdout.buffer.write(b"R");sys.stdout.buffer.flush();sys.stdin.buffer.read();time.sleep(10)')
    async def scenario():
        provider=SimpleNamespace(capabilities=ProviderCapabilities('llm',('complete','stream')),
            asetup=AsyncMock(),acomplete=AsyncMock(),astream=AsyncMock(),aclose=AsyncMock())
        if kind.startswith('shared'):
            owner=SharedSetupOwner('operation','identity',lambda credentials:LLMSessionOwner(provider),complete=provider.acomplete)
            operation=owner.complete(request())
        else:
            owner=LoadedProvider(provider,'synthetic-worker-owner')
            view=owner.require('llm','stream' if kind == 'loaded_stream' else 'complete')
            operation=anext(view.astream(request())) if kind == 'loaded_stream' else view.acomplete(request())
        task=asyncio.create_task(operation)
        while not processes:
            await asyncio.sleep(.001)
        await owner.settle_execution() if kind == 'shared_settle' else await owner.aclose()
        await asyncio.gather(task,return_exceptions=True)
        if kind == 'shared_settle':
            await owner.aclose()
        assert task.cancelled()
        assert processes[0][0].returncode is not None
        provider.acomplete.assert_not_awaited()
        provider.astream.assert_not_called()
    asyncio.run(scenario())


def test_async_cleanup_handles_actual_worker_exit_race(tmp_path,monkeypatch):
    import asyncio
    processes=async_worker(tmp_path,monkeypatch,'import time;time.sleep(10)')
    previous=asyncio.create_subprocess_exec
    async def launch(*args,**kwargs):
        process=await previous(*args,**kwargs)
        original=process.kill
        def raced():
            original()
            raise ProcessLookupError()
        process.kill=raced
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    monkeypatch.setattr(subject,'_STARTUP_SECONDS',.05)
    assert not asyncio.run(subject.async_isolated_valid_schema({},{}))
    assert processes[0][0].returncode is not None
