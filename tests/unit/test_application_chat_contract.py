"""Application transport uses authored public values and controlled providers."""
import asyncio
import json
from uuid import uuid4

import pytest
from django.test import RequestFactory

from api_agent.api import Runtime, create_application
from api_agent.api.chat_contract import completion, catalog, frame
from api_agent.api.studio import StudioBackend
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, StreamEvent
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.sessions import LLMSessionOwner

ORIGIN = 'http://127.0.0.1:8080'
NONCE = 'x' * 32


def request(body=None, method='POST'):
    create_application(Runtime('example'))
    return RequestFactory().generic(method, '/', data=json.dumps(body).encode() if body is not None else b'',
        content_type='application/json', headers={'Origin': ORIGIN, 'X-API-Agent-Session': NONCE}, REMOTE_ADDR='127.0.0.1')


def payload(**changes):
    return {'session_id': str(uuid4()), 'messages': [{'role': 'user', 'content': 'hello'}], **changes}


def backend(**changes):
    async def describe(*, auth):
        return (AvailableSelection('example', display_name='Example'),)
    async def complete(value, *, auth, context):
        return LLMResult(Message('assistant', (TextContent('answer'),)))
    args = dict(owner=LLMSessionOwner(object()), describe=describe, complete=complete,
        policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'example'}), origin=ORIGIN, nonce=NONCE, scrub=lambda value: value)
    return StudioBackend(**(args | changes))


def test_catalog_maps_application_values_and_preserves_omission():
    values = (AvailableSelection('example'), AvailableSelection('other', reasoning_efforts=()))
    result = catalog(values, 'example', None)
    assert result['default'] == 'example' and result['reasoning_default'] is None
    assert result['models'][0]['label'] == 'example'
    assert 'reasoning_efforts' not in result['models'][0]
    assert result['models'][1]['reasoning_efforts'] == []
    assert 'selected_llm' not in result['models'][0]


@pytest.mark.parametrize('effort', [None, '', 'maximum', 1])
def test_explicit_invalid_effort_is_rejected(effort):
    with pytest.raises(ContractError):
        completion(payload(reasoning_effort=effort), 'example', None)


def test_omitted_options_defer_and_explicit_options_survive():
    session_id, value = completion(payload(session_id=None), 'example', None)
    assert value.request_id == session_id and value.selected_llm == 'example'
    assert value.payload.generation.reasoning_effort is None
    _, explicit = completion(payload(reasoning_effort='off'), 'example', 'high')
    assert explicit.payload.generation.reasoning_effort == 'off'


@pytest.mark.parametrize('changes', [dict(session_id='bad'), dict(extra=True), dict(messages=[{'role':'user','text':'hello'}])])
def test_invalid_app_shape_is_rejected(changes):
    with pytest.raises(ContractError):
        completion(payload(**changes), 'example', None)


def test_sse_names_and_single_chunk_completion():
    async def run():
        bridge = backend()
        body = payload()
        response = await bridge.handle(request(body), 'chat_stream')
        assert response.streaming and response.status_code == 200
        chunks = [part.decode() async for part in response.streaming_content]
        assert [part.splitlines()[0] for part in chunks] == ['event: session', 'event: token', 'event: response_complete', 'event: result', 'event: done']
        result = json.loads(chunks[-2].splitlines()[1][6:])
        assert result['response_text'] == 'answer' and result['session_id'] == body['session_id']
        status = await bridge.handle(request({'session_id': body['session_id']}), 'chat_cancel')
        assert json.loads(status.content)['status'] == 'completed'
        await bridge.aclose()
    asyncio.run(run())


def test_json_and_unknown_lifecycle_status():
    async def run():
        bridge = backend()
        answer = await bridge.handle(request(payload()), 'chat')
        assert json.loads(answer.content)['response_text'] == 'answer'
        for capability in ('chat_cancel', 'chat_heartbeat'):
            response = await bridge.handle(request({'session_id': str(uuid4())}), capability)
            assert json.loads(response.content)['status'] == 'unknown'
        await bridge.aclose()
    asyncio.run(run())


