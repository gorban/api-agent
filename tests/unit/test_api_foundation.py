import asyncio
import json

import pytest

from api_agent.api import Runtime, create_application


def http(app, path, method="GET", host="testserver"):
    async def run():
        sent = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            sent.append(message)

        await app(
            {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode())], "server": ("testserver", 80), "client": ("127.0.0.1", 1)},
            receive, send,
        )
        start = next(item for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        headers = {key.lower(): value for key, value in start["headers"]}
        return start["status"], headers, body

    return asyncio.run(run())


def test_lifespan_starts_and_drains_runtime():
    app = create_application(Runtime("demo"))

    async def run():
        emitted = []

        async def receive():
            if not emitted:
                return {"type": "lifespan.startup"}
            assert app.runtime.ready
            return {"type": "lifespan.shutdown"}

        async def send(message):
            emitted.append(message)

        await app({"type": "lifespan"}, receive, send)
        return emitted

    assert asyncio.run(run()) == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert app.runtime.draining and not app.runtime.ready


def test_health_and_pack_isolation():
    first = create_application(Runtime("<demo>"))
    second = create_application(Runtime("demo-pack"))
    for path in ("/app/agent/internals/health", "/app/agent/internals/health/startup", "/app/agent/internals/health/ready"):
        assert http(first, path)[0] == 503
    first.runtime.start()
    assert http(first, "/app/agent/internals/health/ready")[0] == 200
    assert http(second, "/app/agent/internals/health/ready")[0] == 503
    status, _, body = http(first, "/")
    assert status == 200 and b"&lt;demo&gt;" in body and b"<demo>" not in body
    assert b"demo-pack" in http(second, "/app/agent/")[2]
    first.runtime.stop()
    assert http(first, "/app/agent/internals/health/ready")[0] == 503


@pytest.mark.parametrize("path,method,capability", [
    ("/app/agent/models", "GET", "models"),
    ("/app/agent/chat", "POST", "chat"),
    ("/app/agent/chat/stream", "POST", "chat_stream"),
    ("/app/agent/chat/cancel", "POST", "chat_cancel"),
    ("/app/agent/chat/heartbeat", "POST", "chat_heartbeat"),
    ("/app/agent/result-sets/id", "GET", "result_set"),
    ("/app/agent/result-sets", "DELETE", "result_sets"),
])
def test_deferred_routes(path, method, capability):
    status, headers, body = http(create_application(Runtime("demo")), path, method)
    assert status == 501
    assert json.loads(body) == {"code": "not_implemented", "capability": capability}
    assert headers[b"cache-control"] == b"private, no-store"


@pytest.mark.parametrize("path", ["/app/agent/models", "/app/agent/chat", "/app/agent/chat/stream", "/app/agent/chat/cancel", "/app/agent/chat/heartbeat"])
def test_options_are_side_effect_free(path):
    app = create_application(Runtime("demo"))
    status, headers, body = http(app, path, "OPTIONS")
    assert status == 204 and not body
    assert b"OPTIONS" in headers[b"allow"]
    assert not app.runtime.started


def test_wrong_methods_and_exact_paths():
    app = create_application(Runtime("demo"))
    assert http(app, "/app/agent/models", "POST")[0] == 405
    assert http(app, "/app/agent/chat", "GET")[0] == 405
    for path in ("/app/agent/models/", "/app/agent/chat/", "/app/agent/unknown"):
        assert http(app, path)[0] == 404


def test_factory_rejects_parallel_and_foreign_configuration():
    runtime = Runtime("demo")
    with pytest.raises(ValueError):
        create_application(runtime, workers=2)
    with pytest.raises(ValueError):
        create_application(runtime, workers=True)
    with pytest.raises(ValueError):
        create_application(runtime, reload=True)
    with pytest.raises(ValueError):
        Runtime("")
    create_application(runtime)
    with pytest.raises(RuntimeError):
        create_application(runtime, allowed_hosts=("elsewhere",))


@pytest.mark.parametrize("hosts", [
    "testserver", b"testserver", (), None, ("*",), (" bad",), ("bad host",),
    ("https://example.test",), ("example.test:4200",), ("example.test/path",),
    (".example.test",), ("*.example.test",), ("example.test.",), ("bad_underscore",),
])
def test_invalid_allowed_hosts(hosts):
    with pytest.raises(ValueError):
        create_application(Runtime("demo"), allowed_hosts=hosts)


@pytest.mark.parametrize("path", [
    "/app/agent/internals/health", "/app/agent/models", "/does-not-exist",
])
def test_host_validated_before_any_route(path):
    app = create_application(Runtime("demo"))
    assert http(app, path, host="hostile.example")[0] == 400
    assert http(app, path, host="testserver:4200")[0] != 400


def test_application_rejects_unsupported_scope():
    app = create_application(Runtime("demo"))

    async def unused():
        raise AssertionError("receive must not be called")

    with pytest.raises(ValueError, match="Unsupported ASGI scope: websocket"):
        asyncio.run(app({"type": "websocket"}, unused, unused))


def test_factory_rejects_non_runtime_before_settings_access():
    with pytest.raises(TypeError, match="runtime must be a Runtime"):
        create_application(object())


def test_factory_rejects_foreign_django_settings(monkeypatch):
    from api_agent.api import application

    class ForeignSettings:
        configured = True
        API_AGENT_API_MARKER = "another-application"

    monkeypatch.setattr(application, "settings", ForeignSettings())
    monkeypatch.setattr(application, "get_asgi_application", lambda: pytest.fail("Django must not initialize"))
    with pytest.raises(RuntimeError, match="Foreign Django settings"):
        application.create_application(Runtime("demo"))


def test_lifespan_ignores_other_events_before_shutdown():
    app = create_application(Runtime("demo"))
    events = iter(({"type": "lifespan.heartbeat"}, {"type": "lifespan.shutdown"}))
    sent = []

    async def receive():
        return next(events)

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))
    assert sent == [{"type": "lifespan.shutdown.complete"}]
    assert app.runtime.draining
