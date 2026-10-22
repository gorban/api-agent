"""Unauthenticated local status projection with no provider or credential access."""
import html
import json
import re
from itertools import islice

from api_agent.runtime.infrastructure_contract import MAX_SERVICES
from urllib.parse import urlsplit

_STATES = frozenset({'disabled', 'starting', 'ready', 'degraded', 'unavailable'})
_COMPONENTS = ('agent', 'ui', 'tracing', 'flow')
_DESTINATIONS = ('ui', 'tracing', 'flow')
_ID = r'[a-z][a-z0-9_-]{0,63}'
_SERVICE = re.compile(rf'service:({_ID})/({_ID})\Z')
_SERVICE_ENDPOINT = re.compile(rf'(service:{_ID}/{_ID}):({_ID})\Z')
_MAX_ENDPOINTS = 16


def safe_destination(value):
    """Accept an explicitly verified destination without credential-bearing parts."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0):
            return None
    except ValueError:
        return None
    return value


def project_status(snapshot):
    """Only caller-verified links and fixed component states cross this boundary."""
    components = snapshot.get('components', {})
    destinations = snapshot.get('destinations', {})
    states = {name: components.get(name) if components.get(name) in _STATES else 'unavailable'
              for name in _COMPONENTS}
    links = {name: url for name in _DESTINATIONS
             if (url := safe_destination(destinations.get(name))) is not None}
    for name, state in islice(components.items(), MAX_SERVICES + len(_COMPONENTS)):
        if (type(name) is str and _SERVICE.fullmatch(name) is not None
                and state in _STATES and len(states) < MAX_SERVICES + len(_COMPONENTS)):
            states[name] = state
    counts = {}
    for name, value in islice(destinations.items(), MAX_SERVICES * _MAX_ENDPOINTS + len(_DESTINATIONS)):
        match = _SERVICE_ENDPOINT.fullmatch(name) if type(name) is str else None
        if match is not None and match[1] in states:
            service = match[1]
            url = safe_destination(value)
            if url is not None and counts.get(service, 0) < _MAX_ENDPOINTS:
                links[name] = url
                counts[service] = counts.get(service, 0) + 1
    return {'application': 'Agent', 'components': states, 'destinations': links}


class LocalStatusApplication:
    """Wrap the actual application; status reads never invoke its authentication."""
    def __init__(self, application, snapshot):
        self.application, self.snapshot = application, snapshot

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') not in {'/', '/local-status.json'}:
            return await self.application(scope, receive, send)
        from api_agent.api.application import Application
        if isinstance(self.application, Application):
            from django.core.handlers.asgi import ASGIRequest
            from django.core.exceptions import DisallowedHost
            from io import BytesIO
            try:
                ASGIRequest(scope, BytesIO()).get_host()
            except DisallowedHost:
                await send({'type': 'http.response.start', 'status': 400, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'Invalid host'})
                return
        if scope.get('method') != 'GET':
            status, content_type, body = 405, 'text/plain', b'Method not allowed'
        else:
            try:
                projected = project_status(self.snapshot())
            except Exception:
                projected = project_status({})
            if scope['path'] == '/local-status.json':
                content_type = 'application/json'
                body = json.dumps(projected, separators=(',', ':')).encode()
            else:
                content_type = 'text/html; charset=utf-8'
                rows = ''.join(f'<li>{html.escape(name)}: {html.escape(state)}</li>' for name, state in projected['components'].items())
                links = ''.join(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a></li>'
                                for name, url in projected['destinations'].items())
                body = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                        '<meta http-equiv="refresh" content="2"><title>Agent home</title><h1>Agent home</h1><p>Local application status</p>'
                        f'<ul>{rows}</ul><ul>{links}</ul>'
                        '<p><a href="/local-status.json">Current status</a></p></html>').encode()
            status = 200
        await send({'type': 'http.response.start', 'status': status, 'headers': [
            (b'content-type', content_type.encode()), (b'content-length', str(len(body)).encode()),
            (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
            (b'content-security-policy', b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'")]})
        await send({'type': 'http.response.body', 'body': body})
