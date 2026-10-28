import asyncio
import json
import sys
from types import SimpleNamespace
class Process(SimpleNamespace):
    __hash__ = object.__hash__
import pytest
from api_agent.llm_providers.bedrock import sdk
from api_agent.llm_providers.bedrock.auth import BedrockAuthSession
from api_agent.providers.contracts import ContractError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.progress import component_timeout


@pytest.fixture
def auth():
    return BedrockAuthSession({'mode': 'access_key', 'access_key': 'synthetic-key', 'secret_key': 'synthetic-secret'})


@pytest.fixture
def fake_process(monkeypatch):
    processes = []
    monkeypatch.setattr(sdk.os, 'killpg', lambda *args: None)
    settings = {'frames': [{'result': {'ok': True}}], 'returncode': 0}
    async def spawn(*args, **kwargs):
        reader = asyncio.StreamReader()
        for value in settings['frames']:
            reader.feed_data(value if isinstance(value, bytes) else json.dumps(value).encode() + b'\n')
        reader.feed_eof()
        writes = []
        async def drain():
            pass
        async def wait():
            process.returncode = settings['returncode']
            return process.returncode
        process = Process(pid=999999, stdin=SimpleNamespace(write=writes.append, drain=drain, close=lambda: None), stdout=reader, returncode=None, wait=wait, terminate=lambda: None, writes=writes, args=args, kwargs=kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    return settings, processes


@pytest.mark.asyncio
async def test_call_scopes_secrets_to_stdin_and_removes_bearer(auth, fake_process, monkeypatch):
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'synthetic-bearer')
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-openai')
    monkeypatch.setenv('AWS_BEDROCK_BASE_URL', 'https://wrong.invalid')
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'unrelated-token')
    monkeypatch.setenv('AWS_SECURITY_TOKEN', 'unrelated-legacy-token')
    transport = sdk.SDKTransport()
    assert await transport.call('completions', {'modelId': 'synthetic'}, auth=auth) == {'ok': True}
    process = fake_process[1][0]
    assert process.args == (sys.executable, str(sdk.Path(sdk.__file__).with_name('worker.py')))
    assert 'OPENAI_API_KEY' not in process.kwargs['env']
    assert 'AWS_BEDROCK_BASE_URL' not in process.kwargs['env']
    assert 'AWS_BEARER_TOKEN_BEDROCK' not in process.kwargs['env']
    assert 'AWS_SESSION_TOKEN' not in process.kwargs['env']
    assert 'AWS_SECURITY_TOKEN' not in process.kwargs['env']
    assert 'synthetic-secret' not in repr(process.args)
    assert json.loads(process.writes[0])['auth']['secret_key'] == 'synthetic-secret'
    assert not transport._processes
    await transport.aclose()
    with pytest.raises(SessionClosedError):
        await transport.call('completions', {}, auth=auth)


@pytest.mark.asyncio
@pytest.mark.parametrize('frames,message', [([b'bad\n'], 'Invalid'), ([[]], 'Invalid'), ([{'error': 'secret'}], 'error'), ([{'error': 'unknown'}], 'execution'), ([{'wrong': {}}], 'Invalid'), ([{'result': []}], 'Invalid'), ([], 'count'), ([{'result': {}}, {'result': {}}], 'count')])
async def test_invalid_worker_frames_are_safe(auth, fake_process, frames, message):
    fake_process[0]['frames'] = frames
    with pytest.raises((ContractError, ProviderAccessError), match=message):
        await sdk.SDKTransport().call('completions', {}, auth=auth)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('http_status', True), ('http_status', 99), ('http_status', 600),
    ('retry_after', True), ('retry_after', -1), ('retry_after', '2'),
])
async def test_worker_rejects_invalid_retry_metadata(auth, fake_process, field, value):
    frame = {'error': 'unavailable', 'execution': 'possibly_executed', field: value}
    fake_process[0]['frames'] = [frame]
    with pytest.raises(ContractError, match='provider error status|worker retry delay'):
        await sdk.SDKTransport().call('completions', {}, auth=auth)


