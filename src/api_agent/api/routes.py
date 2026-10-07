"""Per-runtime provider routes, registered before serving requests."""
import re
from django.http import HttpResponse
from django.urls import re_path
from api_agent.api.urls import urlpatterns as base_patterns


class RouteError(ValueError):
    pass


class RouteRegistrar:
    def __init__(self):
        self._handlers = {}
        self._frozen = False

    def add(self, path, methods, handler):
        if self._frozen:
            raise RouteError('routes_already_started')
        if not isinstance(path, str) or not path.startswith('/') or any(c in path for c in ('?', '#', '\\')):
            raise RouteError('invalid_provider_route')
        methods = tuple(methods)
        if not methods or not all(isinstance(m, str) and m in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD') for m in methods) or not callable(handler):
            raise RouteError('invalid_provider_route')
        if any(pattern.pattern.match(path.lstrip('/')) for pattern in base_patterns):
            raise RouteError('provider_route_conflict')
        handlers = self._handlers.setdefault(path, {})
        if any(method in handlers for method in methods):
            raise RouteError('provider_route_conflict')
        handlers.update({method: handler for method in methods})

    def freeze(self):
        self._frozen = True
        patterns = []
        for path, handlers in self._handlers.items():
            async def dispatch(request, handlers=dict(handlers)):
                handler = handlers.get(request.method)
                if handler is None:
                    return HttpResponse(status=405, headers={'Allow': ', '.join(handlers)})
                return await handler(request)
            patterns.append(re_path('^' + re.escape(path.lstrip('/')) + '$', dispatch))
        return type('RuntimeRoutes', (), {'urlpatterns': [*patterns, *base_patterns]})
