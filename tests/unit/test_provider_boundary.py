"""Synthetic configured providers; no upstream authentication or browser use."""
import asyncio
import sys
from types import SimpleNamespace
import pytest
from api_agent.auth.session import SessionStore, SessionError
from api_agent.providers.loading import load_provider, provider_schema, ProviderError
from api_agent.api.composition import compose_runtime
from api_agent.api.routes import RouteRegistrar, RouteError
from api_agent.config import load_shared_schema, resolve_config


def factory(tmp_path, body=None):
    path = tmp_path / 'provider.py'
    path.write_text(body or '''class Adapter:
    version = 1
    def __init__(self, config, services):
        self.config = config
        self.services = services
        self.calls = []
    async def astart(self): self.calls.append('start')
    async def aclose(self): self.calls.append('close')
    async def setup_routes(self, registrar, services):
        self.calls.append('routes')
        async def status(request):
            from django.http import JsonResponse
            return JsonResponse({'ready': True})
        registrar.add('/provider/status', ('GET',), status)
def create(config, services): return Adapter(config, services)
''')
    return str(path) + ':create'


def test_provider_location_is_required_and_errors_are_private(tmp_path):
    with pytest.raises(ProviderError, match='location_required'):
        load_provider('', {}, {})
    before = set(sys.modules)
    location = factory(tmp_path, "def create(config, services): raise ValueError('secret-value')\n")
    with pytest.raises(ProviderError) as caught:
        load_provider(location, {}, {})
    assert 'secret-value' not in str(caught.value)
    assert set(sys.modules) == before


def test_provider_config_is_combined_before_factory_import(tmp_path):
    location = factory(tmp_path, "raise AssertionError('do not import during schema loading')\n")
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/config.toml').write_text('version = 1\n[fields.PROVIDER_LIMIT]\ntype = "int"\ndefault = 7\n')
    schema = provider_schema(location, load_shared_schema())
    result = resolve_config(schema, environ={'TUI_MODE': 'unit'})
    assert result.values['PROVIDER_LIMIT'] == 7


def test_lifecycle_and_routes_are_runtime_owned(tmp_path):
    context = SimpleNamespace(pack_id='demo', identity_factory_path=None)
    runtime = compose_runtime(context, {'LLM_PROVIDER': factory(tmp_path)}, callback_origin='https://agent.example', required_kinds={'llm'})
    assert runtime.llm_provider is None
    async def scenario():
        await runtime.astart()
        owner = runtime.llm_provider.implementation
        await runtime.astart()
        assert owner.calls == ['routes', 'start']
        assert runtime.ready
        with pytest.raises(RouteError, match='already_started'):
            runtime.routes.add('/late', ('GET',), lambda request: None)
        await runtime.aclose()
        await runtime.aclose()
        assert owner.calls == ['routes', 'start', 'close']
    asyncio.run(scenario())
    assert runtime.llm_provider.module_name not in sys.modules


def test_route_collisions_do_not_replace_existing_handlers():
    registrar = RouteRegistrar()
    async def handler(request): pass
    asyncio.run(handler(None))
    registrar.add('/provider/test', ('GET',), handler)
    with pytest.raises(RouteError, match='conflict'):
        registrar.add('/provider/test', ('GET',), handler)
    with pytest.raises(RouteError, match='conflict'):
        registrar.add('/app/agent/chat', ('POST',), handler)


def test_registered_routes_dispatch_with_runtime_isolation(tmp_path):
    import json
    from api_agent.api import create_application
    from test_api_foundation import http
    context = SimpleNamespace(pack_id='demo', identity_factory_path=None)
    first = compose_runtime(context, {'LLM_PROVIDER': factory(tmp_path)}, callback_origin='https://agent.example', required_kinds={'llm'})
    second = compose_runtime(context, {}, callback_origin='https://agent.example')
    first_app, second_app = create_application(first), create_application(second)
    async def start():
        await first.astart()
        await second.astart()
    from unittest.mock import patch
    with asyncio.Runner() as runner:
        runner.run(start())
        try:
            with patch('asyncio.run', runner.run):
                status, headers, body = http(first_app, '/provider/status')
                assert status == 200 and json.loads(body) == {'ready': True}
                assert http(second_app, '/provider/status')[0] == 404
                status, headers, body = http(first_app, '/provider/status', method='POST')
                assert status == 405 and headers[b'allow'] == b'GET'
        finally:
            async def close():
                await first.aclose()
                await second.aclose()
            runner.run(close())


def test_request_sessions_do_not_own_provider_token_state():
    now = [0]
    store = SessionStore('demo', 'https://agent.example', clock=lambda: now[0], session_ttl=3, max_sessions=1)
    opened = store.open(None, 'first', create=True)
    assert store.open(opened.cookie_value, 'first').handle is opened.handle
    with pytest.raises(SessionError, match='owner_mismatch'):
        store.open(opened.cookie_value, 'second')
    with pytest.raises(SessionError, match='capacity'):
        store.open(None, 'second', create=True)
    other = SessionStore('other', 'https://agent.example')
    with pytest.raises(SessionError, match='invalid_session'):
        other.open(opened.cookie_value, 'first')
    now[0] = 4
    with pytest.raises(SessionError, match='invalid_session'):
        store.open(opened.cookie_value, 'first')
    asyncio.run(store.aclose())
    with pytest.raises(SessionError, match='store_closed'):
        store.open(None, None, create=True)
    assert not hasattr(store, 'begin')


def test_config_session_bootstraps_provider_fields_and_cli_precedence(tmp_path):
    from api_agent.config.session import ConfigSession
    location = factory(tmp_path, "raise AssertionError('configuration must not import factory')\n")
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/config.toml').write_text('version = 1\n[fields.PROVIDER_LIMIT]\ntype = "int"\ndefault = 7\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False,
                              dotenv_path=tmp_path / '.env', anchors={'owner_root': tmp_path})
    session = ConfigSession(context, load_shared_schema(),
        argv=['--llm-provider', location, '--provider-limit', '9', '--tui-mode', 'unit'],
        environ={'LLM_PROVIDER': '/missing.py:create'})
    assert 'PROVIDER_LIMIT' in session.schema.fields
    result = session.result()
    result.raise_for_errors()
    assert result.values['PROVIDER_LIMIT'] == 9