@pytest.mark.asyncio
async def test_worker_preserves_valid_retry_metadata(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'possibly_executed',
                                  'http_status': 503, 'retry_after': 2.5}]
    with pytest.raises(ProviderAccessError) as caught:
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    assert caught.value.http_status == 503
    assert caught.value.retry_after == 2.5
    assert caught.value.execution == 'possibly_executed'


@pytest.mark.asyncio
async def test_nonzero_worker_exit(auth, fake_process):
    fake_process[0]['returncode'] = 1
    with pytest.raises(ContractError, match='worker failed'):
        await sdk.SDKTransport().call('completions', {}, auth=auth)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,payload', [('other', {}), ('completions', []), ('completions', {'text': 'x' * sdk.MAX_FRAME})])
async def test_invalid_requests_do_not_spawn(auth, fake_process, operation, payload):
    with pytest.raises(ContractError):
        await sdk.SDKTransport().call(operation, payload, auth=auth)
    assert fake_process[1] == []


@pytest.mark.asyncio
async def test_stream_yields_events_and_partial_close_terminates(auth, fake_process):
    fake_process[0]['frames'] = [{'event': {'delta': 'one'}}, {'event': {'delta': 'two'}}]
    transport = sdk.SDKTransport()
    stream = transport.stream('completions_stream', {}, auth=auth)
    assert await anext(stream) == {'delta': 'one'}
    await stream.aclose()
    assert not transport._processes