def test_real_provider_stream_is_consumed_and_closed():
    async def run():
        closed = []
        async def stream(value, *, auth, context):
            try:
                yield StreamEvent(TextContent('an'))
                yield StreamEvent(TextContent('swer'))
                yield StreamEvent(LLMResult(Message('assistant', (TextContent('answer'),))), 'completed')
            finally:
                closed.append(True)
        bridge = backend(stream=stream)
        response = await bridge.handle(request(payload()), 'chat_stream')
        chunks = [part.decode() async for part in response.streaming_content]
        tokens = [json.loads(part.splitlines()[1][6:])['content'] for part in chunks if part.startswith('event: token')]
        assert tokens == ['an', 'swer'] and closed == [True]
        await bridge.aclose()
    asyncio.run(run())


def test_frame_limit_is_encoded_bytes():
    with pytest.raises(ContractError):
        frame('token', {'content': 'é' * 70000})


def test_asgi_disconnect_after_headers_drains_provider():
    async def run():
        entered, stopped, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        bridge = backend(complete=complete)
        app = create_application(Runtime('example', services={'studio_backend': bridge}))
        first = True
        sent = []
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': json.dumps(payload()).encode(), 'more_body': False}
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
            if message['type'] == 'http.response.body' and b'event: session' in message.get('body', b''):
                await entered.wait()
                disconnect.set()
        scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
            'path':'/app/agent/chat/stream','raw_path':b'/app/agent/chat/stream','query_string':b'',
            'root_path':'','server':('testserver',80),'client':('127.0.0.1',1),
            'headers':[(b'host',b'testserver'),(b'origin',ORIGIN.encode()),(b'x-api-agent-session',NONCE.encode()),(b'content-type',b'application/json')]}
        await asyncio.wait_for(app(scope, receive, send), 2)
        assert sent[0]['status'] == 200 and stopped.is_set()
        assert all(turn.done.is_set() for turn in bridge.lifecycle._turns.values())
        await bridge.aclose()
    asyncio.run(run())


def test_stream_error_after_headers_is_safe_and_terminal():
    async def run():
        async def complete(*args, **kwargs):
            raise RuntimeError('credential-like-private-value')
        bridge = backend(complete=complete)
        response = await bridge.handle(request(payload()), 'chat_stream')
        frames = [part async for part in response.streaming_content]
        assert response.status_code == 200
        assert frames[0].startswith(b'event: session') and frames[-1].startswith(b'event: error')
        assert b'credential-like-private-value' not in b''.join(frames)
        await bridge.aclose()
    asyncio.run(run())


def test_never_iterated_response_is_expired_and_disposed():
    async def run():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        bridge = backend(complete=complete)
        bridge.lifecycle.heartbeat_interval = .005
        bridge.lifecycle.expiry = .02
        response = await bridge.handle(request(payload()), 'chat_stream')
        assert response.streaming
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(stopped.wait(), 1)
        await bridge.aclose()
        assert all(turn.done.is_set() for turn in bridge.lifecycle._turns.values())
    asyncio.run(run())


@pytest.mark.parametrize('changes', [dict(session_id=1), dict(session_id='A0000000-0000-0000-0000-000000000000'), dict(max_output_tokens=None), dict(max_output_tokens=0), dict(cache_policy=None), dict(cache_policy='bad'), dict(messages=None)])
def test_additional_invalid_values(changes):
    with pytest.raises(ContractError):
        completion(payload(**changes), 'example', None)


@pytest.mark.parametrize('default,effort', [('absent', None), ('example', 'bad')])
def test_catalog_rejects_unoffered_defaults(default, effort):
    with pytest.raises(ContractError):
        catalog((AvailableSelection('example'),), default, effort)


@pytest.mark.parametrize('changes', [dict(reasoning_default='bad'), dict(stream=1)])
def test_invalid_optional_backend_configuration(changes):
    with pytest.raises(ValueError):
        backend(**changes)


@pytest.mark.parametrize('body', [[], {}, {'session_id':None}, {'session_id':str(uuid4()),'extra':True}])
def test_lifecycle_body_shape_is_strict(body):
    async def run():
        bridge = backend()
        response = await bridge.handle(request(body), 'chat_cancel')
        assert response.status_code == 400
        await bridge.aclose()
    asyncio.run(run())


def test_duplicate_session_rejected_without_second_completion():
    async def run():
        bridge = backend()
        body = payload()
        assert (await bridge.handle(request(body), 'chat')).status_code == 200
        assert (await bridge.handle(request(body), 'chat')).status_code == 409
        await bridge.aclose()
    asyncio.run(run())


