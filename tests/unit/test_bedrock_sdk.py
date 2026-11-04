import asyncio
import json
import sys
from types import SimpleNamespace
class Process(SimpleNamespace):
    __hash__ = object.__hash__
    async def dispose(self):
        self.dispose_calls = getattr(self, 'dispose_calls', 0) + 1
        self.returncode = await self.wait()
import pytest
import pytest_asyncio
from api_agent.llm_providers.bedrock import sdk
from api_agent.llm_providers.bedrock.auth import BedrockAuthSession
from api_agent.providers.contracts import ContractError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.progress import component_timeout


@pytest.mark.asyncio
async def test_ambiguous_reasoning_error_has_format_guidance(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'unknown', 'execution': 'possibly_executed',
                                  'code': 'reasoning_format_ambiguous'}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError, match='BEDROCK_COMPLETIONS_REASONING_FORMAT') as error:
            await transport.call('completions', {}, auth=auth)
        assert error.value.execution == 'possibly_executed'
    finally:
        await transport.aclose()


@pytest.fixture
def auth():
    return BedrockAuthSession({'mode': 'access_key', 'access_key': 'synthetic-key', 'secret_key': 'synthetic-secret'})


@pytest.fixture
def fake_process(monkeypatch):
    processes = []
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
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    return settings, processes


