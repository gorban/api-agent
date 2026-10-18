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
        monkeypatch.setattr(bridge.lifecycle, 'wait_turn', wait)
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


def ingress_scope(path='/app/agent/chat',method='POST',**changes):
    return {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
        'path':path,'raw_path':path.encode(),'query_string':b'','root_path':'',
        'server':('testserver',80),'client':('127.0.0.1',1),
        'headers':[(b'host',b'testserver'),(b'origin',ORIGIN.encode()),
                   (b'x-api-agent-session',NONCE.encode()),(b'content-type',b'application/json')],**changes}


@pytest.mark.parametrize('mode',['nonce','origin','peer','method','preflight','duplicate','host',
                                  'underscore-only','underscore-override','missing-server-host','invalid-query'])
def test_asgi_admission_rejects_before_receiving_or_spooling(monkeypatch,mode):
    from django.core.handlers.asgi import ASGIHandler
    async def run():
        bridge=backend()
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        scope=ingress_scope()
        if mode in ('nonce','origin','host'):
            target={'nonce':b'x-api-agent-session','origin':b'origin','host':b'host'}[mode]
            scope['headers']=[(key,b'wrong' if key == target else value) for key,value in scope['headers']]
        elif mode == 'peer':
            scope['client']=('192.0.2.1',1)
        elif mode == 'method':
            scope['method']='DELETE'
        elif mode == 'preflight':
            scope['method']='OPTIONS'
        elif mode == 'underscore-only':
            scope['headers']=[(key,value) for key,value in scope['headers'] if key != b'x-api-agent-session']
            scope['headers'].append((b'x_api_agent_session',NONCE.encode()))
        elif mode == 'underscore-override':
            scope['headers']=[(key,b'wrong' if key == b'x-api-agent-session' else value) for key,value in scope['headers']]
            scope['headers'].append((b'x_api_agent_session',NONCE.encode()))
        elif mode == 'missing-server-host':
            scope['server']=None
            scope['headers']=[(key,value) for key,value in scope['headers'] if key != b'host']
        elif mode == 'invalid-query':
            scope['query_string']=b'\xff'
        else:
            scope['headers'].append((b'origin',ORIGIN.encode()))
        async def no_body():
            pytest.fail('rejected request body was received')
        async def no_spool(*args):
            pytest.fail('rejected request entered Django body reader')
        monkeypatch.setattr(ASGIHandler,'read_body',no_spool)
        sent=[]
        async def send(message):
            sent.append(message)
        await app(scope,no_body,send)
        expected=405 if mode == 'method' else 400 if mode in ('host','missing-server-host','invalid-query') else 403
        assert sent[0]['status'] == expected
        assert not bridge.lifecycle._turns
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('open_body',[False,True])
@pytest.mark.parametrize('chunks',[1,2])
def test_asgi_actual_byte_cap_rejects_without_spooling_or_drain(monkeypatch,open_body,chunks):
    from django.core.handlers.asgi import ASGIHandler
    async def run():
        bridge=backend(body_limit=64)
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        count=0
        async def receive():
            nonlocal count
            count+=1
            assert count <= chunks
            return {'type':'http.request','body':b'x' * (65 if chunks == 1 else 33),
                    'more_body': count < chunks or open_body}
        async def no_spool(*args):
            pytest.fail('oversized body entered Django reader')
        monkeypatch.setattr(ASGIHandler,'read_body',no_spool)
        sent=[]
        async def send(message):
            sent.append(message)
        await app(ingress_scope(),receive,send)
        assert count == chunks and sent[0]['status'] == 413
        assert not bridge.lifecycle._turns
        await bridge.aclose()
    asyncio.run(run())


