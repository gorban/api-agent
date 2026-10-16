"""Opt-in loopback text-completion bridge with caller-owned provider sessions.

Composition explicitly supplies authorized callbacks, policy, origin and an
in-memory nonce. This adapter does not select identities or execute workflows.
"""
import asyncio
import hmac
import ipaddress
import json
import math
from urllib.parse import urlsplit
from uuid import uuid4

from django.http import HttpResponse, JsonResponse

from ..providers.catalog import catalog_to_wire
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
                 scrub, timeout=120, body_limit=131072):
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
        self._contexts = {}

    async def aclose(self):
        self.closed = True
        for context in tuple(self._contexts.values()):
            try:
                await self._release_context(context)
            except CleanupPendingError:
                raise CleanupPendingError((self,), self.aclose) from None
        await self.owner.aclose()

    async def _release_context(self, context):
        try:
            await finish_cleanup(context.aclose())
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
            return self._response({'code': 'unavailable'}, 503)
        return self._response({'code': 'unknown'}, 502)

    async def handle(self, request, capability):
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
            try:
                response = await self._execute(request, capability)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                response = self._error(error)
        response['Access-Control-Allow-Origin'] = self.origin
        response['Vary'] = 'Origin'
        return response

    async def _execute(self, request, capability):
        if self.closed:
            raise SessionClosedError('Studio bridge closed')
        if capability != 'models':
            body = request.read(self.body_limit + 1)
            if request.content_type != 'application/json' or len(body) > self.body_limit:
                raise ContractError('Invalid request body')
            row = json.loads(body, object_pairs_hook=_object, parse_constant=_constant)
            if type(row) is not dict or 'version' in row:
                raise ContractError('Expected text completion')
            value = request_from_wire(row)
            if not value.request_id:
                raise ContractError('Request identifier required')
        async with report_timeout('Studio request', self.timeout) as budget:
            context = OperationContext(str(uuid4()), budget.deadline)
            self._contexts[context.correlation_id] = context
            try:
                selections = self.policy.filter(await self.owner.call(self.describe, context=context))
                if capability == 'models':
                    return self._response({'models': catalog_to_wire(selections),
                                           'default': self.policy.select_default(selections)})
                self.policy.require_allowed(value.selected_llm)
                selection = next((item for item in selections if item.selected_llm == value.selected_llm), None)
                if selection is None:
                    raise ContractError('Unknown model')
                selection.validate(value)
                async def operation(*, auth):
                    return await self.complete(value, auth=auth, context=context)
                result = result_to_wire(await self.owner.call(operation, context=context))
                if capability == 'chat_stream':
                    frame = {'kind': 'completed', 'payload': result}
                    body = ('data: ' + json.dumps(frame, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode()
                    if max(len(body), len(json.dumps(frame, separators=(',', ':')).encode())) > 131072:
                        raise ContractError('Studio response exceeds its byte limit')
                    return HttpResponse(body, content_type='text/event-stream',
                                        headers={'Cache-Control': 'private, no-store'})
                return self._response(result)
            finally:
                cancelled = asyncio.current_task().cancelling()
                try:
                    await self._release_context(context)
                except CleanupPendingError:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ContractError('Nonfinite JSON number')
