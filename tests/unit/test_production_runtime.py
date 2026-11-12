"""Offline contracts for the noninteractive production ASGI owner."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _provider(tmp_path, name='provider.py'):
    path = tmp_path / name
    path.write_text('def create(config, services): return None\n')
    return str(path) + ':create'


def _lifespan_messages(app, *, watchdog_seconds=None):
    sent, events = [], iter((
        {'type': 'lifespan.startup'},
        {'type': 'lifespan.shutdown'},
    ))

    async def receive():
        return next(events)

    async def send(message):
        sent.append(message)

    async def run():
        await app({'type': 'lifespan'}, receive, send)

    if watchdog_seconds is None:
        asyncio.run(run())
    else:
        asyncio.run(asyncio.wait_for(run(), timeout=watchdog_seconds))
    return sent


def test_loads_compiled_plan_once_at_startup_and_delegates_http(monkeypatch, tmp_path):
    from api_agent.runtime import production

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    settings = production.ProductionSettings(
        plan=plan, pack_id='demo', owner_root=tmp_path, listen_host='127.0.0.1',
        listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000', session_nonce='n' * 32,
        values={'LLM_PROVIDER': 'test:provider', 'NO_STD_IN': True, 'DEBUG': False,
                'RUN_UI': False},
        shutdown_timeout=0.1,
        allowed_hosts=('api.example.test',),
        permitted_browser_origins=('https://console.example.test',),
        client_address_policy='any',
    )
    definition, graph = object(), SimpleNamespace()
    load = Mock(return_value=definition)
    compose = Mock(return_value=SimpleNamespace(runtime=SimpleNamespace(
        astart=AsyncMock(), aclose=AsyncMock(), started=True, ready=True)))
    delegated = []

    async def http(scope, receive, send):
        delegated.append(scope['path'])
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'composed'})

    monkeypatch.setattr(production, 'load_artifact', load)
    monkeypatch.setattr(production, 'selected_application', Mock(return_value=graph))
    monkeypatch.setattr(production, 'compose_chat_runtime', compose)
    monkeypatch.setattr(production, 'create_application', Mock(return_value=http))

    app = production.ProductionApplication(settings, llm_factory=Mock())

    assert load.call_count == 0
    asyncio.run(app._start())
    assert load.call_count == 1
    asyncio.run(app._start())
    assert load.call_count == 1
    assert compose.call_args.kwargs['graph'] is graph
    assert compose.call_args.kwargs['required_kinds'] == ()
    assert 'requires_api' not in compose.call_args.kwargs
    assert compose.call_args.kwargs['extra_services'] is None
    assert compose.call_args.kwargs['permitted_browser_origins'] == ('https://console.example.test',)
    assert compose.call_args.kwargs['client_address_policy'] == 'any'
    assert production.create_application.call_args.kwargs['allowed_hosts'] == ('api.example.test',)
    assert compose.return_value.runtime.astart.await_count == 1

    sent = []
    async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def send(message): sent.append(message)
    asyncio.run(app({'type': 'http', 'path': '/app/agent/chat'}, receive, send))

    assert delegated == ['/app/agent/chat']
    assert sent[0]['status'] == 200
    asyncio.run(app._shutdown())
    assert compose.return_value.runtime.aclose.await_count == 1


def test_shutdown_is_bounded_when_cleanup_hangs(monkeypatch, tmp_path):
    from api_agent.runtime import production

    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000',
        session_nonce='n' * 32, values={'LLM_PROVIDER': 'test:provider'},
        shutdown_timeout=0.001,
    )
    settings.plan.write_bytes(b'compiled')

    async def hang():
        await asyncio.Event().wait()

    runtime = SimpleNamespace(astart=AsyncMock(), aclose=hang, started=True, ready=True)
    monkeypatch.setattr(production, 'load_artifact', Mock(return_value=object()))
    monkeypatch.setattr(production, 'selected_application', Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(production, 'compose_chat_runtime',
                        Mock(return_value=SimpleNamespace(runtime=runtime)))
    monkeypatch.setattr(production, 'create_application', Mock(return_value=AsyncMock()))

    sent = _lifespan_messages(
        production.ProductionApplication(settings, llm_factory=Mock()), watchdog_seconds=.1)

    assert sent == [
        {'type': 'lifespan.startup.complete'},
        {'type': 'lifespan.shutdown.failed', 'message': 'runtime_shutdown_timed_out'},
    ]


@pytest.mark.parametrize('name', ['DEBUG', 'RUN_UI'])
def test_environment_settings_ignore_development_startup_switches(monkeypatch, tmp_path, name):
    from api_agent.runtime.production import settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    monkeypatch.setenv('API_AGENT_PRODUCTION_PLAN', str(plan))
    monkeypatch.setenv('API_AGENT_LLM_PROVIDER', _provider(tmp_path))
    monkeypatch.setenv('API_AGENT_CHAT_ORIGIN', 'http://localhost:3000')
    monkeypatch.setenv('API_AGENT_SESSION_NONCE', 'n' * 32)
    monkeypatch.setenv(name, 'true')

    settings = settings_from_environment()

    assert settings.values['DEBUG'] is False
    assert settings.values['RUN_UI'] is False


def test_environment_settings_are_explicit_loopback_and_secret_free(monkeypatch, tmp_path):
    from api_agent.runtime.production import settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    env = {
        'API_AGENT_PRODUCTION_PLAN': str(plan),
        'API_AGENT_LLM_PROVIDER': _provider(tmp_path),
        'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32,
        'API_AGENT_LLM_MAX_OUTPUT_TOKENS': '12',
        'API_AGENT_SHUTDOWN_TIMEOUT_SECONDS': '1.5',
    }
    settings = settings_from_environment(env)

    assert settings.listen_host == '127.0.0.1'
    assert settings.listen_port == 8080
    assert settings.values['LLM_PROVIDER'] == env['API_AGENT_LLM_PROVIDER']
    assert settings.values['API_PROVIDER'] == ''
    assert settings.values['LLM_MAX_OUTPUT_TOKENS'] == 12
    assert settings.values['NO_STD_IN'] is True
    assert settings.values['DEBUG'] is False
    assert settings.values['RUN_UI'] is False
    assert settings.values['UI_PROVIDER'] == ''
    assert settings.values['TRACE_PROVIDER'] == ''
    assert settings.config.fields['LLM_PROVIDER'].source == 'environment'
    assert settings.config.fields['LLM_PROVIDER'].explicit is True
    env['API_AGENT_LLM_DEFAULT_MODEL'] = 'selected'
    assert settings_from_environment(env).values['LLM_DEFAULT_MODEL'] == 'selected'
    env['TRACE_EXPORT_ENDPOINT'] = 'http://127.0.0.1:1/v1/traces'
    traced = settings_from_environment(env).values
    assert traced['TRACE_PROVIDER'].endswith('trace_adapters/otel/provider.py:create_tracing_provider')
    assert traced['TRACE_EXPORT_ENDPOINT'] == 'http://127.0.0.1:1/v1/traces'
    resolved = settings_from_environment(env).config
    assert resolved.fields['TRACE_PROVIDER'].explicit is True
    assert resolved.fields['TRACE_EXPORT_ENDPOINT'].explicit is True
    assert resolved.values['TRACE_MODE'] == 'OTEL'


def test_environment_settings_allow_explicit_service_ingress_configuration(tmp_path):
    from api_agent.runtime.production import settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    env = {
        'API_AGENT_PRODUCTION_PLAN': str(plan),
        'API_AGENT_LLM_PROVIDER': _provider(tmp_path),
        'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32,
        'API_AGENT_LISTEN_HOST': '0.0.0.0',
        'API_AGENT_ALLOWED_HOSTS': 'api.example.test',
        'API_AGENT_PERMITTED_BROWSER_ORIGINS': 'https://console.example.test',
        'API_AGENT_CLIENT_ADDRESS_POLICY': 'any',
    }

    settings = settings_from_environment(env)

    assert settings.listen_host == '0.0.0.0'
    assert settings.allowed_hosts == ('api.example.test',)
    assert settings.permitted_browser_origins == ('https://console.example.test',)
    assert settings.client_address_policy == 'any'
    for name in ('API_AGENT_LISTEN_HOST', 'API_AGENT_ALLOWED_HOSTS',
                 'API_AGENT_PERMITTED_BROWSER_ORIGINS', 'API_AGENT_CLIENT_ADDRESS_POLICY'):
        assert settings.config.fields[name].explicit is True


@pytest.mark.parametrize(('call', 'message'), [
    (lambda production: production._csv('', 'TEST'), 'nonempty'),
    (lambda production: production._csv('one, one', 'TEST'), 'distinct'),
    (lambda production: production._allowed_hosts('*'), 'ALLOWED_HOSTS'),
    (lambda production: production._browser_origins('https://example.test:bad'), 'invalid origin'),
    (lambda production: production._browser_origins('ftp://example.test'), 'invalid origin'),
    (lambda production: production._browser_origins('https://bad_host'), 'invalid origin'),
    (lambda production: production._browser_origins('https://example.test,https://example.test:443'), 'duplicate'),
    (lambda production: production._integer('bad', 'TEST', low=1, high=2), 'integer'),
    (lambda production: production._integer('3', 'TEST', low=1, high=2), 'range'),
])
def test_network_configuration_parsers_fail_closed(call, message):
    from api_agent.runtime import production

    with pytest.raises(production.ProductionConfigurationError, match=message):
        call(production)


def test_tracing_environment_is_resolved_with_typed_export_settings(tmp_path):
    from api_agent.runtime.production import ProductionConfigurationError, settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    env = {
        'API_AGENT_PRODUCTION_PLAN': str(plan),
        'API_AGENT_LLM_PROVIDER': _provider(tmp_path),
        'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32,
        'TRACE_EXPORT_ENDPOINT': 'http://127.0.0.1:1/v1/traces',
        'TRACE_EXPORT_HEADERS': 'x-key=value',
        'TRACE_EXPORT_PROTOCOL': 'http/protobuf',
        'TRACE_HTTP_DIAGNOSTICS_LEVEL': 'info',
    }

    settings = settings_from_environment(env)

    assert settings.config.fields['TRACE_EXPORT_HEADERS'].explicit is True
    assert settings.config.fields['TRACE_EXPORT_PROTOCOL'].explicit is True
    assert settings.config.values['TRACE_HTTP_DIAGNOSTICS_LEVEL'] == 'info'
    env['TRACE_PROVIDER'] = settings.values['TRACE_PROVIDER']
    del env['TRACE_EXPORT_ENDPOINT']
    with pytest.raises(ProductionConfigurationError, match='TRACE_EXPORT_ENDPOINT'):
        settings_from_environment(env)
    env['TRACE_EXPORT_ENDPOINT'] = 'http://127.0.0.1:1/v1/traces'
    env['TRACE_EXPORT_PROTOCOL'] = 'unsupported'
    with pytest.raises(ProductionConfigurationError, match='TRACE_EXPORT_PROTOCOL'):
        settings_from_environment(env)


@pytest.mark.parametrize(('changes', 'message'), [
    ({'API_AGENT_PRODUCTION_PLAN': ''}, 'API_AGENT_PRODUCTION_PLAN is required'),
    ({'API_AGENT_PRODUCTION_PLAN': '/missing'}, 'readable compiled plan'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_LISTEN_HOST': 'not-an-address'}, 'IP address'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_LISTEN_PORT': 'bad'}, 'integer'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_LISTEN_PORT': '0'}, 'positive integer'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_ALLOWED_HOSTS': '*'}, 'ALLOWED_HOSTS'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_PERMITTED_BROWSER_ORIGINS': '*'}, 'invalid origin'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_LLM_MAX_OUTPUT_TOKENS': '0'}, 'positive'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_SHUTDOWN_TIMEOUT_SECONDS': '0'}, 'range'),
    ({'API_AGENT_PRODUCTION_PLAN': 'x', 'API_AGENT_SHUTDOWN_TIMEOUT_SECONDS': 'bad'}, 'number'),
])
def test_environment_settings_reject_invalid_explicit_configuration(tmp_path, changes, message):
    from api_agent.runtime.production import ProductionConfigurationError, settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    env = {
        'API_AGENT_PRODUCTION_PLAN': str(plan),
        'API_AGENT_LLM_PROVIDER': _provider(tmp_path),
        'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32,
    }
    for key, value in changes.items():
        env[key] = str(plan) if value == 'x' else value

    with pytest.raises(ProductionConfigurationError, match=message):
        settings_from_environment(env)


def test_environment_settings_reject_implicit_or_loopback_only_non_loopback_deployments(tmp_path):
    from api_agent.runtime.production import ProductionConfigurationError, settings_from_environment

    plan = tmp_path / 'selected.json'
    plan.write_bytes(b'compiled')
    env = {
        'API_AGENT_PRODUCTION_PLAN': str(plan),
        'API_AGENT_LLM_PROVIDER': _provider(tmp_path),
        'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32,
        'API_AGENT_LISTEN_HOST': '0.0.0.0',
    }

    with pytest.raises(ProductionConfigurationError, match='non-loopback'):
        settings_from_environment(env)

    env.update(
        API_AGENT_ALLOWED_HOSTS='api.example.test',
        API_AGENT_PERMITTED_BROWSER_ORIGINS='https://console.example.test',
    )
    with pytest.raises(ProductionConfigurationError, match='CLIENT_ADDRESS_POLICY=any'):
        settings_from_environment(env)


def test_start_failure_and_unavailable_requests_are_bounded(monkeypatch, tmp_path):
    from api_agent.runtime import production

    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000',
        session_nonce='n' * 32, values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=1,
    )
    settings.plan.write_bytes(b'compiled')
    monkeypatch.setattr(production, 'load_artifact', Mock(side_effect=RuntimeError('bad plan')))
    app = production.ProductionApplication(settings)
    sent = _lifespan_messages(app)
    assert sent == [{'type': 'lifespan.startup.failed', 'message': 'runtime_start_failed'}]

    sent = []
    async def receive(): return {'type': 'http.request'}
    async def send(message): sent.append(message)
    asyncio.run(app({'type': 'http', 'path': '/app/agent/chat'}, receive, send))
    assert sent[0]['status'] == 503


def test_start_failure_logs_its_category_and_location_without_the_message(monkeypatch, tmp_path, caplog):
    from api_agent.providers.contracts import ContractError
    from api_agent.runtime import production
    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000',
        session_nonce='n' * 32, values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=1,
    )
    settings.plan.write_bytes(b'compiled')
    monkeypatch.setattr(production, 'load_artifact', lambda _: object())
    monkeypatch.setattr(production, 'selected_application', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(production, 'compose_chat_runtime',
                        Mock(side_effect=ContractError('token=secret-value')))

    with caplog.at_level('ERROR', logger='api_agent.runtime.production'):
        sent = _lifespan_messages(production.ProductionApplication(settings))

    assert sent == [{'type': 'lifespan.startup.failed', 'message': 'runtime_start_failed'}]
    assert [record.name for record in caplog.records] == ['api_agent.runtime.production']
    logged = caplog.records[0].getMessage()
    assert logged.startswith('Runtime startup failure category: contract\n')
    assert 'api_agent/runtime/production.py:' in logged
    assert 'secret-value' not in logged


def test_generic_shutdown_failure_and_factory_cli(monkeypatch, tmp_path):
    from api_agent.runtime import production

    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000',
        session_nonce='n' * 32, values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=1,
    )
    settings.plan.write_bytes(b'compiled')
    runtime = SimpleNamespace(astart=AsyncMock(), aclose=AsyncMock(side_effect=RuntimeError('close')),
                              started=True, ready=True)
    monkeypatch.setattr(production, 'load_artifact', Mock(return_value=object()))
    monkeypatch.setattr(production, 'selected_application', Mock(return_value=SimpleNamespace()))
    composed = SimpleNamespace(runtime=runtime)
    compose = Mock(return_value=composed)
    monkeypatch.setattr(production, 'compose_chat_runtime', compose)
    monkeypatch.setattr(production, 'create_application', Mock(return_value=AsyncMock()))
    assert _lifespan_messages(production.ProductionApplication(settings)) == [
        {'type': 'lifespan.startup.complete'},
        {'type': 'lifespan.shutdown.failed', 'message': 'runtime_shutdown_failed'},
    ]
    assert 'requires_api' not in compose.call_args.kwargs

    monkeypatch.setattr(production, 'settings_from_environment', Mock(return_value=settings))
    assert isinstance(production.application(), production.ProductionApplication)
    uvicorn = SimpleNamespace(run=Mock())
    monkeypatch.setitem(sys.modules, 'uvicorn', uvicorn)
    assert production.main([]) == 0
    assert uvicorn.run.call_args.kwargs == {
        'host': '127.0.0.1', 'port': 8080, 'workers': 1, 'reload': False, 'lifespan': 'on',
        'timeout_graceful_shutdown': 1,
    }


def test_shutdown_without_start_and_unknown_lifespan_event(monkeypatch, tmp_path):
    from api_agent.runtime import production

    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000',
        session_nonce='n' * 32, values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=1,
    )
    app = production.ProductionApplication(settings)
    asyncio.run(app._shutdown())

    events = iter(({'type': 'lifespan.unknown'}, {'type': 'lifespan.shutdown'}))
    sent = []
    async def receive(): return next(events)
    async def send(message): sent.append(message)
    asyncio.run(app({'type': 'lifespan'}, receive, send))
    assert sent == [{'type': 'lifespan.shutdown.complete'}]


def test_shutdown_uses_the_configured_drain_bound(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from api_agent.runtime import production

    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000', session_nonce='n' * 32,
        values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=7.5,
    )
    app = production.ProductionApplication(settings)
    app._runtime = SimpleNamespace(aclose=AsyncMock())
    observed = []

    @asynccontextmanager
    async def timeout(value):
        observed.append(value)
        yield

    monkeypatch.setattr(production.asyncio, 'timeout', timeout)
    asyncio.run(app._shutdown())

    assert observed == [7.5]