def test_asgi_admitted_chunked_body_is_replayed_once_and_disconnect_remains_live():
    async def run():
        bridge=backend()
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        body=json.dumps(payload()).encode()
        chunks=[body[:20],body[20:]]
        async def receive():
            if chunks:
                chunk=chunks.pop(0)
                return {'type':'http.request','body':chunk,'more_body':bool(chunks)}
            await asyncio.Event().wait()
        sent=[]
        async def send(message):
            sent.append(message)
        await app(ingress_scope(),receive,send)
        assert sent[0]['status'] == 200 and not chunks
        assert json.loads(b''.join(row.get('body',b'') for row in sent))['response_text'] == 'answer'
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('end',['disconnect','deadline','cancel'])
def test_asgi_body_wait_owns_disconnect_deadline_and_cancellation(end):
    async def run():
        bridge=backend(timeout=.02 if end == 'deadline' else 120)
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        entered,stopped=asyncio.Event(),asyncio.Event()
        async def receive():
            entered.set()
            if end == 'disconnect':
                return {'type':'http.disconnect'}
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        sent=[]
        async def send(message):
            sent.append(message)
        operation=asyncio.create_task(app(ingress_scope(),receive,send))
        await entered.wait()
        if end == 'cancel':
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            await operation
        assert not bridge.lifecycle._turns
        assert (sent[0]['status'] if sent else None) == (408 if end == 'deadline' else None)
        assert end == 'disconnect' or stopped.is_set()
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('features,efforts',[((),None),(('reasoning_effort',),None),(('reasoning_effort',),('low',))])
def test_configured_reasoning_default_rejects_only_incompatible_selected_model(features,efforts):
    async def run():
        calls=[]
        async def describe(**kwargs):
            return (AvailableSelection('example',features=features,reasoning_efforts=efforts),
                    AvailableSelection('other',features=('reasoning_effort',),reasoning_efforts=('high',)))
        async def complete(value,**kwargs):
            calls.append(value.selected_llm)
            return LLMResult(Message('assistant',(TextContent('answer'),)))
        bridge=backend(describe=describe,complete=complete,reasoning_default='high')
        assert (await bridge.handle(request(method='GET'),'models')).status_code == 200
        response=await bridge.handle(request(payload()),'chat')
        assert response.status_code == 502 and json.loads(response.content)['code'] == 'unsupported'
        assert calls == []
        assert (await bridge.handle(request(payload(model='other')),'chat')).status_code == 200
        assert calls == ['other']
        await bridge.aclose()
    asyncio.run(run())


def test_supported_configured_reasoning_default_and_explicit_off_keep_intent():
    async def run():
        calls=[]
        async def describe(**kwargs):
            return (AvailableSelection('example',features=('reasoning_effort',),reasoning_efforts=('off','high')),)
        async def complete(value,**kwargs):
            calls.append(value.payload.generation.reasoning_effort)
            return LLMResult(Message('assistant',(TextContent('answer'),)))
        bridge=backend(describe=describe,complete=complete,reasoning_default='high')
        catalog_response=await bridge.handle(request(method='GET'),'models')
        assert json.loads(catalog_response.content)['reasoning_default'] == 'high'
        assert (await bridge.handle(request(payload()),'chat')).status_code == 200
        assert (await bridge.handle(request(payload(reasoning_effort='off')),'chat')).status_code == 200
        assert calls == ['high','off']
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['preflight','closed','prefix'])
def test_asgi_early_control_responses_never_receive_body(mode):
    async def run():
        bridge=backend()
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        scope=ingress_scope()
        expected=204
        if mode == 'preflight':
            scope['method']='OPTIONS'
            scope['headers'] += [(b'access-control-request-method',b'POST'),
                                 (b'access-control-request-headers',b'Content-Type, X-API-Agent-Session')]
        elif mode == 'closed':
            await bridge.aclose()
            expected=503
        else:
            scope['path']='/mounted/app/agent/chat'
            scope['root_path']='/mounted'
            scope['headers']=[(key,b'wrong' if key == b'x-api-agent-session' else value) for key,value in scope['headers']]
            expected=403
        async def receive():
            pytest.fail('early control received body')
        sent=[]
        async def send(message):
            sent.append(message)
        await app(scope,receive,send)
        assert sent[0]['status'] == expected
        await bridge.aclose()
    asyncio.run(run())


def test_other_asgi_routes_preserve_their_existing_dispatch():
    async def run():
        create_application(Runtime('example'))
        bridge=backend()
        calls=[]
        async def dispatch(scope,receive,send):
            calls.append(scope['path'])
        try:
            await bridge.handle_asgi(ingress_scope('/other'),None,None,dispatch)
            assert calls == ['/other']
        finally:
            await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('operation',['close','iterate'])
