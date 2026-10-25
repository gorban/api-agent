"""Bundled API selection and synthetic target behavior."""
import importlib.util
from dataclasses import replace
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue, resolve_config
from api_agent.runtime.demo_api import bind_demo_api_endpoint, prepare_demo_api
from api_agent.config.declarations import ConfigSchema, FieldDeclaration, compose_schema, load_declarations, load_shared_schema
from api_agent.config.dotenv import read_dotenv
from api_agent.config.session import ConfigSession
from api_agent.providers.loading import provider_location
from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions
from api_agent.runtime.infrastructure_plan import plan_infrastructure
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.resource_pack import load_launch_context


DEMO = Path(__file__).resolve().parents[2] / 'demo'


def test_bundled_endpoint_review_and_clear_env_switches_to_remote(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'base/demo'
    base.mkdir()
    pack.mkdir()
    shared = load_shared_schema(DEMO.parent)
    schema = compose_schema(shared, load_declarations(DEMO / 'simple/cfg/config.toml',
        base_schema=shared, source='pack_default'))
    context = replace(load_launch_context(DEMO.parent), base_root=base, pack_root=pack,
        owner_root=base, launcher_root=base, shared_dotenv_paths=(), workflow_base_root=DEMO.parent)
    session = ConfigSession(context, schema, environ={})

    assert session.result().values['API_ENDPOINT'] == ''
    assert 'API_ENDPOINT' in {candidate.name for candidate in session.default_candidates()}
    assert not next(row for row in session.cleanup_rows() if row.name == 'API_ENDPOINT').present
    from api_agent.tooling.services import selected_contributions, tool_operation_values
    local_config = tool_operation_values(session.result().tool_plan, session.result())
    assert local_config.values['DEMO_API_LOCAL'] is True
    assert [service.id for service in selected_contributions(local_config.tool_plan)] == ['demo/api']

    session.save_defaults(['API_ENDPOINT'])
    assert read_dotenv(base / '.env')['API_ENDPOINT'] == ''
    blank_row = next(row for row in session.cleanup_rows() if row.name == 'API_ENDPOINT')
    assert blank_row.present and blank_row.base_present

    remote = 'https://example.test/graphql'
    session.set_value('API_ENDPOINT', remote, persist=True)
    assert read_dotenv(base / '.env')['API_ENDPOINT'] == remote
    assert session.result().values['API_ENDPOINT'] == remote
    row = next(row for row in session.cleanup_rows() if row.name == 'API_ENDPOINT')
    assert row.present and row.base_present
    remote_config = tool_operation_values(session.result().tool_plan, session.result())
    assert remote_config.values['DEMO_API_LOCAL'] is False
    assert not selected_contributions(remote_config.tool_plan)

    session.delete_keys(['API_ENDPOINT'])
    assert 'API_ENDPOINT' not in read_dotenv(base / '.env')
    assert session.result().values['API_ENDPOINT'] == ''
    assert 'API_ENDPOINT' in {candidate.name for candidate in session.default_candidates()}
    local_config = tool_operation_values(session.result().tool_plan, session.result())
    assert local_config.values['DEMO_API_LOCAL'] is True


def test_demo_named_tool_resolves_to_captured_bundled_factory():
    from api_agent.tooling.selection import compose_tools
    base = DEMO.parent
    shared = load_shared_schema(base)
    schema = compose_schema(shared, load_declarations(DEMO / 'simple/cfg/config.toml',
        base_schema=shared, source='pack_default'))

    selected = compose_tools(load_launch_context(base), schema, environ={}).tools[0]

    assert selected.factory == base / 'src/api_agent/api_providers/graphql_mcp/factory.py'
    assert selected.factory_name == 'create_tool'
    assert selected.id == 'demo/primary'
    assert selected.admission.sources['factory.py'] == selected.factory_bytes
    assert 'API_PROVIDER' not in selected.bindings.values()


def test_external_copy_uses_its_explicit_local_implementation(tmp_path):
    import shutil
    from api_agent.tooling.selection import compose_tools
    base, pack = tmp_path / 'base', tmp_path / 'base/demo'
    shutil.copytree(DEMO, pack)
    descriptor = pack / 'simple/cfg/tools/primary.toml'
    descriptor.write_text(descriptor.read_text().replace(
        'package:api-agent:api_agent/api_providers/graphql_mcp/tool.toml', 'implementation.toml'))
    implementation = descriptor.parent / 'implementation.toml'
    implementation.write_text('version=1\ninterface="verified-read@1"\nfactory="factory.py:create_tool"\n'
        '[inputs.ENDPOINT]\ntype="str"\nrequired=true\n')
    factory = descriptor.parent / 'factory.py'
    factory.write_text('def create_tool(config, services): raise AssertionError("discovery imported factory")\n')
    context = replace(load_launch_context(DEMO.parent), base_root=base, pack_root=pack,
        owner_root=base, workflow_entry=pack / 'simple/flow.md.html', tool_catalog_path=None)
    shared = load_shared_schema(DEMO.parent)
    schema = compose_schema(shared, load_declarations(pack / 'simple/cfg/config.toml',
        base_schema=shared, source='pack_default'))

    selected = compose_tools(context, schema, environ={}).tools[0]

    assert selected.factory == factory
    assert selected.factory_name == 'create_tool'
    assert selected.admission.sources['factory.py'] == factory.read_bytes()


@pytest.mark.parametrize('present', [False, True])
def test_installed_tool_asset_requires_its_own_receipt_without_source_fallback(tmp_path, monkeypatch, present):
    import base64
    from hashlib import sha256
    from importlib.metadata import FileHash, PackagePath
    from api_agent.tooling.declarations import Capture
    from api_agent.tooling.models import ToolSelectionError
    from api_agent.tooling import declarations
    site = tmp_path / 'site-packages'
    selected = site / 'sample_tools/implementation.toml'
    selected.parent.mkdir(parents=True)
    body = b'version=1\n'
    if present:
        selected.write_bytes(body)
    entry = PackagePath('sample_tools/implementation.toml')
    entry.hash = FileHash('sha256=' + base64.urlsafe_b64encode(sha256(body).digest()).decode().rstrip('='))
    entry.size = len(body)
    distribution = SimpleNamespace(metadata={'Name': 'example-tools'}, files=[entry],
        read_text=lambda name: 'controlled receipt', locate_file=lambda path: site / str(path))
    monkeypatch.setattr(declarations.metadata, 'distributions', lambda **kwargs: [distribution])
    capture = Capture({'base': tmp_path / 'base'})
    target = capture.path(tmp_path / 'base/instance.toml',
                          'package:example-tools:sample_tools/implementation.toml')

    if present:
        assert capture.read(target) == body
    else:
        with pytest.raises(ToolSelectionError, match='Cannot capture'):
            capture.read(target)


def config(endpoint='', *, source='pack_default', explicit=False, activation_explicit=False):
    return ConfigResult({'API_ENDPOINT': endpoint, 'DEMO_API_LOCAL': False},
                        {'API_ENDPOINT': ResolvedValue(endpoint, source, explicit),
                         'DEMO_API_LOCAL': ResolvedValue(False, 'pack_default', activation_explicit)},
                        (), (), ())


@pytest.mark.parametrize(('endpoint', 'requires_api', 'expected'), [
    ('', True, True), ('', False, False), ('https://example.test/graphql', True, False),
])
def test_demo_activation_is_operation_scoped(endpoint, requires_api, expected):
    original = config(endpoint, explicit=bool(endpoint))
    context = SimpleNamespace(pack_id='demo', external_pack=False)

    prepared, selected = prepare_demo_api(original, context, requires_api=requires_api)

    assert selected is expected
    assert prepared.values['DEMO_API_LOCAL'] is expected
    assert prepared.fields['API_ENDPOINT'] == original.fields['API_ENDPOINT']
    assert original.values['DEMO_API_LOCAL'] is False


def test_external_pack_cannot_activate_bundled_api():
    original = config()

    prepared, selected = prepare_demo_api(original, SimpleNamespace(pack_id='demo', external_pack=True), requires_api=True)

    assert prepared is original
    assert selected is False


@pytest.mark.parametrize(('bad', 'requires_api', 'message'), [
    (None, True, 'invalid_demo_api_configuration'),
    (config(), 'yes', 'invalid_demo_api_operation'),
    (config(7), True, 'invalid_demo_api_endpoint'),
])
def test_selection_rejects_invalid_configuration(bad, requires_api, message):
    with pytest.raises(ValueError, match=message):
        prepare_demo_api(bad, SimpleNamespace(pack_id='demo', external_pack=False), requires_api=requires_api)


def test_user_cannot_force_internal_activation():
    with pytest.raises(ValueError, match='operation_owned'):
        prepare_demo_api(config(activation_explicit=True), SimpleNamespace(pack_id='demo', external_pack=False), requires_api=True)


def test_endpoint_binding_requires_acquired_output():
    prepared, _ = prepare_demo_api(config(), SimpleNamespace(pack_id='demo', external_pack=False), requires_api=True)
    empty = SimpleNamespace(endpoints={})
    with pytest.raises(ValueError, match='not_ready'):
        bind_demo_api_endpoint(prepared, empty)
    output = SimpleNamespace(name='graphql', host='http://127.0.0.1:58092')

    effective = bind_demo_api_endpoint(prepared, SimpleNamespace(endpoints={'demo/api': (output,)}))

    assert effective.values['API_ENDPOINT'] == 'http://127.0.0.1:58092/graphql'
    assert effective.fields['API_ENDPOINT'].source == 'infrastructure:demo/api/graphql'
    assert prepared.fields['API_ENDPOINT'].source == 'pack_default'


def test_binding_skips_remote_and_rejects_missing_named_output():
    remote, _ = prepare_demo_api(config('https://example.test/graphql'),
                                 SimpleNamespace(pack_id='demo', external_pack=False), requires_api=True)
    assert bind_demo_api_endpoint(remote, SimpleNamespace(endpoints={})) is remote
    local, _ = prepare_demo_api(config(), SimpleNamespace(pack_id='demo', external_pack=False), requires_api=True)
    wrong = SimpleNamespace(name='other', host='http://127.0.0.1:58092')
    with pytest.raises(ValueError, match='not_ready'):
        bind_demo_api_endpoint(local, SimpleNamespace(endpoints={'demo/api': (wrong,)}))


def test_bundled_declaration_plans_only_for_selected_operation():
    base = DEMO.parent
    context = load_launch_context(base)
    contributions = load_infrastructure_contributions(context)
    schema = ConfigSchema({'DEMO_API_LOCAL': FieldDeclaration('DEMO_API_LOCAL', 'bool')})
    scope = StackScope('a' * 16)
    environment = {'DOCKER_HOST': 'unix:///run/test/engine.sock'}
    selected, _ = prepare_demo_api(config(), context, requires_api=True)

    active = plan_infrastructure(selected, schema, contributions, scope, engine_environment=environment)
    disabled, _ = prepare_demo_api(config(), context, requires_api=False)
    inactive = plan_infrastructure(disabled, schema, contributions, scope, engine_environment=environment)

    assert [item.id for item in active.conditional] == ['demo/api']
    assert active.conditional[0].endpoints[0].host == 'http://127.0.0.1:58092'
    assert inactive.conditional == ()


@pytest.mark.parametrize(('original', 'expected_local', 'source', 'explicit'), [
    (ConfigResult({}, {}, (), (), ()), True, None, None),
    (config('', source='cli', explicit=True), True, 'cli', True),
    (config('https://owner.example/graphql', source='pack_default'), False, 'pack_default', False),
    (config('https://remote.example/graphql', source='cli', explicit=True), False, 'cli', True),
])
def test_endpoint_provenance_controls_actual_compose_plan(original, expected_local, source, explicit):
    context = load_launch_context(DEMO.parent)
    services = load_infrastructure_contributions(context)
    schema = ConfigSchema({'DEMO_API_LOCAL': FieldDeclaration('DEMO_API_LOCAL', 'bool')})
    prepared, selected = prepare_demo_api(original, context, requires_api=True)

    plan = plan_infrastructure(prepared, schema, services, StackScope('b' * 16),
                               engine_environment={'DOCKER_HOST': 'unix:///run/test/engine.sock'})

    assert selected is expected_local
    assert [service.id for service in plan.conditional] == (['demo/api'] if expected_local else [])
    assert prepared.values.get('API_ENDPOINT') == original.values.get('API_ENDPOINT')
    if source is None:
        assert 'API_ENDPOINT' not in prepared.fields
    else:
        assert prepared.fields['API_ENDPOINT'].source == source
        assert prepared.fields['API_ENDPOINT'].explicit is explicit


def test_binding_rejects_duplicate_graphql_outputs():
    context = SimpleNamespace(pack_id='demo', external_pack=False)
    prepared, _ = prepare_demo_api(config(), context, requires_api=True)
    output = SimpleNamespace(name='graphql', host='http://127.0.0.1:58092')

    with pytest.raises(ValueError, match='not_ready'):
        bind_demo_api_endpoint(prepared, SimpleNamespace(endpoints={'demo/api': (output, output)}))


@pytest.fixture
def target():
    spec = importlib.util.spec_from_file_location('demo_api_server', DEMO / 'api/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.BoundedServer(('127.0.0.1', 0), module.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(url, payload, *, content_type='application/json'):
    request = Request(url + '/graphql', data=json.dumps(payload).encode(),
                      headers={'Content-Type': content_type})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


@pytest.mark.parametrize('path', ['/', '/graphql'])
def test_browser_routes_describe_graphql_post_contract(target, path):
    with urlopen(target + path, timeout=3) as response:
        body = response.read()
        assert response.status == 200
        assert response.headers['Content-Type'] == 'text/html; charset=utf-8'
        assert int(response.headers['Content-Length']) == len(body)

    page = body.decode('utf-8')
    assert '<title>Demo GraphQL API</title>' in page
    assert 'read-only' in page
    assert '<strong>POST</strong>' in page
    assert 'href="/graphql"' in page
    assert 'Content-Type: application/json' in page
    assert '{"query":"{ items { id name category } }"}' in page
    assert 'GET requests do not execute queries' in page
    assert '32,768 bytes' in page
    assert '16,000 characters' in page
    assert 'Variables must be omitted' in page
    assert 'operationName must be omitted' in page


def test_graphql_get_with_query_does_not_execute(target):
    with pytest.raises(HTTPError) as error:
        urlopen(target + '/graphql?query=%7Bitems%7Bid%7D%7D', timeout=3)

    assert error.value.code == 404
    assert json.load(error.value) == {'error': 'not_found'}


def test_readiness_and_full_introspection(target):
    from graphql import get_introspection_query
    with urlopen(target + '/ready', timeout=3) as response:
        ready = json.load(response)

    result = post(target, {'query': get_introspection_query()})

    assert ready == {'service': 'demo-graphql'}
    assert result['data']['__schema']['queryType']['name'] == 'Query'
    assert result.get('errors') is None


def test_partial_graphql_result_and_rest_equivalence(target):
    result = post(target, {'query': '{ items { id name category } brokenItem { id } }'})
    with urlopen(target + '/items', timeout=3) as response:
        rest = json.load(response)

    assert result['data']['items'] == rest['items']
    assert result['data']['brokenItem'] is None
    assert result['errors'][0]['path'] == ['brokenItem']


def test_item_lookup_and_invalid_graphql_query(target):
    found = post(target, {'query': '{ item(id:"2") { id name } }'})
    absent = post(target, {'query': '{ item(id:"missing") { id } }'})
    invalid = post(target, {'query': '{ unknown }'})

    assert found['data']['item'] == {'id': '2', 'name': 'Birch'}
    assert absent['data']['item'] is None
    assert 'data' not in invalid
    assert invalid['errors'][0]['message'].startswith("Cannot query field")


@pytest.mark.parametrize(('payload', 'content_type', 'status'), [
    ({'query': '{ items { id } }', 'variables': {'x': 1}}, 'application/json', 400),
    ({'query': '{ items { id } }'}, 'text/plain', 415),
])
def test_invalid_request_rejected(target, payload, content_type, status):
    with pytest.raises(HTTPError) as error:
        post(target, payload, content_type=content_type)

    assert error.value.code == status


def test_unknown_paths_and_malformed_body_rejected(target):
    with pytest.raises(HTTPError) as unknown_get:
        urlopen(target + '/other', timeout=3)
    with pytest.raises(HTTPError) as unknown_post:
        urlopen(Request(target + '/other', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=3)
    with pytest.raises(HTTPError) as malformed:
        urlopen(Request(target + '/graphql', data=b'{', headers={'Content-Type': 'application/json'}), timeout=3)
    assert (unknown_get.value.code, unknown_post.value.code, malformed.value.code) == (404, 404, 400)


def test_request_size_and_document_shape_rejected(target):
    with pytest.raises(HTTPError) as oversized:
        urlopen(Request(target + '/graphql', data=b'x' * 32769,
                        headers={'Content-Type': 'application/json'}), timeout=3)
    with pytest.raises(HTTPError) as wrong_shape:
        post(target, ['query'])
    assert (oversized.value.code, wrong_shape.value.code) == (413, 400)


def test_missing_length_rejected(target):
    from http.client import HTTPConnection
    from urllib.parse import urlsplit
    parsed = urlsplit(target)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.putrequest('POST', '/graphql')
        connection.putheader('Content-Type', 'application/json')
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
    finally:
        connection.close()


def test_script_entry_starts_bounded_server(monkeypatch):
    import http.server
    import runpy
    calls = []
    class Server:
        def __init__(self, address, handler):
            calls.append((address, handler))
        def serve_forever(self):
            calls.append('serve')
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', Server)

    runpy.run_path(str(DEMO / 'api/server.py'), run_name='__main__')

    assert calls[0][0] == ('0.0.0.0', 8090)
    assert calls[1] == 'serve'


def test_slow_headers_consume_bounded_workers_and_time_out():
    import socket
    import time
    spec = importlib.util.spec_from_file_location('demo_api_server_bounded', DEMO / 'api/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.BoundedServer(('127.0.0.1', 0), module.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connections = []
    try:
        for _ in range(4):
            connection = socket.create_connection(('127.0.0.1', server.server_port), timeout=2)
            connection.sendall(b'GET /ready HTTP/1.1\r\nHost: localhost\r\n')
            connections.append(connection)
        deadline = time.monotonic() + 2
        while server._workers._value != 0 and time.monotonic() < deadline:
            time.sleep(.01)
        assert server._workers._value == 0
        overflow = socket.create_connection(('127.0.0.1', server.server_port), timeout=2)
        try:
            overflow.settimeout(2)
            assert overflow.recv(1) == b''
        finally:
            overflow.close()
        connections[0].settimeout(6)
        assert connections[0].recv(1) == b''
    finally:
        for connection in connections:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_worker_slot_released_when_thread_dispatch_fails(monkeypatch):
    from http.server import ThreadingHTTPServer
    spec = importlib.util.spec_from_file_location('demo_api_server_dispatch', DEMO / 'api/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.BoundedServer(('127.0.0.1', 0), module.Handler)
    request = SimpleNamespace(close=lambda: None)
    def fail(_self, _request, _address):
        raise RuntimeError('thread unavailable')
    monkeypatch.setattr(ThreadingHTTPServer, 'process_request', fail)
    try:
        with pytest.raises(RuntimeError, match='thread unavailable'):
            server.process_request(request, ('127.0.0.1', 1))
        assert server._workers._value == 4
        server.handle_error(request, ('127.0.0.1', 1))
    finally:
        server.server_close()
