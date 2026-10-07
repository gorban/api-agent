"""Owner lifecycle failures drain the runtime before reporting ASGI failure."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.api.application import Application, Runtime, RuntimeLifecycleError
from api_agent.api.composition import compose_runtime
from api_agent.api.routes import RouteError, RouteRegistrar
from api_agent.config.resolve import ConfigResult
from api_agent.providers.loading import ProviderError


def test_unconfigured_provider_is_explicit_and_configured_provider_is_returned():
    with pytest.raises(ProviderError, match='provider_location_required'):
        Runtime('demo').require_llm_provider()
    implementation = object()
    assert Runtime('demo', llm_provider=SimpleNamespace(implementation=implementation)).require_llm_provider() is implementation


def test_closed_runtime_cannot_restart_synchronously_or_asynchronously():
    runtime = Runtime('demo')
    asyncio.run(runtime.aclose())
    with pytest.raises(RuntimeLifecycleError, match='runtime_closed'):
        runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_closed'):
        asyncio.run(runtime.astart())
    assert runtime.draining and not runtime.ready


def test_synchronous_route_setup_precedes_start_and_shutdown():
    calls = []
    runtime = Runtime('demo')
    def setup(registrar, services):
        assert registrar is runtime.routes
        assert services == {'sessions': None, 'emissions': None}
        calls.append('routes')
    async def start(): calls.append('start')
    async def close(): calls.append('close')
    runtime.identity_adapter = SimpleNamespace(setup_routes=setup, astart=start, aclose=close)
    async def scenario():
        await runtime.astart()
        assert runtime.ready and calls == ['routes', 'start']
        await runtime.aclose()
    asyncio.run(scenario())
    assert calls == ['routes', 'start', 'close']


def test_start_failure_drains_every_owner_and_hides_private_detail():
    calls = []
    async def start(): raise ValueError('private startup detail')
    def owner(name, **hooks):
        async def close(): calls.append(name)
        return SimpleNamespace(aclose=close, **hooks)
    runtime = Runtime('demo', sessions=owner('sessions'), identity_adapter=owner('identity', astart=start),
                      llm_provider=owner('provider'), tracing=owner('tracing'))
    async def scenario():
        with pytest.raises(RuntimeLifecycleError) as caught:
            await runtime.astart()
        assert str(caught.value) == 'runtime_start_failed'
        assert runtime.draining and not runtime.ready
        await runtime.aclose()
    asyncio.run(scenario())
    assert calls == ['sessions', 'identity', 'provider', 'tracing']


def test_cancellation_waits_for_start_task_and_owner_cleanup():
    async def scenario():
        entered, start_stopped, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def start():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                start_stopped.set()
        async def close():
            assert start_stopped.is_set()
            closed.set()
        runtime = Runtime('demo', identity_adapter=SimpleNamespace(astart=start, aclose=close))
        task = asyncio.create_task(runtime.astart())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and runtime.draining and not runtime.ready
        assert runtime._start_task.done() and runtime._close_task.done()
    asyncio.run(scenario())


def test_failed_cleanup_does_not_skip_later_owners_or_run_twice():
    calls = []
    async def bad_close():
        calls.append('failed')
        raise ValueError('private shutdown detail')
    async def good_close(): calls.append('released')
    runtime = Runtime('demo', sessions=SimpleNamespace(aclose=bad_close),
                      identity_adapter=SimpleNamespace(aclose=good_close))
    async def scenario():
        for _ in range(2):
            with pytest.raises(RuntimeLifecycleError, match='^runtime_cleanup_failed$'):
                await runtime.aclose()
    asyncio.run(scenario())
    assert calls == ['failed', 'released']


@pytest.mark.parametrize('event,hook,expected', [
    ('startup', 'astart', 'runtime_start_failed'),
    ('shutdown', 'aclose', 'runtime_cleanup_failed'),
])
def test_lifespan_reports_neutral_failures(event, hook, expected):
    async def fail(): raise ValueError('private owner detail')
    runtime = SimpleNamespace(**{hook: fail})
    app = Application(runtime, None)
    sent = []
    async def receive(): return {'type': 'lifespan.' + event}
    async def send(message): sent.append(message)
    asyncio.run(app({'type': 'lifespan'}, receive, send))
    assert sent == [{'type': f'lifespan.{event}.failed', 'message': expected}]


@pytest.mark.parametrize('values', [None, [], 'invalid',
    ConfigResult({}, {}, (), ('REQUIRED',), ()),
    ConfigResult({}, {}, (), (), ('invalid value',)),
])
def test_composition_rejects_incomplete_or_invalid_configuration(values):
    context = SimpleNamespace(pack_id='demo', identity_factory_path=None)
    with pytest.raises(ValueError, match='^invalid_runtime_configuration$'):
        compose_runtime(context, values, callback_origin='https://agent.example')


def test_composition_accepts_resolved_configuration_without_mutating_it():
    context = SimpleNamespace(pack_id='demo', identity_factory_path=None)
    values = ConfigResult({'LOG_CONTENT': True}, {}, (), (), ())
    runtime = compose_runtime(context, values, callback_origin='https://agent.example')
    assert runtime.pack_id == 'demo' and runtime.llm_provider is None
    assert runtime.sessions.callback_origin == 'https://agent.example'
    assert values.values == {'LOG_CONTENT': True}
    asyncio.run(runtime.aclose())


@pytest.mark.parametrize('path', [None, 'relative', '/bad?query', '/bad#fragment', '/bad\\path'])
def test_route_paths_are_validated(path):
    async def handler(request): pass
    with pytest.raises(RouteError, match='^invalid_provider_route$'):
        RouteRegistrar().add(path, ('GET',), handler)


@pytest.mark.parametrize('methods,handler', [((), lambda request: None),
    (('get',), lambda request: None), ((7,), lambda request: None), (('GET',), None)])
def test_route_methods_and_handlers_are_validated(methods, handler):
    with pytest.raises(RouteError, match='^invalid_provider_route$'):
        RouteRegistrar().add('/valid', methods, handler)