def test_old_response_uses_captured_turn_after_same_id_replacement(operation):
    async def run():
        entered,stopped=asyncio.Event(),asyncio.Event()
        async def complete(value,**kwargs):
            if value.payload.messages[0].content[0].text == 'new':
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return LLMResult(Message('assistant',(TextContent('original'),)))
        bridge=backend(complete=complete)
        body=payload()
        old_response=await bridge.handle(request(body),'chat_stream')
        old_owner=bridge._responses[body['session_id']]
        await bridge.lifecycle.wait_turn(bridge._owner_id,old_owner.turn)
        old_owner.turn._finished -= bridge.lifecycle.terminal_ttl + 1
        # Simulate retirement to exercise the captured-handle guard independently.
        bridge._responses.pop(body['session_id'])
        new_body=body | {'messages':[{'role':'user','content':'new'}]}
        await bridge.handle(request(new_body),'chat_stream')
        await entered.wait()
        current=bridge._responses[body['session_id']]
        if operation == 'close':
            await old_owner.aclose()
        else:
            async with asyncio.timeout(1):
                frames=[part async for part in old_response.streaming_content]
            result=json.loads(next(part for part in frames if part.startswith(b'event: result')).splitlines()[1][6:])
            assert result['response_text'] == 'original'
        assert current.turn.state == 'running' and not stopped.is_set()
        assert bridge._responses[body['session_id']] is current
        await bridge.aclose()
        assert stopped.is_set()
    asyncio.run(run())


@pytest.mark.parametrize('interval,expiry',[(0,30),(True,30),(float('nan'),30),(float('inf'),30),(10,10),(10,2147483.648),(2**1024,2**1025)],
                         ids=['zero','boolean','nan','infinity','order','limit','huge-integer'])
def test_invalid_advertised_timing_rejects_before_provider_access(interval,expiry):
    async def run():
        calls=[]
        async def describe(**kwargs):
            calls.append(True)
        bridge=backend(describe=describe)
        bridge.lifecycle.heartbeat_interval=interval
        bridge.lifecycle.expiry=expiry
        response=await bridge.handle(request(payload()),'chat_stream')
        assert response.status_code == 400 and calls == []
        assert not bridge.lifecycle._turns
        await bridge.aclose()
    asyncio.run(run())


def test_fractional_advertised_milliseconds_remain_positive_and_exact():
    from api_agent.api.chat_contract import heartbeat
    assert heartbeat(.0001,.0002) == {'heartbeat_interval_ms':.1,'heartbeat_timeout_ms':.2}
    assert heartbeat(1,2147483.647)['heartbeat_timeout_ms'] == 2147483647
    async def run():
        bridge=backend()
        bridge.lifecycle.heartbeat_interval=.0001
        bridge.lifecycle.expiry=30
        response=await bridge.handle(request(payload()),'chat_stream')
        frames=[part async for part in response.streaming_content]
        session=json.loads(frames[0].splitlines()[1][6:])
        assert session['heartbeat_interval_ms'] == .1
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['ignored-alias','missing-server','missing-host'])
def test_asgi_framework_metadata_valid_controls_still_receive_bounded_body(mode):
    async def run():
        bridge=backend()
        app=create_application(Runtime('example',services={'studio_backend':bridge}))
        scope=ingress_scope()
        if mode == 'ignored-alias':
            scope['headers'].append((b'x_api_agent_session',b'wrong'))
        elif mode == 'missing-server':
            scope['server']=None
        else:
            scope['headers']=[(key,value) for key,value in scope['headers'] if key != b'host']
        received=0
        async def receive():
            nonlocal received
            received+=1
            if received == 1:
                return {'type':'http.request','body':json.dumps(payload()).encode(),'more_body':False}
            await asyncio.Event().wait()
        sent=[]
        async def send(message):
            sent.append(message)
        await app(scope,receive,send)
        assert sent[0]['status'] == 200
        assert json.loads(b''.join(item.get('body',b'') for item in sent))['response_text'] == 'answer'
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mounted',[False,True])
@pytest.mark.parametrize('path,method,status',[
    ('models','GET',501),('chat','POST',501),('chat/stream','POST',501),
    ('chat/cancel','POST',501),('chat/heartbeat','POST',501),
    ('result-sets/item','GET',501),('result-sets','DELETE',501),
    ('chat','GET',405),('chat','OPTIONS',204),('result-sets','OPTIONS',405)])