def test_response_and_stream_total_limits_are_independent(monkeypatch):
    from api_agent.api import chat_contract
    async def run():
        bridge = backend()
        with pytest.raises(ContractError):
            bridge._response({'value': 'x' * 131072})
        monkeypatch.setattr(chat_contract, 'STREAM_BYTES', 1100)
        response = await bridge.handle(request(payload()), 'chat_stream')
        frames = [part async for part in response.streaming_content]
        assert frames[-1].startswith(b'event: error')
        assert len(b''.join(frames)) <= 1100
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['mismatch', 'tool', 'large', 'missing'])
def test_stream_rejects_invalid_or_unsupported_events(kind):
    from api_agent.providers.llm import ToolCall
    async def run():
        async def stream(*args, **kwargs):
            if kind == 'tool':
                yield StreamEvent(ToolCall('id','example','{}'), 'tool_call')
            elif kind == 'large':
                yield StreamEvent(TextContent('x' * 131073))
            elif kind == 'mismatch':
                yield StreamEvent(TextContent('wrong'))
                yield StreamEvent(LLMResult(Message('assistant',(TextContent('answer'),))), 'completed')
            else:
                yield StreamEvent(TextContent('partial'))
        bridge = backend(stream=stream)
        response = await bridge.handle(request(payload()), 'chat_stream')
        frames = [part async for part in response.streaming_content]
        assert frames[-1].startswith(b'event: error')
        assert not any(part.startswith(b'event: result') for part in frames)
        await bridge.aclose()
    asyncio.run(run())


def test_stream_cancel_reports_terminal_and_does_not_return_result():
    async def run():
        entered = asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        bridge = backend(complete=complete)
        body = payload()
        response = await bridge.handle(request(body), 'chat_stream')
        iterator = response.streaming_content
        assert (await anext(iterator)).startswith(b'event: session')
        await entered.wait()
        cancelled = await bridge.handle(request({'session_id':body['session_id']}), 'chat_cancel')
        assert json.loads(cancelled.content)['status'] == 'cancelled'
        frames = [part async for part in iterator]
        assert frames == [frame('cancelled', {'session_id':body['session_id']})]
        await bridge.aclose()
    asyncio.run(run())


