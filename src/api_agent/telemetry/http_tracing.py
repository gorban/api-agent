"""Finite HTTP trace metadata and request-local ownership; never export raw paths."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

METHODS = frozenset(('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'CONNECT', 'TRACE', '_OTHER'))
ROUTES = frozenset(('/', '/app/agent', '/app/agent/', '/app/agent/internals/health',
    '/app/agent/internals/health/startup', '/app/agent/internals/health/ready',
    '/app/agent/models', '/app/agent/chat', '/app/agent/chat/stream', '/app/agent/chat/cancel',
    '/app/agent/chat/heartbeat', '/app/agent/result-sets', '/app/agent/result-sets/{result_id}'))
_CURRENT = ContextVar('http_trace_ownership', default=None)


def diagnostics_level(values):
    level = values.get('TRACE_HTTP_DIAGNOSTICS_LEVEL', 'off')
    if type(level) is not str or level not in ('off', 'info'):
        raise ValueError('Invalid HTTP tracing diagnostics level')
    return level


@dataclass(frozen=True)
class HTTPMetadata:
    method: str
    route: str | None = None
    diagnostic: bool = False

    @property
    def name(self):
        method = self.method if type(self.method) is str and self.method in METHODS else '_OTHER'
        return ('HTTP' if method == '_OTHER' else method) + (' ' + self.route if type(self.route) is str and self.route in ROUTES else '')

    def attributes(self):
        values = {'http.request.method': self.method if type(self.method) is str and self.method in METHODS else '_OTHER'}
        if type(self.route) is str and self.route in ROUTES:
            values['http.route'] = self.route
        return values


def safe_name(name):
    if type(name) is not str:
        return False
    method, _, route = name.partition(' ')
    return method in (METHODS - {'_OTHER'}) | {'HTTP'} and (name == method or route in ROUTES)


@contextmanager
def bind_http(owner, scope, metadata):
    token = _CURRENT.set((owner, scope, metadata))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_http(owner, scope):
    current = _CURRENT.get()
    return current[2] if current is not None and current[0] is owner and current[1] is scope else None


def _preflight(scope):
    headers = {}
    for name, value in scope.get('headers', ()):
        if type(name) is not bytes or type(value) is not bytes:
            return False
        name = name.lower()
        if name in (b'origin', b'access-control-request-method'):
            if name in headers:
                return False
            headers[name] = value
    origin = headers.get(b'origin', b'')
    method = headers.get(b'access-control-request-method', b'')
    return bool(origin.strip()) and len(origin) <= 4096 and re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,32}", method) is not None


def _request_metadata(scope, *, owned=False, urlconf='api_agent.api.urls'):
    if type(scope.get('method')) is not str:
        return None
    method = scope['method'] if scope['method'] in METHODS else '_OTHER'
    route = None
    if owned:
        from django.conf import settings
        from django.core.handlers.asgi import get_script_prefix
        from django.urls import resolve, Resolver404
        from api_agent.api import views
        path = scope.get('path', '').removeprefix(get_script_prefix(scope))
        try:
            match = resolve(path, urlconf=urlconf if urlconf is not None else settings.ROOT_URLCONF)
        except Resolver404:
            match = None
        fixed = {'/': (views.landing, {}), '/app/agent': (views.landing, {}), '/app/agent/': (views.landing, {}),
            '/app/agent/internals/health': (views.health, {}),
            '/app/agent/internals/health/startup': (views.health, {'probe': 'startup'}),
            '/app/agent/internals/health/ready': (views.health, {'probe': 'ready'}),
            '/app/agent/models': (views.models, {}), '/app/agent/chat': (views.chat, {}),
            '/app/agent/chat/stream': (views.chat_stream, {}), '/app/agent/chat/cancel': (views.chat_cancel, {}),
            '/app/agent/chat/heartbeat': (views.chat_heartbeat, {}), '/app/agent/result-sets': (views.result_sets, {})}
        if match is not None and not match.args:
            if path in fixed and (match.func, match.kwargs) == fixed[path]:
                route = path
            elif (match.func is views.result_set and set(match.kwargs) == {'result_id'}
                  and re.fullmatch(r'/app/agent/result-sets/[^/]+', path)):
                route = '/app/agent/result-sets/{result_id}'
    diagnostic = bool(route and ((method == 'GET' and route in (
        '/app/agent/internals/health', '/app/agent/internals/health/startup', '/app/agent/internals/health/ready'))
        or (method == 'POST' and route == '/app/agent/chat/heartbeat')
        or (method == 'OPTIONS' and _preflight(scope))))
    return HTTPMetadata(method, route, diagnostic)


def suppressed(level, metadata):
    return level == 'off' and metadata is not None and metadata.diagnostic


def request_metadata(scope, *, owned=False, urlconf='api_agent.api.urls'):
    try:
        return _request_metadata(scope, owned=owned, urlconf=urlconf)
    except Exception:
        method = scope.get('method')
        return HTTPMetadata(method if type(method) is str and method in METHODS else '_OTHER')