def test_uncomposed_capabilities_resolve_before_any_body_receive(mounted,path,method,status):
    async def run():
        app=create_application(Runtime('example'))
        scope=ingress_scope('/app/agent/'+path,method)
        scope['client']=('192.0.2.1',1234)
        scope['headers']=[(b'host',b'testserver'),(b'origin',b'https://invalid.example'),
                          (b'x-api-agent-session',b'wrong'),(b'content-length',b'200000')]
        if mounted:
            scope['root_path']='/mounted'
            scope['path']='/mounted'+scope['path']
        async def receive():
            pytest.fail('unavailable capability acquired unauthorized open body')
        sent=[]
        async def send(message):
            sent.append(message)
        await app(scope,receive,send)
        assert sent[0]['status'] == status
        headers=dict(sent[0]['headers'])
        assert all(name == name.lower() for name in headers)
        if status in (204,405):
            assert headers[b'allow'] == (b'DELETE' if path == 'result-sets' else b'POST, OPTIONS')
        if status == 204:
            assert headers[b'access-control-allow-methods'] == b'POST, OPTIONS'
            assert headers[b'access-control-allow-headers'] == b'Content-Type, Authorization'
        if status == 501:
            assert headers[b'cache-control'] == b'private, no-store'
            assert json.loads(sent[1]['body'])['code'] == 'not_implemented'
    asyncio.run(run())


@pytest.mark.parametrize('mode',['host','query','override','empty-override','ordinary','unknown','legacy'])
def test_uncomposed_early_response_preserves_host_and_dispatch(monkeypatch,mode):
    from api_agent.api.application import RuntimeHandler
    from django.urls import re_path
    async def run():
        runtime=Runtime('example')
        app=create_application(runtime)
        scope=ingress_scope()
        expected=400
        if mode == 'host':
            scope['headers']=[(b'host',b'invalid.example')]
        elif mode == 'query':
            scope['query_string']=b'\xff'
        elif mode == 'empty-override':
            runtime.urlconf=()
        elif mode == 'override':
            async def custom(request):
                pytest.fail('custom route must dispatch through Django')
            runtime.urlconf=type('CustomRoutes',(),{'urlpatterns':[re_path(r'^app/agent/chat$',custom)]})
        elif mode == 'ordinary':
            scope['path']='/app/agent/internals/health'
        elif mode == 'unknown':
            scope['path']='/missing'
        else:
            runtime.services={'studio_backend':object()}
        calls=[]
        async def dispatch(self,scope,receive,send):
            calls.append(scope['path'])
        monkeypatch.setattr(RuntimeHandler,'__call__',dispatch)
        async def receive():
            pytest.fail('early response received a body')
        sent=[]
        async def send(message):
            sent.append(message)
        await app(scope,receive,send)
        if mode in ('host','query'):
            assert sent[0]['status'] == expected and not calls
        else:
            assert calls == [scope['path']] and not sent
    asyncio.run(run())


@pytest.mark.parametrize('reuse_capability',['chat','chat_stream'])
def test_retained_sources_reject_reuse_and_enforce_total_owner_bound(reuse_capability):
    async def run():
        bridge=backend()
        bridge.lifecycle.max_active=bridge.lifecycle.max_terminal=1
        body=payload()
        first=await bridge.handle(request(body),'chat_stream')
        owner=bridge._responses[body['session_id']]
        await bridge.lifecycle.wait_turn(bridge._owner_id,owner.turn)
        owner.turn._finished-=bridge.lifecycle.terminal_ttl+1
        for _ in range(3):
            response=await bridge.handle(request(body),reuse_capability)
            assert response.status_code == 409
        second_body=payload()
        second=await bridge.handle(request(second_body),'chat_stream')
        second_owner=bridge._responses[second_body['session_id']]
        await bridge.lifecycle.wait_turn(bridge._owner_id,second_owner.turn)
        assert len(bridge._responses) == 2
        assert (await bridge.handle(request(payload()),'chat_stream')).status_code == 409
        await bridge.aclose()
        assert not bridge._responses
        for response in (first,second):
            assert [frame async for frame in response.streaming_content] == []
    asyncio.run(run())


