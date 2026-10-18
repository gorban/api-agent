"""Opt-in loopback text-completion bridge with caller-owned provider sessions.

Composition explicitly supplies authorized callbacks, policy, origin and an
in-memory nonce. This adapter does not select identities or execute workflows.
"""
import asyncio
import hmac
import ipaddress
import json
import math
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.core.exceptions import DisallowedHost
from django.core.handlers.asgi import ASGIRequest, get_script_prefix

from . import chat_contract
from .chat_lifecycle import ChatLifecycle
from .http_cleanup import close_http_callbacks
from ..providers.contracts import ContractError, ProviderAccessError, SessionClosedError
from ..providers.control import CleanupPendingError, OperationalError, OperationContext, finish_cleanup
from ..providers.inference import request_from_wire, result_to_wire
from ..providers.progress import report_timeout


class StudioBackend:
    """Callbacks: describe(*, auth) -> tuple; complete(request, *, auth, context) -> result.

    describe(auth=...) performs catalog discovery without inference. The owner is a
    persistent LLMSessionOwner, closed by composition through aclose(). The
    scrub callback must remove caller-known credential values from safe errors.
    """
    def __init__(self, *, owner, describe, complete, policy, origin, nonce,
                 scrub, timeout=120, body_limit=131072, reasoning_default=None, stream=None):
        parts = urlsplit(origin)
        if (parts.scheme not in ('http', 'https') or parts.hostname not in ('localhost', '127.0.0.1', '::1')
                or parts.path or parts.query or parts.fragment or parts.username or parts.password
                or type(nonce) is not str or not 32 <= len(nonce) <= 256 or not nonce.isascii()
                or not nonce.isprintable() or not callable(scrub)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(body_limit) is not int or not 1 <= body_limit <= 131072):
            raise ValueError('Invalid Studio bridge configuration')
        self.owner, self.describe, self.complete, self.policy = owner, describe, complete, policy
        self.origin, self._nonce, self.scrub = origin, nonce, scrub
        self.timeout, self.body_limit, self.closed = timeout, body_limit, False
        if reasoning_default is not None and reasoning_default not in chat_contract.EFFORTS:
            raise ValueError('Invalid reasoning default')
        if stream is not None and not callable(stream):
            raise ValueError('Invalid stream callback')
        self.reasoning_default, self.stream = reasoning_default, stream
        self.lifecycle = ChatLifecycle()
        self._owner_id = str(uuid4())
        self._contexts = {}
        self._responses = {}
        self._shutdown_callbacks = None

    async def aclose(self):
        self.closed = True
        if self._shutdown_callbacks is None:
            self._shutdown_callbacks = [self.lifecycle.aclose]
            self._shutdown_callbacks.extend(response.aclose for response in tuple(self._responses.values()))
            for context in tuple(self._contexts.values()):
                async def release(selected=context):
                    await self._release_context(selected)
                self._shutdown_callbacks.append(release)
        try:
            await close_http_callbacks(self._shutdown_callbacks)
        except CleanupPendingError:
            raise CleanupPendingError((self,), self.aclose) from None
        except asyncio.CancelledError as error:
            error.cleanup_pending = CleanupPendingError((self,), self.aclose)
            raise
        self._contexts.clear()
        await self.owner.aclose()

    async def _release_context(self, context):
        try:
            await _finish(context.aclose())
        finally:
            if context.retained_tasks:
                self.closed = True
            else:
                self._contexts.pop(context.correlation_id, None)

    def _local(self, request):
        try:
            local = ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')).is_loopback
        except ValueError:
            local = False
        return local

    def _guard(self, request):
        return (self._local(request) and request.headers.get('Origin') == self.origin and
                hmac.compare_digest(request.headers.get('X-API-Agent-Session', '').encode(), self._nonce.encode()))

    def _response(self, value, status=200):
        if len(json.dumps(value).encode()) > 131072:
            raise ContractError('Application response exceeds byte limit')
        response = JsonResponse(value, status=status)
        response['Cache-Control'] = 'private, no-store'
        return response

    def _error(self, error):
        if isinstance(error, ProviderAccessError):
            try:
                message = self.scrub(error.message)
            except Exception:
                message = None
            if type(message) is not str or not message.isprintable() or len(message) > 512:
                message = 'Provider rejected the request.'
            row = {'code': error.kind, 'message': message}
            if error.status is not None:
                row['status'] = error.status
            return self._response(row, 400 if error.kind == 'invalid_request' else 502)
        if isinstance(error, OperationalError):
            return self._response({'code': error.kind}, 504 if error.kind == 'deadline' else 502)
        if isinstance(error, (ContractError, ValueError, UnicodeError)):
            return self._response({'code': 'invalid_request'}, 400)
        if isinstance(error, SessionClosedError):
            return self._response({'code': 'unavailable',
                'message': 'This session is unavailable. Wait for suite completion, or start a new session.'}, 503)
        return self._response({'code': 'unknown'}, 502)

    def _admit(self, request, capability):
        # Browsers cannot attach the nonce to preflight; validate its exact intent.
        if request.method == 'OPTIONS':
            if (not self._local(request) or request.headers.get('Origin') != self.origin
                    or request.headers.get('Access-Control-Request-Method') != ('GET' if capability == 'models' else 'POST')
                    or {x.strip().lower() for x in request.headers.get('Access-Control-Request-Headers', '').split(',')} != ({'x-api-agent-session'} if capability == 'models' else {'content-type', 'x-api-agent-session'})):
                return self._response({'code': 'forbidden'}, 403)
            response = HttpResponse(status=204)
            response['Access-Control-Allow-Methods'] = 'GET' if capability == 'models' else 'POST'
            response['Access-Control-Allow-Headers'] = ('X-API-Agent-Session' if capability == 'models' else 'Content-Type, X-API-Agent-Session')
        elif not self._guard(request):
            return self._response({'code': 'forbidden'}, 403)
        elif request.method != ('GET' if capability == 'models' else 'POST'):
            return self._response({'code': 'method_not_allowed'}, 405)
        else:
            return None
        return self._cors(response)

    def _cors(self, response):
        response['Access-Control-Allow-Origin'] = self.origin
        response['Vary'] = 'Origin'
        return response

    async def handle(self, request, capability):
        response = self._admit(request, capability)
        if response is None:
            try:
                response = await self._execute(request, capability)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                response = self._error(error)
        return self._cors(response)

    async def handle_asgi(self, scope, receive, send, dispatch):
        routes = {'/app/agent/models': 'models', '/app/agent/chat': 'chat',
                  '/app/agent/chat/stream': 'chat_stream',
                  '/app/agent/chat/cancel': 'chat_cancel',
                  '/app/agent/chat/heartbeat': 'chat_heartbeat'}
        capability = routes.get(scope.get('path', '').removeprefix(get_script_prefix(scope)))
        if capability is None:
            return await dispatch(scope, receive, send)
        seen = set()
        response = None
        for name, value in scope.get('headers', ()):
            name = name.decode('latin1').lower()
            if name in seen and name in ('origin', 'x-api-agent-session', 'host',
                    'access-control-request-method', 'access-control-request-headers'):
                response = self._response({'code': 'forbidden'}, 403)
                break
            seen.add(name)
        if response is None:
            try:
                with BytesIO() as metadata_stream:
                    request = ASGIRequest(scope, metadata_stream)
                    request.get_host()
                    response = self._admit(request, capability)
            except (DisallowedHost, UnicodeDecodeError):
                response = self._response({'code': 'invalid_request'}, 400)
        if response is not None:
            return await self._send_asgi(self._cors(response), send)
        if self.closed:
            return await self._send_asgi(self._cors(self._response({'code': 'unavailable'}, 503)), send)
        async with report_timeout('Application HTTP request', self.timeout) as budget:
            body = bytearray()
            try:
                async with asyncio.timeout(budget.remaining):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        chunk = message.get('body', b'')
                        if len(body) + len(chunk) > self.body_limit:
                            response = self._response({'code': 'invalid_request'}, 413)
                            break
                        body.extend(chunk)
                        if not message.get('more_body', False):
                            break
            except TimeoutError:
                return await self._send_asgi(self._cors(self._response({'code': 'deadline'}, 408)), send)
            if response is not None:
                return await self._send_asgi(self._cors(response), send)
            delivered = False
            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            return await dispatch(scope, bounded_receive, send)

    @staticmethod
    async def _send_asgi(response, send):
        await send({'type': 'http.response.start', 'status': response.status_code,
                    'headers': [(name.lower().encode('latin1'), value.encode('latin1'))
                                for name, value in response.items()]})
        await send({'type': 'http.response.body', 'body': response.content})

    def _body(self, request):
        body = request.read(self.body_limit + 1)
        if request.content_type != 'application/json' or len(body) > self.body_limit:
            raise ContractError('Invalid request body')
        return json.loads(body, object_pairs_hook=_object, parse_constant=_constant)

    async def _execute(self, request, capability):
        if self.closed:
            raise SessionClosedError('Studio bridge closed')
        if capability in ('chat_cancel', 'chat_heartbeat'):
            row = self._body(request)
            if type(row) is not dict or set(row) != {'session_id'} or row['session_id'] is None:
                raise ContractError('Invalid lifecycle request')
            session_id = chat_contract.session_identifier(row['session_id'])
            if capability == 'chat_cancel':
                status = await self.lifecycle.cancel(self._owner_id, session_id)
            else:
                status = self.lifecycle.heartbeat(self._owner_id, session_id)
            return self._response({'session_id': session_id, 'status': status})
        hints = (chat_contract.heartbeat(self.lifecycle.heartbeat_interval, self.lifecycle.expiry)
                 if capability == 'chat_stream' else None)
        row = None if capability == 'models' else self._body(request)
        if capability != 'models':
            chat_contract.completion(row, 'default', self.reasoning_default)
        async with report_timeout('Studio request', self.timeout) as budget:
            context = OperationContext(str(uuid4()), budget.deadline)
            self._contexts[context.correlation_id] = context
            transferred = False
            try:
                selections = self.policy.filter(await self.owner.call(self.describe, context=context))
                default = self.policy.select_default(selections)
                application_catalog = chat_contract.catalog(selections, default, self.reasoning_default)
                if capability == 'models':
                    return self._response(application_catalog)
                session_id, value = chat_contract.completion(row, default, self.reasoning_default)
                self.policy.require_allowed(value.selected_llm)
                selection = next((item for item in selections if item.selected_llm == value.selected_llm), None)
                if selection is None:
                    raise ContractError('Unknown model')
                selection.validate(value)
                queue = asyncio.Queue(maxsize=16)
                async def invoke(context):
                    async with report_timeout('Studio completion', self.timeout):
                        if capability == 'chat_stream' and self.stream is not None:
                            async def operation(*, auth):
                                return self.stream(value, auth=auth, context=context)
                            final = None
                            chunks = []
                            chunk_bytes = 0
                            async with self.owner.stream(operation, context=context) as events:
                                async for event in events:
                                    if event.kind == 'text':
                                        chunks.append(event.payload.text)
                                        chunk_bytes += len(event.payload.text.encode())
                                        if chunk_bytes > 131072 or len(chunks) > 4096:
                                            raise ContractError('Application stream exceeds byte limit')
                                        await queue.put(event.payload.text)
                                    elif event.kind == 'completed':
                                        final = chat_contract.result(session_id, event.payload)
                                    else:
                                        raise ContractError('Unsupported application stream event')
                            if final is None or ''.join(chunks) != final['response_text']:
                                raise ContractError('Invalid stream completion')
                            return final
                        async def operation(*, auth):
                            return await self.complete(value, auth=auth, context=context)
                        return chat_contract.result(session_id, await self.owner.call(operation, context=context))
                response_owner = ResponseCleanup(self, session_id) if capability == 'chat_stream' else None
                if session_id in self._responses or len(self._responses) >= self.lifecycle.max_active + self.lifecycle.max_terminal:
                    return self._response({'code': 'session_unavailable'}, 409)
                try:
                    turn = self.lifecycle.start(self._owner_id, session_id, invoke, context=context)
                except ValueError:
                    return self._response({'code': 'session_unavailable'}, 409)
                transferred = True
                self._contexts.pop(context.correlation_id, None)
                if capability == 'chat_stream':
                    owner = response_owner
                    owner.turn = turn
                    self._responses[session_id] = owner
                    try:
                        callbacks = getattr(request, 'scope', {}).get('api_agent_http_cleanup')
                        if callbacks is not None:
                            callbacks.append(owner.aclose)
                        owner.source = self._events(session_id, turn, queue, hints)
                        return StreamingHttpResponse(owner.source,
                            content_type='text/event-stream', headers={'Cache-Control': 'private, no-store'})
                    except BaseException:
                        await _finish(owner.aclose())
                        raise
                try:
                    terminal = await self.lifecycle.wait_turn(self._owner_id, turn)
                    if terminal.state == 'completed':
                        return self._response(terminal.result)
                    if terminal.state == 'cancelled':
                        return self._response({'session_id': session_id, 'code': 'cancelled'}, 409)
                    raise terminal.error
                except asyncio.CancelledError:
                    try:
                        await _finish(self.lifecycle.disconnect_turn(self._owner_id, turn))
                    except CleanupPendingError:
                        pass
                    raise
            finally:
                if not transferred:
                    await self._release_context(context)

    async def _events(self, session_id, turn, queue, hints):
        source = self._raw_events(session_id, turn, queue, hints)
        total = count = 0
        try:
            async for body in source:
                total += len(body)
                count += 1
                # Reserve a bounded terminal error within the shared stream cap.
                if total > chat_contract.STREAM_BYTES - 1024 or count >= chat_contract.STREAM_EVENTS:
                    yield chat_contract.frame('error', {'session_id': session_id, 'code': 'invalid_request'})
                    return
                yield body
        finally:
            await source.aclose()
            owner = self._responses.get(session_id)
            if owner is not None and owner.turn is turn:
                self._responses.pop(session_id, None)

    async def _raw_events(self, session_id, turn, queue, hints):
        settled = False
        waiter = None
        try:
            waiter = _spawn(self.lifecycle.wait_turn(self._owner_id, turn))
            yield chat_contract.frame('session', {'session_id': session_id, **hints})
            while not waiter.done() or not queue.empty():
                if not queue.empty():
                    yield chat_contract.frame('token', {'content': queue.get_nowait()})
                    continue
                read = _spawn(queue.get())
                try:
                    done, _ = await asyncio.wait((read, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if read in done:
                        yield chat_contract.frame('token', {'content': read.result()})
                finally:
                    if not read.done():
                        read.cancel()
                        await asyncio.gather(read, return_exceptions=True)
            terminal = await waiter
            settled = True
            if terminal.state == 'cancelled':
                yield chat_contract.frame('cancelled', {'session_id': session_id})
                return
            if terminal.state != 'completed':
                raise terminal.error
            if self.stream is None:
                yield chat_contract.frame('token', {'content': terminal.result['response_text']})
            yield chat_contract.frame('response_complete', {'session_id': session_id})
            yield chat_contract.frame('result', terminal.result)
            yield chat_contract.frame('done', {'session_id': session_id})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            payload = json.loads(self._error(error).content)
            yield chat_contract.frame('error', {'session_id': session_id, **payload})
        finally:
            try:
                if not settled:
                    await _finish(self.lifecycle.disconnect_turn(self._owner_id, turn))
            finally:
                if waiter is not None:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ContractError('Nonfinite JSON number')


def _spawn(coroutine):
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


class ResponseCleanup:
    """Keep both operation and exact iterator disposal reachable from the backend."""
    def __init__(self, backend, session_id):
        self.backend, self.session_id = backend, session_id
        self.source = self.turn = None
        self._callbacks = None

    async def aclose(self):
        if self._callbacks is None:
            async def disconnect():
                if self.turn is not None:
                    await self.backend.lifecycle.disconnect_turn(self.backend._owner_id, self.turn)
            self._callbacks = [disconnect]
            if self.source is not None:
                self._callbacks.append(self.source.aclose)
        try:
            await close_http_callbacks(self._callbacks)
        except CleanupPendingError:
            raise CleanupPendingError((self,), self.aclose) from None
        except asyncio.CancelledError as error:
            error.cleanup_pending = CleanupPendingError((self,), self.aclose)
            raise
        if self.backend._responses.get(self.session_id) is self:
            self.backend._responses.pop(self.session_id, None)


async def _finish(coroutine):
    try:
        return await finish_cleanup(coroutine)
    except BaseException:
        coroutine.close()
        raise
