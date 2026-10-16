"""Anonymous probes and explicit deferred public API responses."""

from functools import wraps
from html import escape

from django.http import HttpResponse, JsonResponse


def methods(*allowed):
    allow = ", ".join(allowed)

    def decorate(view):
        @wraps(view)
        async def wrapped(request, *args, **kwargs):
            capability = view.__name__
            if capability in ("models", "chat", "chat_stream"):
                backend = (_runtime(request).services or {}).get("studio_backend")
                if backend is not None:
                    return await backend.handle(request, capability)
            if request.method == "OPTIONS" and "OPTIONS" in allowed:
                response = HttpResponse(status=204)
                response["Allow"] = allow
                response["Access-Control-Allow-Methods"] = allow
                response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
                return response
            if request.method not in allowed:
                return HttpResponse(status=405, headers={"Allow": allow})
            return await view(request, *args, **kwargs)

        return wrapped

    return decorate


def _runtime(request):
    return request.scope["api_agent_runtime"]


@methods("GET")
async def landing(request):
    pack_id = escape(_runtime(request).pack_id, quote=True)
    return HttpResponse(
        f"<!doctype html><html><head><title>API Agent</title></head>"
        f"<body><h1>API Agent</h1><p>Selected pack: {pack_id}</p>"
        "<p>API capabilities are being implemented.</p></body></html>",
        content_type="text/html; charset=utf-8",
    )


@methods("GET")
async def health(request, probe="health"):
    runtime = _runtime(request)
    healthy = runtime.started if probe == "startup" else runtime.ready
    return JsonResponse(
        {"probe": probe, "status": "healthy" if healthy else "unhealthy", "pack_id": runtime.pack_id},
        status=200 if healthy else 503,
    )


@methods("GET", "OPTIONS")
async def models(request):
    return deferred("models")








@methods("POST", "OPTIONS")
async def chat(request):
    return deferred("chat")


@methods("POST", "OPTIONS")
async def chat_stream(request):
    return deferred("chat_stream")


@methods("POST", "OPTIONS")
async def chat_cancel(request):
    return deferred("chat_cancel")


@methods("POST", "OPTIONS")
async def chat_heartbeat(request):
    return deferred("chat_heartbeat")


@methods("GET")
async def result_set(request, result_id):
    return deferred("result_set")


@methods("DELETE")
async def result_sets(request):
    return deferred("result_sets")


def deferred(capability):
    return JsonResponse(
        {"code": "not_implemented", "capability": capability},
        status=501,
        headers={"Cache-Control": "private, no-store"},
    )