def test_scheduler_rejection_closes_coroutine(monkeypatch):
    from api_agent.api.studio import _spawn
    import inspect
    async def operation():
        pass
    coroutine = operation()
    def reject(value):
        raise RuntimeError('scheduler unavailable')
    monkeypatch.setattr(asyncio, 'create_task', reject)
    with pytest.raises(RuntimeError):
        _spawn(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_catalog_cleanup_pending_keeps_context_and_auth_until_retry():
    from api_agent.providers.control import OperationContext, CleanupPendingError
    async def run():
        bridge = backend()
        entered, release = asyncio.Event(), asyncio.Event()
        async def resistant():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        context = OperationContext('catalog')
        work = asyncio.create_task(context.run(resistant()))
        bridge._contexts['catalog'] = context
        await entered.wait()
        with pytest.raises(CleanupPendingError) as pending:
            await bridge.aclose()
        assert bridge.closed and pending.value.owners == (bridge,)
        assert bridge._contexts['catalog'] is context
        release.set()
        await work
        await pending.value.retry()
        assert not bridge._contexts
    asyncio.run(run())


def test_queued_stream_backpressure_preserves_order_and_total_bound():
    async def run():
        async def stream(*args, **kwargs):
            for _ in range(32):
                yield StreamEvent(TextContent('é'))
            yield StreamEvent(LLMResult(Message('assistant',(TextContent('é' * 32),))), 'completed')
        bridge = backend(stream=stream)
        response = await bridge.handle(request(payload()), 'chat_stream')
        iterator = response.streaming_content
        await anext(iterator)
        await asyncio.sleep(.01)
        frames = [part async for part in iterator]
        assert sum(part.startswith(b'event: token') for part in frames) == 32
        assert len(b''.join(frames)) < 1048576
        await bridge.aclose()
    asyncio.run(run())


def test_stream_event_count_bound(monkeypatch):
    from api_agent.api import chat_contract
    async def run():
        bridge = backend()
        monkeypatch.setattr(chat_contract, 'STREAM_EVENTS', 2)
        response = await bridge.handle(request(payload()), 'chat_stream')
        frames = [part async for part in response.streaming_content]
        assert len(frames) == 2 and frames[-1].startswith(b'event: error')
        await bridge.aclose()
    asyncio.run(run())


def test_early_stream_close_drains_outstanding_waiter(monkeypatch):
    async def run():
        bridge = backend()
        waiting, stopped = asyncio.Event(), asyncio.Event()
        async def wait(*args):
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        monkeypatch.setattr(bridge.lifecycle, 'wait', wait)
        response = await bridge.handle(request(payload()), 'chat_stream')
        source = response._iterator
        assert (await anext(source)).startswith(b'event: session')
        await waiting.wait()
        await source.aclose()
        assert stopped.is_set()
        await bridge.aclose()
    asyncio.run(run())


def test_waiter_allocation_failure_disposes_admitted_operation(monkeypatch):
    from api_agent.api import studio
    async def run():
        bridge = backend()
        response = await bridge.handle(request(payload()), 'chat_stream')
        original = studio._spawn
        def reject(coroutine):
            coroutine.close()
            raise RuntimeError('scheduler unavailable')
        monkeypatch.setattr(studio, '_spawn', reject)
        frames = [part async for part in response.streaming_content]
        assert len(frames) == 1 and frames[0].startswith(b'event: error')
        monkeypatch.setattr(studio, '_spawn', original)
        assert all(turn.done.is_set() for turn in bridge.lifecycle._turns.values())
        await bridge.aclose()
    asyncio.run(run())


def test_frame_escaped_json_limit_rejects_small_raw_unicode():
    with pytest.raises(ContractError):
        frame('token', {'content': '😀' * 11000})


def test_valid_maximum_text_allows_session_and_result_overhead():
    async def run():
        text = 'a' * 65536
        async def complete(*args, **kwargs):
            return LLMResult(Message('assistant',(TextContent(text),)))
        bridge = backend(complete=complete)
        response = await bridge.handle(request(payload()), 'chat_stream')
        frames = [part async for part in response.streaming_content]
        assert frames[-1].startswith(b'event: done')
        assert 131072 < len(b''.join(frames)) < 1048576
        assert all(len(part) <= 131072 for part in frames)
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('retain_response', [False, True])
@pytest.mark.parametrize('resistant', [False, True])
def test_asgi_disconnect_while_send_is_blocked_closes_exact_source(retain_response,resistant):
    async def run():
        from api_agent.providers.control import CleanupPendingError
        entered, stopped, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = asyncio.Event()
        retained = []
        async def complete(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if resistant:
                    await release.wait()
                raise
            finally:
                stopped.set()
        bridge = backend(complete=complete)
        bridge.lifecycle.cleanup_timeout = .02
        original = bridge.handle
        async def retain(*args):
            response = await original(*args)
            if retain_response:
                retained.append(response)
            return response
        bridge.handle = retain
        app = create_application(Runtime('example', services={'studio_backend':bridge}))
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type':'http.request','body':json.dumps(payload()).encode(),'more_body':False}
            await disconnect.wait()
            return {'type':'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body' and b'event: session' in message.get('body',b''):
                await entered.wait()
                disconnect.set()
                await asyncio.Event().wait()
        scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
            'path':'/app/agent/chat/stream','raw_path':b'/app/agent/chat/stream','query_string':b'',
            'root_path':'','server':('testserver',80),'client':('127.0.0.1',1),
            'headers':[(b'host',b'testserver'),(b'origin',ORIGIN.encode()),(b'x-api-agent-session',NONCE.encode()),(b'content-type',b'application/json')]}
        try:
            if resistant:
                with pytest.raises(CleanupPendingError) as error:
                    await asyncio.wait_for(app(scope,receive,send),2)
                assert not stopped.is_set() and bridge._responses
                release.set()
                await asyncio.wait_for(stopped.wait(),1)
                await error.value.retry()
            else:
                await asyncio.wait_for(app(scope,receive,send),2)
        finally:
            release.set()
        assert stopped.is_set()
        assert not bridge._responses
        assert all(turn.done.is_set() for turn in bridge.lifecycle._turns.values())
        await bridge.aclose()
    asyncio.run(run())