@pytest.mark.parametrize('retirement',['close','iterate','retry'])
def test_response_retirement_allows_reuse_without_stale_removal(retirement):
    from api_agent.providers.control import CleanupPendingError
    async def run():
        bridge=backend()
        body=payload()
        response=await bridge.handle(request(body),'chat_stream')
        old=bridge._responses[body['session_id']]
        await bridge.lifecycle.wait_turn(bridge._owner_id,old.turn)
        old.turn._finished-=bridge.lifecycle.terminal_ttl+1
        if retirement == 'iterate':
            assert [part async for part in response.streaming_content][-1].startswith(b'event: done')
        elif retirement == 'close':
            await old.aclose()
        else:
            source=old.source
            class PendingSource:
                pending=True
                async def aclose(self):
                    if self.pending:
                        raise RuntimeError('pending source')
                    await source.aclose()
            pending=PendingSource()
            old.source=pending
            with pytest.raises(CleanupPendingError) as error:
                await old.aclose()
            assert (await bridge.handle(request(body),'chat_stream')).status_code == 409
            assert bridge._responses[body['session_id']] is old
            pending.pending=False
            await error.value.retry()
        assert body['session_id'] not in bridge._responses
        replacement=await bridge.handle(request(body),'chat_stream')
        assert replacement.status_code == 200
        current=bridge._responses[body['session_id']]
        await old.aclose()
        assert bridge._responses[body['session_id']] is current
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('efforts,override_status,expected',[(None,502,['example']),((),502,['example']),(('off',),200,['example','other'])])
def test_mixed_catalog_preserves_choices_and_rejects_incompatible_selected_effort(efforts,override_status,expected):
    async def run():
        calls=[]
        async def describe(**kwargs):
            return (AvailableSelection('example',features=('reasoning_effort',),reasoning_efforts=('high',)),
                    AvailableSelection('other',features=('reasoning_effort',) if efforts is not None else (),reasoning_efforts=efforts))
        async def complete(value,**kwargs):
            calls.append(value.selected_llm)
            return LLMResult(Message('assistant',(TextContent('answer'),)))
        bridge=backend(describe=describe,complete=complete,reasoning_default='high')
        response=await bridge.handle(request(method='GET'),'models')
        assert response.status_code == 200
        rows=json.loads(response.content)['models']
        assert [row['value'] for row in rows] == ['example','other']
        assert rows[1].get('reasoning_efforts') == (list(efforts) if efforts is not None else None)
        assert calls == []
        assert (await bridge.handle(request(payload()),'chat')).status_code == 200
        rejected=await bridge.handle(request(payload(model='other')),'chat')
        assert rejected.status_code == 502 and json.loads(rejected.content)['code'] == 'unsupported'
        assert calls == ['example']
        assert (await bridge.handle(request(payload(model='other',reasoning_effort='off')),'chat')).status_code == override_status
        assert calls == expected
        await bridge.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('configured',[None,'off','high'])
def test_every_advertised_selection_accepts_omitted_reasoning(configured):
    async def run():
        calls=[]
        async def describe(**kwargs):
            return (AvailableSelection('example',features=('reasoning_effort',),reasoning_efforts=('off','high')),
                    AvailableSelection('other') if configured is None else
                    AvailableSelection('other',features=('reasoning_effort',),reasoning_efforts=('off','high')))
        async def complete(value,**kwargs):
            calls.append((value.selected_llm,value.payload.generation.reasoning_effort))
            return LLMResult(Message('assistant',(TextContent('answer'),)))
        bridge=backend(describe=describe,complete=complete,reasoning_default=configured)
        response=await bridge.handle(request(method='GET'),'models')
        for row in json.loads(response.content)['models']:
            assert (await bridge.handle(request(payload(model=row['value'])),'chat')).status_code == 200
        assert calls == [('example',configured),('other',configured)]
        await bridge.aclose()
    asyncio.run(run())