@pytest.mark.asyncio
async def test_stop_handles_already_exited_group(monkeypatch):
    calls = []
    def killpg(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError()
    monkeypatch.setattr(sdk.os, 'killpg', killpg)
    async def wait():
        return 0
    process = Process(pid=99999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    await transport._stop(process)
    assert calls == [(99999, sdk.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_close_drains_active_process(auth, fake_process):
    transport = sdk.SDKTransport()
    stream = transport.stream('completions_stream', {}, auth=auth)
    await anext(stream)
    await transport.aclose()
    assert not transport._processes
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['timeout', 'cancel'])
async def test_real_controlled_subprocess_is_reaped(auth, monkeypatch, action, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    original = asyncio.create_subprocess_exec
    children = []
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, '-c', 'import sys,time; sys.stdin.readline(); time.sleep(60)', **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    transport = sdk.SDKTransport(timeout=60)
    if action == 'timeout':
        with pytest.raises(TimeoutError):
            async with component_timeout('test bounded parent', .1):
                await transport.call('completions', {}, auth=auth)
    else:
        task = asyncio.create_task(transport.call('completions', {}, auth=auth))
        while not children:
            await asyncio.sleep(0)
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert children[0].returncode is not None
    assert not transport._processes


@pytest.mark.asyncio
async def test_spawn_failure_is_safe(auth, monkeypatch):
    async def spawn(*args, **kwargs):
        raise OSError('synthetic-secret')
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(ContractError, match='start') as error:
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_oversized_worker_frame_is_safe(auth, fake_process):
    fake_process[0]['frames'] = [b'x' * (sdk.MAX_FRAME + 1) + b'\n']
    with pytest.raises(ContractError, match='response'):
        await sdk.SDKTransport().call('completions', {}, auth=auth)


@pytest.mark.asyncio
async def test_session_mode_preserves_sdk_chain_environment(fake_process):
    session = BedrockAuthSession({'mode': 'session'})
    assert await sdk.SDKTransport().call('completions', {}, auth=session) == {'ok': True}


@pytest.mark.asyncio
async def test_stream_can_close_from_another_task(auth, fake_process):
    transport = sdk.SDKTransport()
    stream = transport.stream('completions_stream', {}, auth=auth)
    await anext(stream)
    await asyncio.create_task(stream.aclose())
    assert not transport._processes


@pytest.mark.asyncio
async def test_total_stream_budget_includes_consumer_delay(auth, fake_process):
    fake_process[0]['frames'] = [{'event': {}}, {'event': {}}]
    stream = sdk.SDKTransport(timeout=.01).stream('completions_stream', {}, auth=auth)
    await anext(stream)
    await asyncio.sleep(.02)
    with pytest.raises(TimeoutError):
        await anext(stream)


@pytest.mark.asyncio
async def test_unsupported_platform_fails_before_spawn(auth, fake_process, monkeypatch):
    monkeypatch.setattr(sdk.os, 'name', 'nt')
    with pytest.raises(SetupPrerequisiteError, match='POSIX'):
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    assert not fake_process[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['authentication', 'authorization', 'rate_limited', 'unavailable', 'invalid_request', 'unknown', 'setup'])
async def test_worker_error_classification(auth, fake_process, kind):
    fake_process[0]['frames'] = [{'error': kind, 'execution': 'possibly_executed'}]
    error_type = SetupPrerequisiteError if kind == 'setup' else ProviderAccessError
    with pytest.raises(error_type) as error:
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    if kind != 'setup':
        assert error.value.kind == kind
        assert error.value.execution == 'possibly_executed'


def _process_still_running(stat):
    try:
        return stat.read_text().split()[2] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.mark.parametrize('error', [FileNotFoundError(2, 'synthetic missing stat'),
                                   ProcessLookupError(3, 'synthetic exited process')])
def test_process_liveness_probe_accepts_disappearance_during_read(error):
    def read_text():
        raise error
    assert _process_still_running(SimpleNamespace(read_text=read_text)) is False


@pytest.mark.parametrize('state,expected', [('R', True), ('S', True), ('Z', False)])
def test_process_liveness_probe_preserves_running_and_zombie_distinction(state, expected):
    stat = SimpleNamespace(read_text=lambda: f'123 (synthetic) {state} 1 2 3')
    assert _process_still_running(stat) is expected


def test_process_liveness_probe_does_not_hide_other_read_failures():
    def read_text():
        raise PermissionError('synthetic unreadable stat')
    with pytest.raises(PermissionError, match='unreadable stat'):
        _process_still_running(SimpleNamespace(read_text=read_text))


@pytest.mark.asyncio
@pytest.mark.parametrize('parent_exits', [False, True])
async def test_cleanup_terminates_credential_process_descendant(auth, monkeypatch, tmp_path, parent_exits):
    original = asyncio.create_subprocess_exec
    pidfile = tmp_path / 'helper.pid'
    # Helpers stay in the isolated worker group; fake worker can exit first.
    script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); open(sys.argv[1],'w').write(str(p.pid)); " + ("print('{\"result\":{}}', flush=True)" if parent_exits else "time.sleep(60)")
    children = []
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, '-c', script, str(pidfile), **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    transport = sdk.SDKTransport(timeout=.3)
    task = asyncio.create_task(transport.call('completions', {}, auth=auth))
    while not pidfile.exists():
        await asyncio.sleep(.005)
    pid = int(pidfile.read_text())
    results = await asyncio.gather(task, return_exceptions=True)
    if parent_exits:
        assert results == [{}]
    else:
        assert isinstance(results[0], TimeoutError)
    assert children[0].returncode is not None
    stat = __import__('pathlib').Path(f'/proc/{pid}/stat')
    for _ in range(100):
        if not _process_still_running(stat):
            break
        await asyncio.sleep(.005)
    assert not _process_still_running(stat)
    assert not transport._processes


@pytest.mark.asyncio
async def test_source_worker_launch_does_not_need_pythonpath(monkeypatch, tmp_path):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    # Invalid auth fails before importing boto3 or resolving any AWS credentials.
    auth = BedrockAuthSession({'mode': 'unsupported'})
    with pytest.raises(ProviderAccessError) as error:
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    assert error.value.kind == 'unknown'
    assert error.value.execution == 'not_started'


@pytest.mark.asyncio
async def test_expired_budget_rejects_ready_work_without_calling_it():
    called = []
    async def immediate():
        called.append(True)
        return {'event': {}}
    with pytest.raises(TimeoutError, match='deadline expired'):
        await sdk.SDKTransport()._timed(immediate, SimpleNamespace(remaining=0))
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_call', [False, True])
@pytest.mark.parametrize('spawn_fails', [False, True])
async def test_close_waits_for_owned_launch(auth, monkeypatch, fake_process, cancel_call, spawn_fails):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = sdk.asyncio.create_subprocess_exec
    transport = sdk.SDKTransport()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        if spawn_fails:
            raise OSError('synthetic-secret')
        return await original(*args, **kwargs)
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    call = asyncio.create_task(transport.call('completions', {}, auth=auth))
    await entered.wait()
    if cancel_call:
        call.cancel()
    closes = [asyncio.create_task(transport.aclose()) for _ in range(2)]
    await asyncio.sleep(.01)
    remained_pending = all(not close.done() for close in closes)
    release.set()
    await asyncio.gather(*closes)
    assert remained_pending
    assert not transport._processes
    assert all(process.returncode is not None for process in fake_process[1])
    with pytest.raises((SessionClosedError, ContractError, asyncio.CancelledError)) as error:
        await call
    assert 'synthetic-secret' not in str(error.value)
    count = len(fake_process[1])
    with pytest.raises(SessionClosedError):
        await transport.call('completions', {}, auth=auth)
    assert len(fake_process[1]) == count


@pytest.mark.asyncio
async def test_close_during_initial_progress_prevents_launch(auth, fake_process, monkeypatch):
    from contextlib import asynccontextmanager
    original = sdk.component_timeout
    transport = sdk.SDKTransport()
    @asynccontextmanager
    async def closing_progress(*args, **kwargs):
        async with original(*args, **kwargs) as budget:
            yield budget
        await transport.aclose()
    monkeypatch.setattr(sdk, 'component_timeout', closing_progress)
    with pytest.raises(SessionClosedError):
        await transport.call('completions', {}, auth=auth)
    assert not fake_process[1]


@pytest.mark.asyncio
async def test_closed_gate_is_rechecked_before_scheduled_spawn(auth, fake_process, monkeypatch):
    transport = sdk.SDKTransport()
    original = asyncio.create_task
    def create_task(work, *args, **kwargs):
        if work.cr_code.co_name == 'launch':
            transport._closed = True
        return original(work, *args, **kwargs)
    monkeypatch.setattr(sdk.asyncio, 'create_task', create_task)
    with pytest.raises(SessionClosedError):
        await transport.call('completions', {}, auth=auth)
    assert not fake_process[1]
    assert not transport._launches
    await transport.aclose()


@pytest.mark.asyncio
async def test_cancelled_close_still_waits_for_launch_cleanup(auth, fake_process, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = sdk.asyncio.create_subprocess_exec
    transport = sdk.SDKTransport()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(sdk.asyncio, 'create_subprocess_exec', spawn)
    call = asyncio.create_task(transport.call('completions', {}, auth=auth))
    await entered.wait()
    close = asyncio.create_task(transport.aclose())
    await asyncio.sleep(.01)
    close.cancel()
    await asyncio.sleep(.01)
    assert not close.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    with pytest.raises(SessionClosedError):
        await call
    assert all(process.returncode is not None for process in fake_process[1])
    assert not transport._launches
    assert not transport._processes
    await transport.aclose()


@pytest.mark.asyncio
async def test_failed_close_drains_other_owners_and_can_retry(fake_process):
    transport = sdk.SDKTransport()
    calls = {'first': 0, 'second': 0}
    async def first_wait():
        calls['first'] += 1
        if calls['first'] == 1:
            raise OSError('synthetic-private-diagnostic')
        return 0
    async def second_wait():
        calls['second'] += 1
        return 0
    first = Process(pid=999991, wait=first_wait)
    second = Process(pid=999992, wait=second_wait)
    transport._processes.update((first, second))
    with pytest.raises(ContractError, match='cleanup failed') as error:
        await transport.aclose()
    assert 'synthetic-private-diagnostic' not in str(error.value)
    assert calls == {'first': 1, 'second': 1}
    assert transport._processes == {first}
    failed = transport._closing
    await transport.aclose()
    assert calls == {'first': 2, 'second': 1}
    assert not transport._processes
    assert transport._closing is not failed
    succeeded = transport._closing
    await transport.aclose()
    assert transport._closing is succeeded
    assert calls == {'first': 2, 'second': 1}


@pytest.mark.asyncio
async def test_cancelled_drain_retains_owner_and_allows_fresh_retry(fake_process):
    transport = sdk.SDKTransport()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def wait():
        calls.append(True)
        entered.set()
        await release.wait()
        return 0
    process = Process(pid=999993, wait=wait)
    transport._processes.add(process)
    close = asyncio.create_task(transport.aclose())
    await entered.wait()
    cancelled = transport._closing
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert transport._processes == {process}
    release.set()
    await transport.aclose()
    assert transport._closing is not cancelled
    assert not transport._processes
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_acquired_owner_snapshot_is_captured_once(fake_process, monkeypatch):
    calls = []
    values = {'mode': 'temporary', 'access_key': 'key', 'secret_key': 'secret', 'session_token': 'token'}
    class Owner:
        def worker_config(self):
            calls.append('snapshot')
            assert calls == ['acquire', 'snapshot']
            return values
    async def acquire():
        calls.append('acquire')
        return Owner()
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'ambient')
    monkeypatch.setenv('AWS_SECURITY_TOKEN', 'ambient')
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'ambient')
    await sdk.SDKTransport().call('completions', {}, auth=acquire)
    process = fake_process[1][0]
    assert calls == ['acquire', 'snapshot']
    assert json.loads(process.writes[0])['auth'] == values
    assert not {'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN', 'AWS_BEARER_TOKEN_BEDROCK'} & process.kwargs['env'].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,payload', [('other', {}), ('completions', []), ('completions', {'text': 'x' * sdk.MAX_FRAME})])
async def test_request_admission_precedes_acquisition(fake_process, operation, payload):
    calls = []
    async def acquire():
        calls.append(True)
        raise AssertionError('must not acquire')
    with pytest.raises(ContractError):
        await sdk.SDKTransport().call(operation, payload, auth=acquire)
    assert calls == []


@pytest.mark.asyncio
async def test_acquired_snapshot_size_is_bounded(fake_process):
    async def acquire():
        return SimpleNamespace(worker_config=lambda: {'mode': 'temporary', 'session_token': 'x' * sdk.MAX_FRAME})
    with pytest.raises(ContractError, match='limit'):
        await sdk.SDKTransport().call('completions', {}, auth=acquire)
    assert not fake_process[1]


@pytest.mark.asyncio
async def test_acquisition_uses_transport_deadline(fake_process):
    entered = asyncio.Event()
    async def acquire():
        entered.set()
        await asyncio.Event().wait()
    with pytest.raises(TimeoutError):
        await sdk.SDKTransport(timeout=.01).call('completions', {}, auth=acquire)
    assert entered.is_set()
    assert not fake_process[1]


@pytest.mark.asyncio
async def test_close_during_acquisition_prevents_worker_launch(fake_process):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def acquire():
        entered.set()
        await release.wait()
        return SimpleNamespace(worker_config=lambda: {'mode': 'temporary'})
    transport = sdk.SDKTransport()
    call = asyncio.create_task(transport.call('completions', {}, auth=acquire))
    await entered.wait()
    await transport.aclose()
    release.set()
    with pytest.raises(SessionClosedError):
        await call
    assert not fake_process[1]


@pytest.mark.asyncio
async def test_request_newline_counts_toward_frame_limit(fake_process, monkeypatch):
    auth_config = {'mode': 'api_key', 'api_key': 'synthetic'}
    payload = {'text': ''}
    size = len(json.dumps({'operation': 'models', 'payload': payload, 'auth': auth_config}).encode())
    monkeypatch.setattr(sdk, 'MAX_FRAME', size)
    async def auth():
        return SimpleNamespace(worker_config=lambda: auth_config)
    with pytest.raises(ContractError, match='limit'):
        await sdk.SDKTransport().call('models', payload, auth=auth)
    assert not fake_process[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [float('nan'), (1,), {1: 'bad'}])
async def test_non_json_payload_never_acquires(value, fake_process):
    async def auth():
        pytest.fail('Acquired invalid JSON request')
    with pytest.raises(ContractError, match='JSON'):
        await sdk.SDKTransport().call('models', {'value': value}, auth=auth)
    assert not fake_process[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_first', [True, False])
async def test_successful_disposal_is_inert_for_late_other_owner(auth, fake_process, monkeypatch, provider_first):
    signals = []
    monkeypatch.setattr(sdk.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    fake_process[0]['frames'] = [{'event': {'delta': 'one'}}, {'event': {'delta': 'two'}}]
    transport = sdk.SDKTransport()
    stream = transport.stream('completions_stream', {}, auth=auth)
    assert await anext(stream) == {'delta': 'one'}
    process = fake_process[1][0]
    first, second = (transport.aclose, stream.aclose) if provider_first else (stream.aclose, transport.aclose)
    await first()
    assert signals == [(process.pid, sdk.signal.SIGTERM), (process.pid, sdk.signal.SIGKILL)]
    await second()
    await transport.aclose()
    await stream.aclose()
    await transport._stop(process)
    assert len(signals) == 2
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
async def test_concurrent_cleanup_waiter_cancellation_cannot_revoke_owner(monkeypatch):
    transport = sdk.SDKTransport()
    signals, waits = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(sdk.os, 'killpg', lambda pid, sig: signals.append(sig))
    async def wait():
        waits.append(True)
        entered.set()
        await release.wait()
        return 0
    process = Process(pid=999999, wait=wait)
    transport._processes.add(process)
    first = asyncio.create_task(transport._stop(process))
    await entered.wait()
    second = asyncio.create_task(transport._stop(process))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    third = asyncio.create_task(transport._stop(process))
    await asyncio.sleep(0)
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    assert not second.done()
    assert process in transport._processes
    assert not transport._stops[process]['task'].cancelled()
    release.set()
    await second
    assert signals == [sdk.signal.SIGTERM, sdk.signal.SIGKILL]
    assert waits == [True]
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
@pytest.mark.parametrize('gone', [True, False])
async def test_reap_retry_never_resends_completed_or_absent_group_signals(monkeypatch, gone):
    signals, waits = [], []
    transport = sdk.SDKTransport()
    def kill(pid, sig):
        signals.append(sig)
        if gone:
            raise ProcessLookupError()
    monkeypatch.setattr(sdk.os, 'killpg', kill)
    async def wait():
        waits.append(True)
        if len(waits) == 1:
            raise OSError('synthetic-secret')
        return 0
    process = Process(pid=999999, wait=wait)
    transport._processes.add(process)
    with pytest.raises(OSError):
        await transport._stop(process)
    assert process in transport._processes
    expected = [sdk.signal.SIGTERM] if gone else [sdk.signal.SIGTERM, sdk.signal.SIGKILL]
    assert signals == expected
    await transport._stop(process)
    assert signals == expected and len(waits) == 2
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
async def test_signal_failure_retains_only_unfinished_phase_for_retry(monkeypatch):
    transport = sdk.SDKTransport()
    signals = []
    def kill(pid, sig):
        signals.append(sig)
        if len(signals) == 2:
            raise PermissionError('synthetic-secret')
    monkeypatch.setattr(sdk.os, 'killpg', kill)
    async def wait(): return 0
    process = Process(pid=999999, wait=wait)
    transport._processes.add(process)
    with pytest.raises(PermissionError):
        await transport._stop(process)
    assert process in transport._processes
    await transport._stop(process)
    assert signals == [sdk.signal.SIGTERM, sdk.signal.SIGKILL, sdk.signal.SIGKILL]
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
async def test_interrupted_disposal_owner_retries_reap_without_resignaling(monkeypatch):
    transport = sdk.SDKTransport()
    signals, waits = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(sdk.os, 'killpg', lambda pid, sig: signals.append(sig))
    async def wait():
        waits.append(True)
        entered.set()
        await release.wait()
        return 0
    process = Process(pid=999999, wait=wait)
    transport._processes.add(process)
    waiter = asyncio.create_task(transport._stop(process))
    await entered.wait()
    transport._stops[process]['task'].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert process in transport._processes
    release.set()
    await transport._stop(process)
    assert signals == [sdk.signal.SIGTERM, sdk.signal.SIGKILL]
    assert waits == [True, True]
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
async def test_empty_sdk_credentials_preserve_parent_authentication_classification(fake_process):
    from api_agent.llm_providers.bedrock import worker
    frames, calls = [], []
    def session_factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(region_name='us-east-1', get_credentials=lambda: None)
    worker.execute({'timeout': 240, 'operation': 'models', 'payload': {}, 'auth': {'mode': 'session', 'region': 'us-east-1', 'profile': 'synthetic'}},
        frames.append, session_factory=session_factory,
        http_factory=lambda **kw: pytest.fail('HTTP constructed without credentials'))
    assert frames == [{'error': 'authentication', 'execution': 'not_started'}]
    assert calls == [{'region_name': 'us-east-1', 'profile_name': 'synthetic'}]
    fake_process[0]['frames'] = frames
    async def auth():
        return SimpleNamespace(worker_config=lambda: {'mode': 'session', 'region': 'us-east-1', 'profile': 'synthetic'})
    with pytest.raises(ProviderAccessError) as raised:
        await sdk.SDKTransport().call('models', {}, auth=auth)
    assert raised.value.kind == 'authentication'
    assert raised.value.execution == 'not_started'
    assert 'synthetic' not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('parent', [None, .5])
async def test_worker_timeout_uses_remaining_budget(auth, fake_process, parent):
    transport = sdk.SDKTransport()
    assert transport.timeout == 900
    async with component_timeout('Synthetic caller', parent):
        assert await transport.call('responses', {}, auth=auth) == {'ok': True}
    sent = json.loads(fake_process[1][0].writes[0])['timeout']
    assert 0 < sent <= (900 if parent is None else parent)
    if parent is None:
        assert sent > 30
    assert not transport._processes


@pytest.mark.parametrize('value', [True, False, 0, -1, float('inf'), float('nan'), '240', None, 3601, 10**1000, [], {}])
def test_transport_rejects_invalid_deadline_before_auth(value):
    with pytest.raises(ValueError, match='inference timeout'):
        sdk.SDKTransport(timeout=value)


@pytest.mark.asyncio
async def test_expiry_after_launch_never_writes_and_cleans_owner(auth, fake_process, monkeypatch):
    from api_agent.providers.progress import TimeoutBudget
    original = TimeoutBudget.remaining
    monkeypatch.setattr(TimeoutBudget, 'remaining', property(
        lambda budget: 0 if fake_process[1] else original.__get__(budget)))
    transport = sdk.SDKTransport()
    with pytest.raises(TimeoutError, match='deadline expired'):
        await transport.call('responses', {}, auth=auth)
    assert fake_process[1][0].writes == []
    assert not transport._processes


@pytest.mark.asyncio
async def test_wire_timeout_cannot_overflow_frame_limit(auth, fake_process, monkeypatch):
    initial = json.dumps({'operation': 'responses', 'payload': {},
                          'auth': dict(auth.worker_config())}).encode() + b'\n'
    monkeypatch.setattr(sdk, 'MAX_FRAME', len(initial))
    transport = sdk.SDKTransport()
    with pytest.raises(ContractError, match='transport limit'):
        await transport.call('responses', {}, auth=auth)
    assert len(fake_process[1]) == 1
    assert fake_process[1][0].writes == []
    assert not transport._processes