@pytest.mark.asyncio
async def test_network_failure_has_clear_safe_diagnostic(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'network', 'execution': 'possibly_executed'}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError, match='Bedrock network request failed; connection lost or timed out') as caught:
            await transport.call('models', {}, auth=auth)
        assert caught.value.kind == 'network'
        assert caught.value.execution == 'possibly_executed'
        assert caught.value.usage is None
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_sdk_metadata_is_bounded_and_not_yielded_as_an_inference_result(auth, fake_process):
    from api_agent.workflows.inference_capture import collect_inference
    from api_agent.telemetry.privacy import EmissionBoundary
    fake_process[0]['frames'] = [{'result': {'ok': True}, 'diagnostics': {'version': 1, 'http_status': 200}}]
    transport = sdk.SDKTransport()
    with collect_inference(EmissionBoundary()) as capture:
        try:
            assert await transport.call('responses', {}, auth=auth) == {'ok': True}
            assert capture['transport_diagnostics'][None] == {'version': 1, 'http_status': 200}
        finally:
            await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('frame', [
    {'result': {}, 'diagnostics': {'version': 1, 'body': 'not metadata'}},
    {'event': {}, 'diagnostics': {'version': 1}},
])
async def test_sdk_rejects_unrecognized_metadata_frames(auth, fake_process, frame):
    fake_process[0]['frames'] = [frame]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='Invalid Bedrock worker diagnostics'):
            await transport.call('responses', {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_http_success_contract_rejection_retains_code_usage_and_shape(auth, fake_process):
    from api_agent.workflows.inference_capture import collect_inference, capture_inference
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    diagnostics = {'version':1, 'response_validation':'rejected', 'reasoning_text_characters':0,
                   'finish_reasons':['stop'], 'validation_rule':'tool_finish_consistency',
                   'validation_path':'$.choices[].finish_reason'}
    fake_process[0]['frames'] = [dict(error='unknown', execution='possibly_executed', http_status=200,
        code='tool_finish_mismatch', diagnostics=diagnostics,
        usage={'input_tokens':375, 'output_tokens':25, 'cached_input_tokens':None, 'reasoning_tokens':None})]
    transport = sdk.SDKTransport()
    try:
        with collect_inference(EmissionBoundary(ContentPolicy(True)), retain_raw=True) as capture:
            with pytest.raises(ProviderAccessError, match='response failed contract validation') as caught:
                await transport.call('completions', {}, auth=auth)
            capture_inference('output', {}, 'call', unavailable=True)
        assert caught.value.code == 'tool_finish_mismatch' and caught.value.status == 200
        assert caught.value.usage.input_tokens == 375 and caught.value.usage.output_tokens == 25
        assert caught.value.provider_diagnostics == diagnostics
        assert 'Validation: tool_finish_consistency at $.choices[].finish_reason.' in str(caught.value)
        assert capture['rows'] == [dict(phase='response', content={}, reason='unavailable', diagnostics=diagnostics)]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_native_converse_shape_diagnosis_survives_worker_and_sdk_with_content_disabled(
        auth, fake_process, monkeypatch):
    from api_agent.llm_providers.bedrock import worker
    from test_bedrock_runtime_worker import request as worker_request
    from test_bedrock_converse import response

    monkeypatch.setenv('LOG_CONTENT', 'False')
    raw = response()
    raw['output']['message']['content'] = [{'reasoningContent': {'private-canary': 'secret'}}]
    frames = []
    worker.execute(worker_request('converse', {'endpoint': 'runtime', 'prefix': '/converse',
        'body': {'modelId': 'operator.model-v2:1', 'messages': []}}), frames.append,
        boto_client_factory=lambda *args: SimpleNamespace(converse=lambda **kwargs: raw, close=lambda: None))
    assert frames[0]['diagnostics']['validation_rule'] == 'reasoning_content'
    assert frames[0]['diagnostics']['validation_path'] == '$.output.message.content[].reasoningContent'
    assert frames[0]['execution'] == 'possibly_executed'
    assert frames[0]['usage']['input_tokens'] == 2
    assert 'private-canary' not in str(frames)

    fake_process[0]['frames'] = frames
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('converse', {}, auth=auth)
        assert caught.value.execution == 'possibly_executed'
        assert caught.value.usage.input_tokens == 2
        assert 'Validation: reasoning_content at $.output.message.content[].reasoningContent.' in str(caught.value)
        assert 'private-canary' not in str(caught.value)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_sdk_keeps_generic_message_when_shape_path_is_unavailable(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'unknown', 'execution': 'possibly_executed',
        'code': 'invalid_response', 'diagnostics': {'version': 1, 'response_validation': 'rejected'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('converse', {}, auth=auth)
        assert 'Validation:' not in str(caught.value)
        assert caught.value.provider_diagnostics == {'version': 1, 'response_validation': 'rejected'}
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(code='provider body'), dict(code=[]), dict(error='network'),
    dict(execution='not_started'), dict(diagnostics={'version':1,'text':'secret'}), dict(code=None)])
async def test_contract_failure_metadata_is_not_an_unrestricted_error_channel(auth, fake_process, change):
    fake_process[0]['frames'] = [dict(error='unknown', execution='possibly_executed',
        code='invalid_response', diagnostics={'version':1}) | change]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='Invalid Bedrock worker'):
            await transport.call('completions', {}, auth=auth)
    finally:
        await transport.aclose()


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
async def test_stop_delegates_disposal_to_the_registered_owner():
    async def wait(): return 0
    process = Process(pid=99999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    await transport._stop(process)
    assert process.dispose_calls == 1 and process.returncode == 0
    assert not transport._processes



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
    original = sdk.spawn_worker
    children = []
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, '-c', 'import sys,time; sys.stdin.readline(); time.sleep(60)', **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
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
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
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
@pytest.mark.parametrize('startup_delay', [0, .5])
async def test_cleanup_terminates_credential_process_descendant(auth, monkeypatch, tmp_path, parent_exits, startup_delay):
    import os
    import signal
    from api_agent.testing.processes import _group_running
    from api_agent.testing import processes

    pidfile = tmp_path / 'helper.pid'
    # Prepare the synthetic worker before starting the inference deadline. The
    # request on stdin releases it only after SDKTransport owns its process group.
    script = ("import os,subprocess,sys,time; from pathlib import Path; "
              f"time.sleep({startup_delay}); "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
              "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "pending=sys.argv[1]+'.pending'; Path(pending).write_text(str(p.pid)); "
              "os.replace(pending,sys.argv[1]); sys.stdin.readline(); ") + (
                  "print('{\"result\":{}}', flush=True)" if parent_exits else "time.sleep(60)")
    child = await sdk.spawn_worker(sys.executable, '-c', script, str(pidfile),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    transport = sdk.SDKTransport(timeout=2)
    try:
        async with component_timeout('Synthetic credential helper startup', 10):
            while not pidfile.exists():
                assert child.returncode is None, 'Synthetic worker exited before publishing helper readiness'
                await asyncio.sleep(.005)
        assert int(pidfile.read_text()) > 0
        assert _group_running(child.pid), 'Synthetic helper must be live before cleanup'
        original_signal = processes.signal_owned_group
        signals = []
        def reserved_signal(process, signum):
            assert process is child.process
            assert process.returncode is None, 'Worker PID reservation was released before group signaling'
            processes.observe_exit(process)
            signals.append(signum)
            return original_signal(process, signum)
        monkeypatch.setattr(processes, 'signal_owned_group', reserved_signal)

        async def spawn(*args, **kwargs):
            return child
        monkeypatch.setattr(sdk, 'spawn_worker', spawn)
        results = await asyncio.gather(transport.call('completions', {}, auth=auth), return_exceptions=True)
        if parent_exits:
            assert results == [{}]
        else:
            assert isinstance(results[0], TimeoutError)
        assert child.returncode is not None
        async with component_timeout('Synthetic credential helper disposal', 5):
            while _group_running(child.pid):
                await asyncio.sleep(.005)
        assert not transport._processes
        assert signals
    finally:
        try:
            await transport.aclose()
        finally:
            # Before admission, this test owns the still-unreaped worker group.
            if child.returncode is None:
                await child.dispose()


@pytest.mark.asyncio
async def test_source_worker_launch_does_not_need_pythonpath(monkeypatch, tmp_path):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.setenv('PYTHONNOUSERSITE', '1')
    # Use a clean interpreter, not the test environment's installed api-agent
    # wheel, which can accidentally hide missing source-worker imports.
    monkeypatch.setattr(sdk.sys, 'executable', sys._base_executable)
    monkeypatch.chdir(tmp_path)
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
    original = sdk.spawn_worker
    transport = sdk.SDKTransport()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        if spawn_fails:
            raise OSError('synthetic-secret')
        return await original(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
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
    original = sdk.spawn_worker
    transport = sdk.SDKTransport()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
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
    fake_process[0]['frames'] = [{'event': {'delta': 'one'}}, {'event': {'delta': 'two'}}]
    transport = sdk.SDKTransport()
    stream = transport.stream('completions_stream', {}, auth=auth)
    assert await anext(stream) == {'delta': 'one'}
    process = fake_process[1][0]
    first, second = (transport.aclose, stream.aclose) if provider_first else (stream.aclose, transport.aclose)
    await first()
    assert process.dispose_calls == 1
    await second()
    await transport.aclose()
    await stream.aclose()
    await transport._stop(process)
    assert process.dispose_calls == 1
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
async def test_concurrent_cleanup_waiter_cancellation_cannot_revoke_owner(monkeypatch):
    transport = sdk.SDKTransport()
    waits = []
    entered, release = asyncio.Event(), asyncio.Event()
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
    assert process.dispose_calls == len(waits)
    assert waits == [True]
    assert not transport._processes and not transport._stops


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [OSError('controlled IO failure'), PermissionError('controlled refusal'), TimeoutError('controlled incomplete disposal')])
async def test_failed_disposal_retries_the_same_owner(failure):
    calls = []
    async def wait():
        calls.append(True)
        if len(calls) == 1: raise failure
        return 0
    process = Process(pid=999999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    with pytest.raises(type(failure)):
        await transport._stop(process)
    assert process in transport._processes and transport._cleanup_pending()
    await transport._stop(process)
    assert calls == [True, True] and process.dispose_calls == 2
    assert not transport._processes and not transport._stops



@pytest.mark.asyncio
async def test_refused_owner_disposal_stays_pending_until_successful_retry():
    ready = False
    async def wait():
        if not ready: raise PermissionError('controlled refusal')
        return 0
    process = Process(pid=999999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    for _ in range(2):
        with pytest.raises(PermissionError): await transport._stop(process)
        assert process in transport._processes and transport._cleanup_pending()
    ready = True
    await transport._stop(process)
    assert process.dispose_calls == 3 and not transport._processes



@pytest.mark.asyncio
async def test_unowned_handle_is_rejected_without_retiring_its_receipt():
    process = SimpleNamespace()
    transport = sdk.SDKTransport()
    # A foreign handle has no authority to claim disposal.
    class Foreign: pass
    process = Foreign()
    transport._processes.add(process)
    with pytest.raises(RuntimeError, match='cannot reserve'):
        await transport._stop(process)
    assert process in transport._processes and transport._cleanup_pending()



@pytest.mark.asyncio
async def test_normal_retirement_allows_concurrent_work_until_a_failure_is_known(auth, fake_process):
    from api_agent.providers.control import CleanupPendingError
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait():
        entered.set()
        await release.wait()
        raise PermissionError('controlled refusal')
    process = Process(pid=999999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    stopping = asyncio.create_task(transport._stop(process))
    await entered.wait()
    assert await transport.call('models', {}, auth=auth) == {'ok': True}
    assert len(fake_process[1]) == 1
    release.set()
    with pytest.raises(PermissionError): await stopping
    with pytest.raises(CleanupPendingError):
        await transport.call('models', {}, auth=auth)
    assert len(fake_process[1]) == 1



@pytest.mark.asyncio
async def test_new_worker_is_blocked_if_cleanup_fails_during_auth(auth, fake_process):
    from api_agent.providers.control import CleanupPendingError
    ready = False
    async def wait():
        if not ready: raise PermissionError('controlled refusal')
        return 0
    process = Process(pid=999999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    async def acquire():
        with pytest.raises(PermissionError): await transport._stop(process)
        return await auth()
    with pytest.raises(CleanupPendingError):
        await transport.call('models', {}, auth=acquire)
    assert not fake_process[1]
    ready = True
    await transport.aclose()



@pytest.mark.asyncio
async def test_owner_completion_releases_admission_receipt():
    async def wait(): return 0
    process = Process(pid=999999, wait=wait)
    transport = sdk.SDKTransport()
    transport._processes.add(process)
    await transport._stop(process)
    assert not transport._cleanup_pending()
    assert process.dispose_calls == 1



@pytest.mark.asyncio
async def test_interrupted_disposal_owner_retries_reap_without_resignaling(monkeypatch):
    transport = sdk.SDKTransport()
    waits = []
    entered, release = asyncio.Event(), asyncio.Event()
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
    assert transport._cleanup_pending()
    release.set()
    await transport._stop(process)
    assert process.dispose_calls == len(waits)
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


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', [b'{bad}\n', b'{}\n', b'[""]\n', pytest.param(b'"' + b'x' * 33000, id='oversized-handoff')])
async def test_worker_authority_channel_rejects_invalid_handoffs(auth, fake_process, monkeypatch, wire):
    import os
    import socket
    original_spawn = sdk.spawn_worker
    tasks = []
    async def spawn(*args, **kwargs):
        fd = os.dup(kwargs['pass_fds'][0])
        async def send():
            channel = socket.socket(fileno=fd)
            channel.setblocking(False)
            try:
                try:
                    await asyncio.get_running_loop().sock_sendall(channel, wire)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                channel.close()
        tasks.append(asyncio.create_task(send()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    with pytest.raises(ContractError, match='authority handoff'):
        await sdk.SDKTransport().call('completions', {}, auth=auth, join=lambda values: None)
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize('union', [[], ['different'], ['synthetic-key', ''], ['synthetic-key', '\x00' * 10000]])
async def test_parent_rejects_invalid_joined_authority_and_releases_lease(auth, fake_process, monkeypatch, union):
    import os
    import socket
    original_spawn = sdk.spawn_worker
    tasks = []
    async def spawn(*args, **kwargs):
        fd = os.dup(kwargs['pass_fds'][0])
        async def send():
            channel = socket.socket(fileno=fd)
            channel.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_sendall(channel, b'["synthetic-key"]\n')
            finally:
                channel.close()
        tasks.append(asyncio.create_task(send()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    lease = SimpleNamespace(release=__import__('unittest.mock', fromlist=['Mock']).Mock())
    async def join(values):
        assert values == ['synthetic-key']
        return union, lease
    with pytest.raises(ContractError, match='authority handoff'):
        await sdk.SDKTransport().call('completions', {}, auth=auth, join=join)
    lease.release.assert_called_once()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_unjoined_dispatch_callback_runs_before_result(auth, fake_process):
    events = []
    result = await sdk.SDKTransport().call('completions', {}, auth=auth, on_dispatch=lambda: events.append('dispatch'))
    assert result == {'ok': True} and events == ['dispatch']


@pytest.mark.asyncio
async def test_worker_error_preserves_valid_usage_without_releasing_unjoined_response(auth, fake_process):
    usage = {'input_tokens': 4, 'output_tokens': 2, 'cached_input_tokens': None, 'reasoning_tokens': None}
    fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'possibly_executed', 'usage': usage}]
    with pytest.raises(ProviderAccessError) as caught:
        await sdk.SDKTransport().call('completions', {}, auth=auth)
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (4, 2)
    fake_process[0]['frames'][0]['provider_response'] = {'message': 'untrusted'}
    with pytest.raises(ContractError, match='Invalid Bedrock worker response'):
        await sdk.SDKTransport().call('completions', {}, auth=auth)


@pytest.mark.asyncio
async def test_joined_authority_without_dispatch_callback_still_requires_worker_ack(auth, fake_process, monkeypatch):
    import os
    import socket
    original_spawn = sdk.spawn_worker
    tasks, observed = [], []
    async def spawn(*args, **kwargs):
        fd = os.dup(kwargs['pass_fds'][0])
        async def exchange():
            channel = socket.socket(fileno=fd)
            channel.setblocking(False)
            loop = asyncio.get_running_loop()
            try:
                await loop.sock_sendall(channel, b'["synthetic-key"]\n')
                data = bytearray()
                while not data.endswith(b'\n'):
                    data.extend(await loop.sock_recv(channel, 1024))
                observed.append(json.loads(data))
                await loop.sock_sendall(channel, b'1')
                observed.append(await loop.sock_recv(channel, 1))
            finally:
                channel.close()
        tasks.append(asyncio.create_task(exchange()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    async def join(values): return None
    result = await sdk.SDKTransport().call('completions', {}, auth=auth, join=join)
    await asyncio.gather(*tasks)
    assert result == {'ok': True}
    assert observed == [['synthetic-key'], b'1']


@pytest.mark.asyncio
@pytest.mark.parametrize('code,expected_error,expected_body', [
    ('tool_finish_mismatch', ProviderAccessError, {'content':'safe rejected body'}),
    ('response_safety_rejected', ContractError, None),
])
async def test_joined_failure_body_cannot_contradict_safety_rejection(auth, fake_process, monkeypatch,
                                                                   code, expected_error, expected_body):
    import os
    import socket
    fake_process[0]['frames'] = [dict(error='unknown', execution='possibly_executed', code=code,
                                    provider_response={'content':'safe rejected body'})]
    original_spawn = sdk.spawn_worker
    tasks = []
    async def spawn(*args, **kwargs):
        descriptor = os.dup(kwargs['pass_fds'][0])
        async def exchange():
            channel = socket.socket(fileno=descriptor)
            channel.setblocking(False)
            loop = asyncio.get_running_loop()
            try:
                await loop.sock_sendall(channel, b'["synthetic-key"]\n')
                data = bytearray()
                while not data.endswith(b'\n'):
                    data.extend(await loop.sock_recv(channel, 1024))
                await loop.sock_sendall(channel, b'1')
                assert await loop.sock_recv(channel, 1) == b'1'
            finally:
                channel.close()
        tasks.append(asyncio.create_task(exchange()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    async def join(values):
        return None
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(expected_error) as caught:
            await transport.call('completions', {}, auth=auth, join=join)
        assert getattr(caught.value, 'provider_response', None) == expected_body
    finally:
        await transport.aclose()
        await asyncio.gather(*tasks)


@pytest_asyncio.fixture
async def output_limit_join(fake_process, monkeypatch):
    import os
    import socket
    original_spawn = sdk.spawn_worker
    tasks = []
    async def spawn(*args, **kwargs):
        channel = socket.socket(fileno=os.dup(kwargs['pass_fds'][0]))
        channel.setblocking(False)
        async def exchange():
            loop = asyncio.get_running_loop()
            try:
                await loop.sock_sendall(channel, b'["synthetic-key"]\n')
                data = bytearray()
                while not data.endswith(b'\n'):
                    data.extend(await loop.sock_recv(channel, 1024))
                await loop.sock_sendall(channel, b'1')
                await loop.sock_recv(channel, 1)
            finally:
                channel.close()
        tasks.append(asyncio.create_task(exchange()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    async def join(values):
        return None
    yield join
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_explicit_output_rejection_becomes_typed_safe_fact(auth, fake_process, output_limit_join):
    from api_agent.providers.output_limits import OutputLimitRejected
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'not_started',
                                 'http_status': 400, 'maximum_output_tokens': 8192}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(OutputLimitRejected) as caught:
            await transport.call('responses', {}, auth=auth, join=output_limit_join)
        assert caught.value.maximum_tokens == 8192
        assert caught.value.execution == 'not_started'
        assert caught.value.usage is None
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_output_rejection_retains_pending_cleanup_and_authority_lease(
        auth, fake_process, output_limit_join, monkeypatch):
    from unittest.mock import Mock
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.output_limits import OutputLimitRejected
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'not_started',
                                  'http_status': 400, 'maximum_output_tokens': 8192}]
    lease = SimpleNamespace(release=Mock())
    async def join(values):
        await output_limit_join(values)
        return values, lease
    absent = False
    signals = []
    original_dispose = Process.dispose
    async def dispose(process):
        signals.append('dispose')
        if not absent: raise PermissionError('synthetic permission denied')
        await original_dispose(process)
    monkeypatch.setattr(Process, 'dispose', dispose)
    transport = sdk.SDKTransport()
    with pytest.raises(OutputLimitRejected) as caught:
        await transport.call('responses', {}, auth=auth, join=join)
    process = fake_process[1][0]
    assert caught.value.maximum_tokens == 8192
    assert caught.value.retained_owners == (transport,)
    assert isinstance(caught.value.cleanup_error, CleanupPendingError)
    assert process in transport._processes
    lease.release.assert_not_called()
    with pytest.raises(CleanupPendingError):
        await transport.call('responses', {}, auth=auth, join=join)
    assert len(fake_process[1]) == 1
    absent = True
    await transport.aclose()
    lease.release.assert_called_once()
    assert not transport._processes and not transport._leases
    assert signals == ['dispose', 'dispose']


@pytest.mark.asyncio
async def test_late_authority_lease_after_concurrent_disposal_is_retained_on_release_failure(
        auth, fake_process, monkeypatch):
    import os
    import socket
    from unittest.mock import Mock
    from api_agent.providers.control import CleanupPendingError
    transport = sdk.SDKTransport()
    lease = SimpleNamespace(release=Mock(side_effect=[PermissionError('lease denied'), None]))
    original_spawn = sdk.spawn_worker
    tasks = []
    async def spawn(*args, **kwargs):
        channel = socket.socket(fileno=os.dup(kwargs['pass_fds'][0]))
        channel.setblocking(False)
        async def send():
            try:
                await asyncio.get_running_loop().sock_sendall(channel, b'["synthetic-key"]\n')
            finally:
                channel.close()
        tasks.append(asyncio.create_task(send()))
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(sdk, 'spawn_worker', spawn)
    async def join(values):
        await transport._stop(fake_process[1][0])
        return values, lease
    with pytest.raises(SessionClosedError) as caught:
        await transport.call('completions', {}, auth=auth, join=join)
    assert isinstance(caught.value.cleanup_error, CleanupPendingError)
    assert not transport._processes and transport._leases
    await transport.aclose()
    assert not transport._leases
    assert lease.release.call_count == 2
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_join_may_return_no_authority_lease(auth, fake_process, output_limit_join):
    async def join(values):
        return values, None
    transport = sdk.SDKTransport()
    assert await transport.call('completions', {}, auth=auth, join=join) == {'ok': True}
    assert not transport._leases


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_error', [False, True])
async def test_cancellation_after_completed_stop_is_not_reported_as_pending(
        auth, fake_process, monkeypatch, primary_error):
    if primary_error:
        fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'not_started'}]
    original_finish = sdk.finish_cleanup
    calls = 0
    async def cancel_after_stop(awaitable):
        nonlocal calls
        calls += 1
        result = await original_finish(awaitable)
        if calls == 2:
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(sdk, 'finish_cleanup', cancel_after_stop)
    transport = sdk.SDKTransport()
    with pytest.raises(asyncio.CancelledError) as caught:
        await transport.call('completions', {}, auth=auth)
    assert not hasattr(caught.value, 'cleanup_error')
    assert not transport._processes and not transport._leases


@pytest.mark.asyncio
async def test_successful_result_with_denied_cleanup_reports_pending(auth, fake_process, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    absent = False
    original_dispose = Process.dispose
    async def dispose(process):
        if not absent: raise PermissionError('denied')
        await original_dispose(process)
    monkeypatch.setattr(Process, 'dispose', dispose)
    transport = sdk.SDKTransport()
    with pytest.raises(CleanupPendingError) as caught:
        await transport.call('completions', {}, auth=auth)
    assert caught.value.owners == (transport,)
    assert fake_process[1][0] in transport._processes
    absent = True
    await transport.aclose()
    assert not transport._processes


@pytest.mark.asyncio
async def test_cancellation_with_retained_worker_keeps_pending_receipt(auth, fake_process, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    transport = sdk.SDKTransport()
    original_stop = transport._stop
    async def cancelled_stop(process):
        raise asyncio.CancelledError()
    monkeypatch.setattr(transport, '_stop', cancelled_stop)
    with pytest.raises(asyncio.CancelledError) as caught:
        await transport.call('completions', {}, auth=auth)
    assert caught.value.retained_owners == (transport,)
    assert isinstance(caught.value.cleanup_error, CleanupPendingError)
    assert fake_process[1][0] in transport._processes
    monkeypatch.setattr(transport, '_stop', original_stop)
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'maximum_output_tokens': True}, {'maximum_output_tokens': 0},
    {'maximum_output_tokens': 1048577}, {'http_status': 500},
    {'execution': 'possibly_executed'}, {'usage': {}}, {'provider_response': {}},
])
async def test_output_rejection_marker_rejects_unproven_fact(auth, fake_process, change, output_limit_join):
    fake_process[0]['frames'] = [dict({'error': 'invalid_request', 'execution': 'not_started',
        'http_status': 400, 'maximum_output_tokens': 8192}, **change)]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError):
            await transport.call('responses', {}, auth=auth, join=output_limit_join)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_output_rejection_marker_requires_joined_authority(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'invalid_request', 'execution': 'not_started',
        'http_status': 400, 'maximum_output_tokens': 8192}]
    with pytest.raises(ContractError, match='output limit rejection'):
        await sdk.SDKTransport().call('responses', {}, auth=auth)


@pytest.mark.asyncio
async def test_output_rejection_marker_after_event_is_not_preinference(auth, fake_process, output_limit_join):
    fake_process[0]['frames'] = [{'event': {'type': 'started'}},
        {'error': 'invalid_request', 'execution': 'not_started',
         'http_status': 400, 'maximum_output_tokens': 8192}]
    with pytest.raises(ContractError, match='output limit rejection'):
        await sdk.SDKTransport().call('responses_stream', {}, auth=auth, join=output_limit_join)


@pytest.mark.asyncio
async def test_exclusive_validation_negotiates_once_and_caches(auth, fake_process, output_limit_join):
    from botocore.exceptions import ClientError
    from unittest.mock import AsyncMock
    from api_agent.llm_providers.bedrock import worker
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.providers.shared_setup import SharedSetupOwner
    from test_output_limits import request as generation_request
    from test_bedrock_runtime_worker import request as worker_request
    from test_bedrock_output_limits import EXCLUSIVE_MESSAGE

    frames, dispatched = [], []
    def converse(**kwargs):
        raise ClientError({'Error': {'Code': 'ValidationException', 'Message': EXCLUSIVE_MESSAGE},
                           'ResponseMetadata': {'HTTPStatusCode': 400}}, 'Converse')
    worker.execute(worker_request('converse', {'endpoint': 'runtime', 'prefix': '/converse',
        'body': {'modelId': 'operator.model-v2:1', 'messages': [], 'inferenceConfig': {'maxTokens': 200000}}}),
        frames.append, boto_client_factory=lambda *args: SimpleNamespace(converse=converse, close=lambda: None),
        join=lambda values, arguments: values)
    assert frames == [{'error': 'invalid_request', 'execution': 'not_started',
                       'http_status': 400, 'maximum_output_tokens': 127999}]
    fake_process[0]['frames'] = frames
    transport = sdk.SDKTransport()
    async def call(callback):
        return await callback(auth=auth)
    async def describe(*, auth):
        return ()
    async def complete(effective, *, auth):
        dispatched.append(effective.payload.generation.max_output_tokens)
        if len(dispatched) == 1:
            await transport.call('converse', {}, auth=auth, join=output_limit_join)
        return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(1, 2))
    owner = SharedSetupOwner('op', 'identity', lambda credentials: SimpleNamespace(call=call, aclose=AsyncMock()),
                             describe=describe, complete=complete, supports_output_limit=True)
    try:
        await owner.complete(generation_request())
        await owner.complete(generation_request())
    finally:
        await owner.aclose()
        await transport.aclose()
    assert dispatched == [200000, 127999, 127999]


@pytest.mark.asyncio
async def test_catalog_worker_scrub_counts_are_logged_without_values(auth, fake_process, caplog):
    import logging
    diagnostics = dict(version=1, scrub_empty_strings=2, scrub_omitted_members=1, scrub_omitted_events=0)
    fake_process[0]['frames'] = [{'result': {'data': []}, 'diagnostics': diagnostics}]
    transport = sdk.SDKTransport()
    try:
        with caplog.at_level(logging.INFO, logger='api_agent.scrubbing'):
            assert await transport.call('models', {}, auth=auth) == {'data': []}
        assert 'operation=bedrock_model_discovery empty_strings=2 omitted_members=1 omitted_events=0' in caplog.text
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation, diagnostics', [
    ('completions', dict(version=1, scrub_empty_strings=1, scrub_omitted_members=0, scrub_omitted_events=0)),
    ('models', dict(version=1, scrub_empty_strings=1)),
])
async def test_invalid_scrub_count_routes_or_incomplete_counts_reject(auth, fake_process, operation, diagnostics):
    fake_process[0]['frames'] = [{'result': {'data': []}, 'diagnostics': diagnostics}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='Invalid Bedrock scrub diagnostics'):
            await transport.call(operation, {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('sequence_fields', [{}, {'stream_sequence': 12}])
async def test_stream_failure_details_reach_application_with_optional_content_disabled(auth, fake_process, monkeypatch, sequence_fields):
    monkeypatch.setenv('LOG_CONTENT', 'False')
    fake_process[0]['frames'] = [{'error': 'unknown', 'execution': 'possibly_executed',
        'code': 'invalid_response', 'http_status': 200,
        'diagnostics': {'version': 1, 'response_validation': 'rejected',
            'validation_rule': 'stream_state', 'validation_path': '$',
            'stream_event': 'response.output_item.done', **sequence_fields,
            'validation_location': 'api_agent.llm_providers.bedrock.mantle:600'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('responses_stream', {}, auth=auth)
        message = str(caught.value)
        assert 'failed contract validation' in message
        assert 'Stream event: response.output_item.done.' in message
        assert ('Sequence: 12.' in message) == bool(sequence_fields)
        assert 'Validation location: api_agent.llm_providers.bedrock.mantle:600.' in message
        assert caught.value.status == 200
        assert caught.value.execution == 'possibly_executed'
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_redacted_native_stream_error_retains_retry_category(auth, fake_process):
    from api_agent.providers.inference_retry import retryable
    fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'possibly_executed',
        'http_status': 424, 'aws_error_rejected': True, 'model_stream_error': True}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await transport.call('converse_stream', {}, auth=auth)
        assert caught.value.code == 'ModelStreamErrorException'
        assert retryable(caught.value)
        assert 'additional AWS diagnostic withheld' in str(caught.value)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'model_stream_error': False}, {'error': 'unknown'},
    {'http_status': 400}, {'execution': 'not_started'}])
async def test_native_stream_category_rejects_conflicting_error_facts(auth, fake_process, change):
    fake_process[0]['frames'] = [dict(error='unavailable', execution='possibly_executed',
        http_status=424, model_stream_error=True, **{}) | change]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='native stream error category'):
            await transport.call('converse_stream', {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_native_stream_category_cannot_be_reused_on_another_operation(auth, fake_process):
    fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'possibly_executed',
                                'http_status': 424, 'model_stream_error': True}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='native stream error category'):
            await transport.call('responses_stream', {}, auth=auth)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_native_stream_category_cannot_contradict_admitted_aws_code(auth, fake_process, output_limit_join):
    fake_process[0]['frames'] = [{'error': 'unavailable', 'execution': 'possibly_executed',
        'http_status': 424, 'model_stream_error': True,
        'aws_error': {'code': 'OtherException', 'message': 'Synthetic service failure'}}]
    transport = sdk.SDKTransport()
    try:
        with pytest.raises(ContractError, match='native stream error category'):
            await transport.call('converse_stream', {}, auth=auth, join=output_limit_join)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('login', [False, True])
async def test_failed_pipe_attachment_blocks_transport_until_retained_worker_retires(auth, monkeypatch, login):
    from api_agent.llm_providers.bedrock import owned_worker, login_transport
    from api_agent.providers.control import CleanupPendingError
    transport = login_transport.LoginTransport(timeout=2) if login else sdk.SDKTransport(timeout=2)
    async def operation():
        if login:
            return await transport.acquire({'method': 'direct', 'login': False}, {})
        return await transport.call('models', {}, auth=auth)
    def denied(process): raise PermissionError('controlled disposal refusal')
    async def broken(*args, **kwargs): raise OSError('controlled pipe attachment failure')
    try:
        with monkeypatch.context() as failure:
            failure.setattr(owned_worker, 'finalize', denied)
            failure.setattr(asyncio.get_running_loop(), 'connect_write_pipe', broken)
            with pytest.raises(ContractError, match='start|worker response') as caught:
                await operation()
            assert caught.value.cleanup_error.owners == (transport,)
            owner, = transport._processes
            assert isinstance(owner, owned_worker.OwnedWorker)
            assert owner.returncode is None
            with pytest.raises(CleanupPendingError):
                await operation()
    finally:
        await transport.aclose()
    assert owner.returncode is not None
    assert not transport._processes


@pytest.mark.asyncio
async def test_raw_async_child_has_no_numeric_group_signal_authority(monkeypatch):
    import os
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    await child.wait()
    claims = []
    async def claimed_disposal(): claims.append(True)
    child.dispose = claimed_disposal
    def forbidden(*args): pytest.fail('reaped async PID was signalled')
    monkeypatch.setattr(sdk, 'os', SimpleNamespace(**(vars(os) | {'killpg': forbidden})))
    transport = sdk.SDKTransport()
    transport._processes.add(child)
    try:
        with pytest.raises(RuntimeError, match='cannot reserve'):
            await transport._stop(child)
        assert child in transport._processes
        assert not claims
    finally:
        transport._processes.discard(child)
        transport._stops.pop(child, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('login', [False, True])
async def test_success_cannot_be_released_after_owned_disposal_expires_budget(auth, monkeypatch, login):
    from api_agent.llm_providers.bedrock import owned_worker, login_transport
    from api_agent.providers import progress
    child = await owned_worker.spawn_worker(sys.executable, '-c',
        'import sys; print("ready", flush=True); sys.stdin.readline(); print(\'{"result":{}}\', flush=True)',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    assert await asyncio.wait_for(child.stdout.readline(), 5) == b'ready\n'
    now = [0]
    monkeypatch.setattr(progress, '_clock', lambda: now[0])
    async def prepared(*args, **kwargs): return child
    original = owned_worker.finalize
    def expire_after_disposal(process):
        result = original(process)
        now[0] = 3
        return result
    module = login_transport if login else sdk
    monkeypatch.setattr(module, 'spawn_worker', prepared)
    monkeypatch.setattr(owned_worker, 'finalize', expire_after_disposal)
    transport = module.LoginTransport(timeout=2) if login else sdk.SDKTransport(timeout=2)
    try:
        with pytest.raises(TimeoutError, match='deadline expired'):
            if login:
                await asyncio.wait_for(transport.acquire({'method': 'direct', 'login': False}, {}), 10)
            else:
                await asyncio.wait_for(transport.call('completions', {}, auth=auth), 10)
        assert child.returncode == 0
        assert not transport._processes
    finally:
        await transport.aclose()
        await child.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('login', [False, True])
async def test_foreign_spawn_failure_metadata_cannot_claim_worker_ownership(auth, monkeypatch, login):
    from api_agent.llm_providers.bedrock import login_transport
    error = RuntimeError('controlled failed launch')
    error.retained_owners = (object(),)
    async def fail(*args, **kwargs): raise error
    module = login_transport if login else sdk
    monkeypatch.setattr(module, 'spawn_worker', fail)
    transport = login_transport.LoginTransport() if login else sdk.SDKTransport()
    with pytest.raises(RuntimeError) as caught:
        if login:
            await transport.acquire({'method': 'direct', 'login': False}, {})
        else:
            await transport.call('models', {}, auth=auth)
    assert caught.value is error
    assert not transport._processes


@pytest.mark.asyncio
async def test_login_rechecks_pending_owner_at_launch_admission(monkeypatch):
    from unittest.mock import Mock
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport
    from api_agent.providers.control import CleanupPendingError
    transport = LoginTransport()
    monkeypatch.setattr(transport, '_cleanup_pending', Mock(side_effect=[False, True]))
    with pytest.raises(CleanupPendingError):
        await transport.acquire({'method': 'direct', 'login': False}, {})
    assert not transport._processes


@pytest.mark.asyncio
@pytest.mark.parametrize('refused', [False, True])
async def test_repeated_login_cancellation_cannot_skip_worker_disposal(monkeypatch, refused):
    from api_agent.llm_providers.bedrock import owned_worker, login_transport
    child = await owned_worker.spawn_worker(sys.executable, '-c',
        'import sys,time; print(\'{"auth":"ready"}\', flush=True); sys.stdin.readline(); time.sleep(60)',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    prompt, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def prepared(*args, **kwargs): return child
    async def ready(): pass
    async def write(text):
        assert text == 'ready'
        prompt.set()
    async def read():
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
    interaction = SimpleNamespace(check_ready=ready, write=write, read=read)
    transport = login_transport.LoginTransport(timeout=10)
    pending = None
    try:
        with monkeypatch.context() as controls:
            controls.setattr(login_transport, 'spawn_worker', prepared)
            if refused:
                def denied(process): raise PermissionError('controlled disposal refusal')
                controls.setattr(owned_worker, 'finalize', denied)
            pending = asyncio.create_task(transport.acquire({'method': 'direct', 'login': True}, {}, interaction))
            await asyncio.wait_for(prompt.wait(), 5)
            pending.cancel()
            await asyncio.wait_for(draining.wait(), 5)
            pending.cancel()
            await asyncio.sleep(.02)
            assert not pending.done(), 'worker ownership escaped while input draining remained incomplete'
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            if refused:
                assert caught.value.retained_owners == (transport,)
                assert caught.value.cleanup_error.owners == (transport,)
                assert child in transport._processes and child.returncode is None
            else:
                assert child.returncode is not None and not transport._processes
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await transport.aclose()
        await child.dispose()
    assert child.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('classified', [False, True])
@pytest.mark.parametrize('refused', [False, True])
@pytest.mark.parametrize('interrupt', [False, True])
async def test_authority_channel_close_failure_cannot_skip_registered_worker(auth, monkeypatch, classified, refused, interrupt):
    import socket
    from api_agent.llm_providers.bedrock import owned_worker
    from api_agent.providers.control import CleanupPendingError
    pair = socket.socketpair
    channels, children = [], []
    class Channel:
        def __init__(self, raw, parent):
            self.raw = raw
            self.parent = parent
            self.attempts = 0
        def __getattr__(self, name): return getattr(self.raw, name)
        def close(self):
            self.attempts += 1
            self.raw.close()
            if self.parent and interrupt:
                raise asyncio.CancelledError()
            if self.parent or not classified:
                raise OSError('private controlled close detail')
    def allocate():
        result = tuple(Channel(raw, index == 0) for index, raw in enumerate(pair()))
        channels.extend(result)
        return result
    async def prepared(*args, **kwargs):
        descriptor = kwargs['pass_fds'][0]
        code = ('import socket,sys,time; sys.stdin.readline(); '
                f'channel=socket.socket(fileno={descriptor}); '
                "channel.sendall(b'[]\\n'); channel.recv(1024); time.sleep(60)")
        child = await owned_worker.spawn_worker(sys.executable, '-c', code, **kwargs)
        children.append(child)
        return child
    async def join(values):
        assert classified and values == []
        raise ProviderAccessError('authentication', 'Controlled classified primary failure')
    transport = sdk.SDKTransport(timeout=10)
    try:
        with monkeypatch.context() as controls:
            controls.setattr(sdk, 'socket', SimpleNamespace(**(vars(socket) | {'socketpair': allocate})))
            controls.setattr(sdk, 'spawn_worker', prepared)
            if refused:
                def denied(process): raise PermissionError('controlled disposal refusal')
                controls.setattr(owned_worker, 'finalize', denied)
            expected = asyncio.CancelledError if interrupt else ProviderAccessError if classified else ContractError
            with pytest.raises(expected) as caught:
                await transport.call('models', {}, auth=auth, join=join)
            assert len(channels) == 2 and all(channel.attempts == 1 for channel in channels)
            assert 'private controlled close detail' not in str(caught.value)
            if classified and not interrupt:
                assert caught.value.kind == 'authentication'
                assert isinstance(caught.value.channel_cleanup_error, ContractError)
            if refused:
                assert caught.value.cleanup_error.owners == (transport,)
                assert children[0] in transport._processes
                with pytest.raises(CleanupPendingError):
                    await transport.call('models', {}, auth=auth)
            else:
                assert children[0].returncode is not None and not transport._processes
    finally:
        await transport.aclose()
        for child in children: await child.dispose()
    assert all(channel.raw.fileno() == -1 for channel in channels)
    assert children[0].returncode is not None


@pytest.mark.parametrize('failure', [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()])
def test_authority_channel_close_preserves_interrupt_and_attempts_remaining_channels(failure):
    calls = []
    def interrupted():
        calls.append('first')
        raise failure
    def closed(): calls.append('second')
    with pytest.raises(type(failure)) as caught:
        sdk._close_authority_channels([SimpleNamespace(close=interrupted), SimpleNamespace(close=closed)], None)
    assert caught.value is failure
    assert calls == ['first', 'second']


@pytest.mark.asyncio
@pytest.mark.parametrize('allocation_fails', [False, True])
async def test_authority_setup_failure_retires_every_allocated_channel_before_spawn(auth, monkeypatch, allocation_fails):
    import socket
    pair = socket.socketpair
    channels = []
    class Channel:
        def __init__(self, raw):
            self.raw = raw
            self.closes = 0
        def setblocking(self, value): raise OSError('private controlled setup detail')
        def close(self):
            self.closes += 1
            self.raw.close()
    def allocate():
        if allocation_fails: raise OSError('private controlled setup detail')
        result = tuple(Channel(raw) for raw in pair())
        channels.extend(result)
        return result
    async def forbidden(*args, **kwargs): pytest.fail('worker started after authority setup failure')
    monkeypatch.setattr(sdk, 'socket', SimpleNamespace(**(vars(socket) | {'socketpair': allocate})))
    monkeypatch.setattr(sdk, 'spawn_worker', forbidden)
    transport = sdk.SDKTransport()
    with pytest.raises(ContractError, match='prepare Bedrock authority') as caught:
        await transport.call('models', {}, auth=auth, join=forbidden)
    assert 'private controlled setup detail' not in str(caught.value)
    assert len(channels) == (0 if allocation_fails else 2)
    assert all(channel.closes == 1 and channel.raw.fileno() == -1 for channel in channels)
    assert not transport._processes


@pytest.mark.parametrize('control_first', [False, True])
@pytest.mark.parametrize('classified', [False, True])
def test_channel_cleanup_interrupt_takes_precedence_over_ordinary_failures(control_first, classified):
    interruption = asyncio.CancelledError()
    ordinary = OSError('private ordinary close failure')
    failures = [interruption, ordinary] if control_first else [ordinary, interruption]
    calls = []
    def channel(index):
        def close():
            calls.append(index)
            raise failures[index]
        return SimpleNamespace(close=close)
    primary = ProviderAccessError('authentication', 'Controlled primary') if classified else None
    with pytest.raises(asyncio.CancelledError) as caught:
        sdk._close_authority_channels([channel(0), channel(1)], primary)
    assert caught.value is interruption
    assert calls == [0, 1]


def test_channel_cleanup_keeps_first_interrupt_while_attempting_later_channels():
    interruption = KeyboardInterrupt()
    def first(): raise interruption
    def second(): raise asyncio.CancelledError()
    with pytest.raises(KeyboardInterrupt) as caught:
        sdk._close_authority_channels([SimpleNamespace(close=first), SimpleNamespace(close=second)], None)
    assert caught.value is interruption
