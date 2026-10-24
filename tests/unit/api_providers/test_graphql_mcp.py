import asyncio
import hashlib
import json

import pytest
import pytest_asyncio
from graphql import build_schema, graphql_sync, get_introspection_query

from api_agent.api_providers.graphql_mcp import GraphQLMcpOwner, GraphQLMcpError, GraphQLQueryError
from api_agent.providers.tools import PreparedCall


@pytest_asyncio.fixture
async def endpoint():
    schema = build_schema('type Query { hello: String! broken: String echo: String echoToken: String remoteFailure: String }')
    calls = []

    def resolve_hello(*_):
        return 'world'

    def resolve_broken(_, info):
        raise ValueError(info.context['authorization'] or 'synthetic failure')

    def resolve_echo(_, info):
        return info.context['authorization']

    def resolve_echo_token(_, info):
        return info.context['authorization'].partition(' ')[2]

    schema.query_type.fields['hello'].resolve = resolve_hello
    schema.query_type.fields['broken'].resolve = resolve_broken
    schema.query_type.fields['echo'].resolve = resolve_echo
    schema.query_type.fields['echoToken'].resolve = resolve_echo_token

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            length = int(next(x.split(b':', 1)[1] for x in head.split(b'\r\n') if x.lower().startswith(b'content-length:')))
            body = json.loads(await reader.readexactly(length))
            calls.append(body)
            auth = next((x.split(b':', 1)[1].strip().decode() for x in head.split(b'\r\n') if x.lower().startswith(b'authorization:')), '')
            result = graphql_sync(schema, body['query'], context_value={'authorization': auth})
            if body['query'].strip() == '{ remoteFailure }':
                payload = b'{"errors":[{"message":"synthetic upstream failure"}]}'
                status = b'HTTP/1.1 503 Service Unavailable'
            else:
                payload = json.dumps(result.formatted).encode()
                status = b'HTTP/1.1 200 OK'
            writer.write(status + b'\r\ncontent-type: application/json\r\ncontent-length: ' + str(len(payload)).encode() + b'\r\nconnection: close\r\n\r\n' + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/graphql', calls


def prepared(spec, query):
    return PreparedCall(spec=spec, arguments={'query': query}, invocation_id='one', digest=hashlib.sha256(query.encode()).hexdigest())


@pytest.mark.asyncio
async def test_real_published_mcp_preserves_partial_data_and_errors(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        assert len(calls) == 1  # fixed complete introspection through query-graphql
        call = prepared(spec, '{ hello broken }')
        await owner.validate(call)
        assert len(calls) == 1  # validation did not execute
        result = await owner.invoke(call)
        assert result.status == 'partial'
        assert result.data == {'hello': 'world', 'broken': None}
        assert len(result.errors) == 1
        assert len(calls) == 2
        next_call = PreparedCall(spec, {'query': '{ hello }'}, 'two', 'synthetic-digest')
        next_result = await owner.invoke(next_call)
        assert next_result.status == 'success'
        assert next_result.data == {'hello': 'world'}
    finally:
        await owner.aclose()
    assert owner._process is None


@pytest.mark.asyncio
@pytest.mark.parametrize('query,code', [
    ('mutation { hello }', 'query_operation'),
    ('query($x: String!) { hello }', 'query_variables'),
    ('{ unknown }', 'query_schema'),
    ('{ hello } { broken }', 'query_operation'),
])
async def test_rejection_precedes_execution(endpoint, query, code):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        with pytest.raises(GraphQLQueryError, match=code):
            await owner.validate(prepared(spec, query))
        assert len(calls) == 1
    finally:
        await owner.aclose()


def test_endpoint_and_header_secrets_rejected():
    with pytest.raises(GraphQLMcpError, match='endpoint_invalid'):
        GraphQLMcpOwner('http://user:secret@example.test/graphql')
    with pytest.raises(GraphQLMcpError, match='headers_invalid'):
        GraphQLMcpOwner('http://example.test/graphql', headers={'Authorization': 'x\nInjected: y'})

@pytest.mark.asyncio
async def test_locked_assets_prepare_offline(tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets, installed_script
    script = await prepare_assets(tmp_path, npm='npm', timeout=20)
    assert script == installed_script(tmp_path)
    metadata = json.loads((tmp_path / 'node_modules/mcp-graphql/package.json').read_text())
    assert metadata['version'] == '2.0.4'


@pytest.mark.asyncio
async def test_root_field_allowlist_blocks_before_execution(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url, root_fields=frozenset({'hello'}))
    try:
        spec = await owner.describe()
        with pytest.raises(GraphQLQueryError, match='query_root_field'):
            await owner.validate(prepared(spec, '{ broken }'))
        assert len(calls) == 1
    finally:
        await owner.aclose()

@pytest.mark.parametrize('body,is_error,code', [
    ('GraphQL request failed: Bad Request\n{}', True, 'http_failure'),
    ('unexpected child error', True, 'mcp_tool_error'),
    ('not json', False, 'graphql_json'),
    ('{}', False, 'graphql_shape'),
    ('{"errors":[]}', False, 'graphql_shape'),
])
def test_result_envelopes_fail_closed(body, is_error, code):
    with pytest.raises(GraphQLMcpError, match=code):
        GraphQLMcpOwner._parse_json(body, is_error)


@pytest.mark.parametrize('content', [
    {},
    {'content': []},
    {'content': [{'type': 'image', 'data': 'x'}]},
    {'content': [{'type': 'text', 'text': 1}]},
])
def test_mcp_content_requires_one_text_block(content):
    with pytest.raises(GraphQLMcpError, match='mcp_content'):
        GraphQLMcpOwner._text(content)


def test_missing_assets_fail_before_start(tmp_path):
    from api_agent.api_providers.graphql_mcp import installed_script
    with pytest.raises(GraphQLMcpError, match='assets_missing'):
        installed_script(tmp_path)


@pytest.mark.asyncio
async def test_fragment_expansion_limit_blocks_before_execution(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        query = 'query { ...F ...F ...F ...F ...F } fragment F on Query { hello }'
        await owner.validate(prepared(spec, query))
        assert len(calls) == 1
        with pytest.raises(GraphQLQueryError, match='query_root_field'):
            await owner.validate(prepared(spec, '{ __schema { queryType { name } } }'))
        assert len(calls) == 1
    finally:
        await owner.aclose()

@pytest.mark.parametrize('body', [
    '{"data": []}',
    '{"errors": ["bad"]}',
])
def test_graphql_json_shape_is_checked(body):
    with pytest.raises(GraphQLMcpError, match='graphql_shape'):
        GraphQLMcpOwner._parse_json(body, False)


@pytest.mark.asyncio
async def test_incomplete_and_errored_introspection_fail_closed(monkeypatch):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def errored(*_):
        return {'isError': True, 'content': [{'type': 'text', 'text': 'The GraphQL response has errors, please fix the query: {"errors":[{"message":"no"}]}'}]}

    monkeypatch.setattr(owner, '_call', errored)
    with pytest.raises(GraphQLMcpError, match='introspection_failed'):
        await owner.describe()

    async def incomplete(*_):
        return {'content': [{'type': 'text', 'text': '{"data":{"__schema":{}}}'}]}

    monkeypatch.setattr(owner, '_call', incomplete)
    with pytest.raises(GraphQLMcpError, match='introspection_incomplete'):
        await owner.describe()


@pytest.mark.asyncio
async def test_bad_query_arguments_fail_before_mcp_call(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        for value, code in [('', 'query_size'), ('{', 'query_syntax'), ('{ hello } #'+('x'*9000), 'query_size')]:
            with pytest.raises(GraphQLQueryError, match=code):
                await owner.validate(prepared(spec, value))
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_failure_result_does_not_claim_partial_data(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        result = await owner.invoke(prepared(spec, '{ broken }'))
        assert result.status == 'partial'  # nullable field yields data object with error
        assert result.data == {'broken': None}
    finally:
        await owner.aclose()

class _Input:
    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, value):
        self.sent.append(json.loads(value))

    async def drain(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _Output:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        return self.lines.pop(0) if self.lines else b''


class _Process:
    def __init__(self, lines):
        self.stdin = _Input()
        self.stdout = _Output(lines)
        self.stderr = None

    async def wait(self):
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize('line,code', [
    (b'', 'protocol_output'),
    (b'not json\n', 'protocol_output'),
    (b'[]\n', 'protocol_output'),
    (b'{"jsonrpc":"2.0","id":99,"result":{}}\n', 'protocol_id'),
    (b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}\n', 'mcp_error'),
    (b'{"jsonrpc":"2.0","id":1,"result":null}\n', 'protocol_output'),
])
async def test_jsonrpc_rejects_broken_frames(line, code):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    owner._process = _Process([line])
    with pytest.raises(GraphQLMcpError, match=code):
        await owner._rpc('test', {})
    assert owner._process.stdin.sent[0]['id'] == 1


@pytest.mark.asyncio
async def test_jsonrpc_ignores_notification_and_matches_id():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    owner._process = _Process([
        b'{"jsonrpc":"2.0","method":"notifications/progress"}\n',
        b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n',
    ])
    assert await owner._rpc('test', {}) == {'ok': True}

@pytest.mark.parametrize('kwargs,code', [
    ({'timeout': 0}, 'timeout_invalid'),
    ({'root_fields': frozenset()}, 'root_fields_invalid'),
    ({'node': '/missing/node'}, 'node_missing'),
    ({'headers': []}, 'headers_invalid'),
])
def test_invalid_owner_configuration(kwargs, code):
    with pytest.raises(GraphQLMcpError, match=code):
        GraphQLMcpOwner('http://example.test/graphql', **kwargs)


@pytest.mark.asyncio
async def test_bad_asset_preparation_configuration(tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets
    with pytest.raises(GraphQLMcpError, match='asset_timeout_invalid'):
        await prepare_assets(tmp_path, timeout=0)
    with pytest.raises(GraphQLMcpError, match='npm_missing'):
        await prepare_assets(tmp_path, npm='/missing/npm')


@pytest.mark.asyncio
async def test_missing_installed_asset_blocks_describe(tmp_path):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', assets_dir=tmp_path)
    with pytest.raises(GraphQLMcpError, match='assets_missing'):
        await owner.describe()
    assert owner._process is None


@pytest.mark.asyncio
async def test_content_size_is_bounded():
    from api_agent.api_providers.graphql_mcp.owner import MAX_RESULT
    with pytest.raises(GraphQLMcpError, match='result_size'):
        GraphQLMcpOwner._text({'content': [{'type': 'text', 'text': 'x'*(MAX_RESULT+1)}]})

@pytest.mark.asyncio
async def test_standard_api_provider_factory_adapts_tool_owner(monkeypatch, tmp_path, endpoint):
    from api_agent.api_providers.graphql_mcp import factory
    from api_agent.providers.contracts import APIRequest
    url, calls = endpoint
    provider = factory.create({'API_ENDPOINT': url}, {'owner_root': tmp_path})
    with pytest.raises(GraphQLMcpError, match='provider_not_started'):
        await provider.describe()
    await provider.astart()
    view = provider.for_invocation()
    assert view is not provider._owner
    await view.aclose()
    try:
        spec = await provider.describe()
        call = prepared(spec, '{ hello }')
        await provider.validate(call)
        result = await provider.ainvoke(APIRequest(payload=call, operation='invoke'))
        assert result.payload.status == 'success'
        assert result.payload.data == {'hello': 'world'}
        assert (tmp_path / '.api-agent' / 'graphql-mcp' / 'package-lock.json').is_file()
        assert len(calls) == 2
    finally:
        await provider.aclose()

@pytest.mark.asyncio
async def test_api_provider_loader_conformance(tmp_path):
    from api_agent.api_providers.graphql_mcp import factory
    from api_agent.providers.loading import aload_provider
    location = f'{factory.__file__}:create'
    loaded = await aload_provider(location, {'API_ENDPOINT': 'http://127.0.0.1:1/graphql'}, {'owner_root': tmp_path}, kind='api')
    assert loaded.require('api', 'invoke').capabilities.kind == 'api'
    await loaded.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('script,code', [
    ('#!/bin/sh\nexit 7\n', 'asset_install'),
    ('#!/bin/sh\nsleep 2\n', 'asset_timeout'),
    ('#!/bin/sh\nyes x | head -c 70000\n', 'asset_output'),
])
async def test_asset_preparation_fails_closed(tmp_path, script, code):
    from api_agent.api_providers.graphql_mcp import prepare_assets
    command = tmp_path / 'controlled-npm'
    command.write_text(script)
    command.chmod(0o755)
    with pytest.raises(GraphQLMcpError, match=code):
        await prepare_assets(tmp_path / 'assets', npm=str(command), timeout=0.05 if code == 'asset_timeout' else 3)

class _Emissions:
    def __init__(self):
        self.values = []

    def register_secret(self, value):
        self.values.append(value)


def test_factory_registers_explicit_header_secret_before_owner_validation(tmp_path):
    from api_agent.api_providers.graphql_mcp.factory import create
    emissions = _Emissions()
    with pytest.raises(GraphQLMcpError, match='endpoint_invalid'):
        create({'API_ENDPOINT': 'invalid', 'API_HEADERS': {'Authorization': 'Bearer synthetic'}},
               {'owner_root': tmp_path, 'emissions': emissions})
    assert set(emissions.values) == {'Bearer synthetic', 'synthetic'}


@pytest.mark.parametrize('config,services,code', [
    ({'API_ENDPOINT': 'http://example.test', 'API_HEADERS': []}, {'owner_root': '/tmp'}, 'headers_invalid'),
    ({'API_ENDPOINT': 'http://example.test'}, {}, 'owner_root_missing'),
    ({'API_ENDPOINT': 'http://example.test', 'API_ROOT_FIELDS': []}, {'owner_root': '/tmp'}, 'root_fields_invalid'),
])
def test_factory_configuration_failures(config, services, code):
    from api_agent.api_providers.graphql_mcp.factory import create
    with pytest.raises(GraphQLMcpError, match=code):
        create(config, services)


@pytest.mark.asyncio
async def test_factory_ainvoke_rejects_unbound_input(tmp_path):
    from api_agent.api_providers.graphql_mcp.factory import create
    from api_agent.providers.contracts import APIRequest
    provider = create({'API_ENDPOINT': 'http://127.0.0.1:1/graphql'}, {'owner_root': tmp_path})
    with pytest.raises(GraphQLMcpError, match='api_request_invalid'):
        await provider.ainvoke(APIRequest(payload={'query': '{ hello }'}, operation='invoke'))
    with pytest.raises(GraphQLMcpError, match='api_request_invalid'):
        await provider.ainvoke(APIRequest(payload=None), auth='x')

@pytest.mark.parametrize('query,code', [
    ('{ ' + ' '.join('f'+str(i)+': hello' for i in range(410)) + ' }', 'query_complexity'),
    ('{ ' + 'n {'*13 + 'hello' + '}'*13 + ' }', 'query_depth'),
])
def test_ast_resource_bounds_before_schema_validation(query, code):
    from graphql import parse, visit
    from api_agent.api_providers.graphql_mcp.owner import _QueryBounds
    with pytest.raises(GraphQLQueryError, match=code):
        visit(parse(query), _QueryBounds())


def test_ast_variable_reference_detected():
    from graphql import parse, visit
    from api_agent.api_providers.graphql_mcp.owner import _QueryBounds
    bounds = _QueryBounds()
    visit(parse('query($x: String) { hello }'), bounds)
    assert bounds.variable is True


class _NeverOutput:
    async def readline(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_jsonrpc_deadline_bounds_stalled_child():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', timeout=0.01)
    owner._process = _Process([])
    owner._process.stdout = _NeverOutput()
    with pytest.raises(GraphQLMcpError, match='timeout'):
        await owner._rpc('test', {})

@pytest.mark.asyncio
async def test_process_start_error_is_classified(monkeypatch):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def failed_start(*_, **__):
        raise OSError('controlled launch failure')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', failed_start)
    with pytest.raises(GraphQLMcpError, match='process_start'):
        await owner.describe()
    assert owner._process is None


class _Stderr:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _):
        return self.chunks.pop(0) if self.chunks else b''


@pytest.mark.asyncio
async def test_child_stderr_is_bounded_without_logging():
    from api_agent.api_providers.graphql_mcp.owner import MAX_STDERR
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    process = _Process([])
    process.stderr = _Stderr([b'secret', b'x'*(MAX_STDERR+1)])
    process.killed = False

    def kill():
        process.killed = True

    process.kill = kill
    owner._process = process
    await owner._drain_stderr()
    assert process.killed


@pytest.mark.asyncio
async def test_start_failure_closes_process(monkeypatch):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    process = _Process([])
    process.stderr = _Stderr([])

    async def fake_start(*_, **__):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_start)
    with pytest.raises(GraphQLMcpError, match='protocol_output'):
        await owner.describe()
    assert process.stdin.closed
    assert owner._process is None

@pytest.mark.asyncio
async def test_prepared_binding_and_missing_query_rejected(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        with pytest.raises(GraphQLQueryError, match='call_binding'):
            await owner.validate(PreparedCall(spec, {'wrong': '{ hello }'}, 'one', 'x'))
        with pytest.raises(GraphQLQueryError, match='query_size'):
            await owner.validate(prepared(spec, ''))
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response,code', [
    ('{"data":{"hello":"world"},"errors":[{"message":"bad"}]}', 'mcp_envelope'),
    ('{"data":null}', 'graphql_shape'),
])
async def test_inconsistent_success_envelope_rejected(endpoint, response, code, monkeypatch):
    url, _ = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()

        async def inconsistent(*_):
            return {'content': [{'type': 'text', 'text': response}]}

        monkeypatch.setattr(owner, '_call', inconsistent)
        result = await owner.invoke(prepared(spec, '{ hello }'))
        assert result.status == 'failure'
        assert result.diagnostic == code
        assert result.data is None
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_inline_fragment_is_validated_without_execution(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        await owner.validate(prepared(spec, '{ ... on Query { hello } }'))
        assert len(calls) == 1
    finally:
        await owner.aclose()

@pytest.mark.asyncio
async def test_process_timeout_forces_reap():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', timeout=0.01)
    process = _Process([])
    process.killed = False

    async def wait():
        if not process.killed:
            await asyncio.Event().wait()
        return 0

    def kill():
        process.killed = True

    process.wait = wait
    process.kill = kill
    owner._process = process
    await owner.aclose()
    assert process.killed
    assert owner._process is None


@pytest.mark.asyncio
async def test_unreaped_child_retains_owner(monkeypatch):
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', timeout=0.01)
    process = _Process([])
    process.kill = lambda: None

    async def wait():
        await asyncio.Event().wait()

    process.wait = wait
    owner._process = process
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, 0.001)

    monkeypatch.setattr(asyncio, 'wait_for', fast_wait_for)
    with pytest.raises(GraphQLMcpError, match='process_unreaped'):
        await owner.aclose()
    assert owner._process is process


@pytest.mark.asyncio
async def test_protocol_frame_overrun_is_classified():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    process = _Process([])

    class Overflow:
        async def readline(self):
            raise asyncio.LimitOverrunError('synthetic', 1)

    process.stdout = Overflow()
    owner._process = process
    with pytest.raises(GraphQLMcpError, match='protocol_output'):
        await owner._rpc('test', {})


@pytest.mark.asyncio
async def test_missing_query_root_fails_introspection(monkeypatch):
    import api_agent.api_providers.graphql_mcp.owner as owner_module
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def response(*_):
        return {'content': [{'type': 'text', 'text': '{"data":{"__schema":{}}}'}]}

    monkeypatch.setattr(owner, '_call', response)
    monkeypatch.setattr(owner_module, 'build_client_schema', lambda *_: type('Schema', (), {'query_type': None})())
    with pytest.raises(GraphQLMcpError, match='introspection_incomplete'):
        await owner.describe()

@pytest.mark.asyncio
async def test_stderr_eof_and_already_closed_stdin():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    process = _Process([])
    process.stderr = _Stderr([])
    process.stdin.closed = True
    owner._process = process
    await owner._drain_stderr()
    await owner.aclose()
    assert owner._process is None


@pytest.mark.asyncio
async def test_undefined_variable_reference_rejected_before_schema(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        with pytest.raises(GraphQLQueryError, match='query_variables'):
            await owner.validate(prepared(spec, '{ hello(x: $x) }'))
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_fragment_expansion_complexity_rejected(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        query = '{ ' + ' '.join('...F' for _ in range(5)) + ' } fragment F on Query { ' + ' '.join('hello' for _ in range(90)) + ' }'
        with pytest.raises(GraphQLQueryError, match='query_complexity'):
            await owner.validate(prepared(spec, query))
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_fragment_expansion_depth_and_cycle_guard(endpoint, monkeypatch):
    import api_agent.api_providers.graphql_mcp.owner as owner_module
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        monkeypatch.setattr(owner_module, 'validate', lambda *_: [])
        fragments = []
        for index in range(1, 14):
            selection = 'n { ...F' + str(index+1) + ' }' if index < 13 else 'hello'
            fragments.append(f'fragment F{index} on Query {{ {selection} }}')
        with pytest.raises(GraphQLQueryError, match='query_depth'):
            await owner.validate(prepared(spec, '{ ...F1 } ' + ' '.join(fragments)))
        with pytest.raises(GraphQLQueryError, match='query_fragment_cycle'):
            await owner.validate(prepared(spec, '{ ...F } fragment F on Query { ...F }'))
        await owner.validate(prepared(spec, '{ n { hello } ... on Query { hello } hello }'))
        assert len(calls) == 1
    finally:
        await owner.aclose()

@pytest.mark.asyncio
async def test_installed_asset_identity_and_missing_output(tmp_path):
    from api_agent.api_providers.graphql_mcp import installed_script, prepare_assets
    package = tmp_path / 'node_modules/mcp-graphql/package.json'
    package.parent.mkdir(parents=True)
    package.write_text('{"name":"mcp-graphql","version":"0.0.0"}')
    with pytest.raises(GraphQLMcpError, match='assets_invalid'):
        installed_script(tmp_path)
    command = tmp_path / 'controlled-npm'
    command.write_text('#!/bin/sh\nexit 0\n')
    command.chmod(0o755)
    with pytest.raises(GraphQLMcpError, match='assets_missing'):
        await prepare_assets(tmp_path / 'other', npm=str(command))


@pytest.mark.asyncio
async def test_asset_launch_oserror_classified(monkeypatch, tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets

    async def failed(*_, **__):
        raise OSError('controlled')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', failed)
    with pytest.raises(GraphQLMcpError, match='asset_install'):
        await prepare_assets(tmp_path)


@pytest.mark.asyncio
async def test_factory_repeated_start_does_not_reinstall(monkeypatch, tmp_path):
    from api_agent.api_providers.graphql_mcp import factory
    count = 0

    async def prepare(_):
        nonlocal count
        count += 1

    monkeypatch.setattr(factory, 'prepare_assets', prepare)
    provider = factory.create({'API_ENDPOINT': 'http://127.0.0.1:1/graphql', 'API_ROOT_FIELDS': ['hello']}, {'owner_root': tmp_path})
    await provider.astart()
    await provider.astart()
    assert count == 1
    assert provider._owner._root_fields == frozenset({'hello'})
    await provider.aclose()


def test_factory_registers_multiple_header_values(tmp_path):
    from api_agent.api_providers.graphql_mcp.factory import create
    emissions = _Emissions()
    create({'API_ENDPOINT': 'http://127.0.0.1:1/graphql', 'API_HEADERS': {'A': 'a', 'B': '', 'C': 'b'}},
           {'owner_root': tmp_path, 'emissions': emissions})
    assert emissions.values == ['a', 'b']

@pytest.mark.asyncio
async def test_asset_child_unexpected_error_is_sanitized(monkeypatch, tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets

    class BadStream:
        async def read(self, _):
            raise RuntimeError('raw synthetic child detail')

    class EmptyStream:
        async def read(self, _):
            return b''

    class ExitedProcess:
        returncode = 0
        stdout = BadStream()
        stderr = EmptyStream()

        async def wait(self):
            return 0

    async def fake_start(*_, **__):
        return ExitedProcess()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_start)
    with pytest.raises(GraphQLMcpError, match='asset_install') as error:
        await prepare_assets(tmp_path)
    assert 'raw synthetic child detail' not in str(error.value)


@pytest.mark.asyncio
async def test_asset_preparation_cancellation_reaps_child(monkeypatch, tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets
    started = asyncio.Event()
    killed = asyncio.Event()

    class WaitingStream:
        async def read(self, _):
            await asyncio.Event().wait()

    class WaitingProcess:
        returncode = None
        stdout = WaitingStream()
        stderr = WaitingStream()

        async def wait(self):
            started.set()
            await killed.wait()
            self.returncode = -9
            return -9

        def kill(self):
            killed.set()

    process = WaitingProcess()

    async def fake_start(*_, **__):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_start)
    task = asyncio.create_task(prepare_assets(tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set()
    assert process.returncode == -9

@pytest.mark.asyncio
async def test_concurrent_describe_freezes_one_schema_snapshot(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        first, second = await asyncio.gather(owner.describe(), owner.describe())
        assert first is second
        assert len(calls) == 1
    finally:
        await owner.aclose()

@pytest.mark.asyncio
async def test_real_mcp_partial_response_scrubs_header_and_token_before_result(endpoint):
    url, calls = endpoint
    secret = 'SYNTHETIC_TEST_ONLY'
    owner = GraphQLMcpOwner(url, headers={'Authorization': 'Bearer ' + secret})
    try:
        spec = await owner.describe()
        result = await owner.invoke(prepared(spec, '{ hello echo echoToken broken }'))
        assert result.status == 'partial'
        assert result.data == {'hello': 'world', 'echo': '[REDACTED]',
                               'echoToken': '[REDACTED]', 'broken': None}
        assert result.errors[0]['message'] == '[REDACTED]'
        assert secret not in json.dumps({'data': result.data, 'errors': result.errors})
        assert len(calls) == 2
    finally:
        await owner.aclose()


def test_scrub_traverses_nested_keys_and_extensions():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', headers={'X-Key': 'SYNTHETIC_TEST_ONLY'})
    source = {'SYNTHETIC_TEST_ONLY': {'extensions': [{'detail': 'before SYNTHETIC_TEST_ONLY after'}]}}
    assert owner._scrub(source) == {'[REDACTED]': {'extensions': [{'detail': 'before [REDACTED] after'}]}}

@pytest.mark.asyncio
async def test_invocation_views_refresh_schema_without_restarting_transport():
    schemas = [build_schema('type Query { first: String }'),
               build_schema('type Query { second: String }')]
    current = 0
    requests = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            length = int(next(x.split(b':', 1)[1] for x in head.split(b'\r\n') if x.lower().startswith(b'content-length:')))
            body = json.loads(await reader.readexactly(length))
            requests.append(body)
            result = graphql_sync(schemas[current], body['query'])
            payload = json.dumps(result.formatted).encode()
            writer.write(b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: ' + str(len(payload)).encode() + b'\r\nconnection: close\r\n\r\n' + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/graphql'
        transport = GraphQLMcpOwner(url)
        try:
            first_view = transport.for_invocation()
            first_spec = await first_view.describe()
            assert 'first: String' in first_spec.input_schema['properties']['query']['description']
            process = transport._process
            current = 1
            assert await first_view.describe() is first_spec
            second_view = transport.for_invocation()
            second_spec = await second_view.describe()
            assert second_spec.schema_fingerprint != first_spec.schema_fingerprint
            assert 'second: String' in second_spec.input_schema['properties']['query']['description']
            assert 'first: String' not in second_spec.input_schema['properties']['query']['description']
            assert transport._process is process
            await second_view.validate(prepared(second_spec, '{ second }'))
            with pytest.raises(GraphQLQueryError, match='query_schema'):
                await first_view.validate(prepared(first_spec, '{ second }'))
            await first_view.aclose()
            assert transport._process is process
            assert len(requests) == 2
        finally:
            await transport.aclose()

@pytest.mark.parametrize('endpoint', [None, 'http://[::1', 'http://example.test:bad/graphql',
                                     'http://example.test:0/graphql', 'https://example.test/graphql?token=x'])
def test_endpoint_rejects_malformed_or_embedded_credentials(endpoint):
    with pytest.raises(GraphQLMcpError, match='endpoint_invalid'):
        GraphQLMcpOwner(endpoint)

@pytest.mark.asyncio
async def test_locked_asset_integrity_rejects_tampered_dist_dependency_and_lock(tmp_path):
    from api_agent.api_providers.graphql_mcp import prepare_assets, installed_script
    await prepare_assets(tmp_path)
    targets = [tmp_path / 'node_modules/mcp-graphql/dist/index.js',
               tmp_path / 'node_modules/zod/package.json',
               tmp_path / 'package-lock.json',
               tmp_path / 'package.json']
    for target in targets:
        original = target.read_bytes()
        try:
            target.write_bytes(original + b'\nsynthetic-tamper')
            with pytest.raises(GraphQLMcpError, match='assets_integrity'):
                installed_script(tmp_path)
        finally:
            target.write_bytes(original)
    assert installed_script(tmp_path).is_file()


def test_retained_notices_cover_installed_license_files():
    from pathlib import Path
    package = Path(__file__).resolve().parents[3] / 'src/api_agent/api_providers/graphql_mcp'
    installed = package / 'node_modules'
    notices = package / 'licenses/third-party'
    missing = []
    for path in installed.rglob('*'):
        if path.is_file() and (path.name.lower().startswith('license') or
                               path.name.lower().startswith('licence') or path.name.lower() == 'copying'):
            if not (notices / path.relative_to(installed)).is_file():
                missing.append(str(path.relative_to(installed)))
    assert missing == []

@pytest.mark.asyncio
async def test_real_mcp_http_failure_becomes_safe_renderable_result(endpoint):
    url, calls = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()
        result = await owner.invoke(prepared(spec, '{ remoteFailure }'))
        assert result.status == 'failure'
        assert result.data is None
        assert result.errors == ()
        assert result.diagnostic == 'http_failure'
        assert len(calls) == 2
    finally:
        await owner.aclose()

@pytest.mark.asyncio
async def test_installed_assets_reject_symlink_escape_and_missing_lock(tmp_path):
    from api_agent.api_providers.graphql_mcp import installed_script, prepare_assets
    await prepare_assets(tmp_path)
    script = tmp_path / 'node_modules/mcp-graphql/dist/index.js'
    outside = tmp_path.parent / (tmp_path.name + '-outside.js')
    outside.write_bytes(script.read_bytes())
    script.unlink()
    script.symlink_to(outside)
    with pytest.raises(GraphQLMcpError, match='assets_integrity'):
        installed_script(tmp_path)
    script.unlink()
    script.write_bytes(outside.read_bytes())
    (tmp_path / 'package-lock.json').unlink()
    with pytest.raises(GraphQLMcpError, match='assets_integrity'):
        installed_script(tmp_path)


def test_tree_digest_includes_unexpected_symlinks(tmp_path):
    from api_agent.api_providers.graphql_mcp.assets import _tree_sha256
    (tmp_path / 'file.txt').write_text('synthetic')
    initial = _tree_sha256(tmp_path)
    (tmp_path / 'link.txt').symlink_to('file.txt')
    assert _tree_sha256(tmp_path) != initial


@pytest.mark.asyncio
async def test_source_lock_fingerprint_blocks_preparation(monkeypatch, tmp_path):
    import api_agent.api_providers.graphql_mcp.assets as assets
    monkeypatch.setattr(assets, '_LOCK_SHA256', '0'*64)
    with pytest.raises(GraphQLMcpError, match='lock_integrity'):
        await assets.prepare_assets(tmp_path)
    assert not (tmp_path / 'node_modules').exists()


def test_unsupported_authorization_scheme_still_scrubs_full_value():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', headers={'Authorization': 'Token SYNTHETIC_TEST_ONLY'})
    assert owner._scrub('Token SYNTHETIC_TEST_ONLY') == '[REDACTED]'
    assert owner._scrub('SYNTHETIC_TEST_ONLY') == 'SYNTHETIC_TEST_ONLY'


@pytest.mark.asyncio
async def test_stderr_kill_race_is_silent():
    from api_agent.api_providers.graphql_mcp.owner import MAX_STDERR
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    process = _Process([])
    process.stderr = _Stderr([b'x'*(MAX_STDERR+1)])
    process.kill = lambda: (_ for _ in ()).throw(ProcessLookupError())
    owner._process = process
    await owner._drain_stderr()


@pytest.mark.asyncio
async def test_oversized_printed_schema_fails_discovery(endpoint, monkeypatch):
    import api_agent.api_providers.graphql_mcp.owner as owner_module
    url, _ = endpoint
    owner = GraphQLMcpOwner(url)
    monkeypatch.setattr(owner_module, 'print_schema', lambda *_: 'x'*(owner_module.MAX_SDL+1))
    try:
        with pytest.raises(GraphQLMcpError, match='schema_size'):
            await owner.describe()
    finally:
        await owner.aclose()


def test_scrub_rejects_excessive_depth_and_key_collision():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', headers={'X-Key': 'SYNTHETIC_TEST_ONLY'})
    nested = 'leaf'
    for _ in range(34):
        nested = [nested]
    with pytest.raises(GraphQLMcpError, match='result_depth'):
        owner._scrub(nested)
    with pytest.raises(GraphQLMcpError, match='result_secret_collision'):
        owner._scrub({'SYNTHETIC_TEST_ONLY': 1, '[REDACTED]': 2})


@pytest.mark.asyncio
async def test_lifecycle_failure_during_invoke_propagates(endpoint, monkeypatch):
    url, _ = endpoint
    owner = GraphQLMcpOwner(url)
    try:
        spec = await owner.describe()

        async def failed(*_):
            raise GraphQLMcpError('process_start')

        monkeypatch.setattr(owner, '_call', failed)
        with pytest.raises(GraphQLMcpError, match='process_start'):
            await owner.invoke(prepared(spec, '{ hello }'))
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_stderr_drain_failure_is_sanitized():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    owner._process = _Process([])

    async def failed():
        raise RuntimeError('raw synthetic secret')

    owner._stderr_task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    with pytest.raises(GraphQLMcpError, match='stderr_drain') as error:
        await owner.aclose()
    assert 'raw synthetic secret' not in str(error.value)
    assert owner._process is None
    await owner.aclose()


@pytest.mark.asyncio
async def test_normalized_credentials_are_scrubbed_from_direct_results(endpoint):
    url, _ = endpoint
    secret = 'SYNTHETIC_TEST_ONLY'
    owner = GraphQLMcpOwner(url, headers={'Authorization': ' Bearer  ' + secret + ' '})
    try:
        spec = await owner.describe()
        result = await owner.invoke(prepared(spec, '{ echoToken }'))
        assert secret not in repr(result)
        assert result.data == {'echoToken': '[REDACTED]'}
        assert owner._scrub({'other': secret}) == {'other': '[REDACTED]'}
    finally:
        await owner.aclose()


def test_surrounding_header_whitespace_is_scrubbed():
    secret = 'SYNTHETIC_TEST_ONLY'
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql', headers={'X-Key': '  ' + secret + '  '})
    assert owner._scrub({'echo': secret}) == {'echo': '[REDACTED]'}
    assert owner._scrub(secret) == '[REDACTED]'


def test_tab_separated_authorization_token_is_registered():
    from api_agent.api_providers.graphql_mcp.owner import header_secret_values
    secret = 'SYNTHETIC_TEST_ONLY'
    values = header_secret_values('Authorization', ' Bearer\t' + secret + ' ')
    assert secret in values
    assert 'Bearer ' + secret in values
    assert header_secret_values('X-Key', '  ') == frozenset()
    assert header_secret_values(None, secret) == frozenset()
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql',
                            headers={'Authorization': ' Bearer\t' + secret + ' '})
    assert owner._headers['Authorization'] == 'Bearer ' + secret
    assert owner._scrub(secret) == '[REDACTED]'


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_path', [
    ('__schema', 'directives'),
    ('__schema', 'types', 0, 'fields'),
    ('__schema', 'types', 0, 'kind'),
])
async def test_missing_selected_introspection_field_is_rejected(monkeypatch, missing_path):
    schema = build_schema('directive @extra on FIELD\ntype Query { hello: String }')
    full = graphql_sync(schema, get_introspection_query()).formatted['data']
    target = full
    for key in missing_path[:-1]:
        target = target[key]
    target.pop(missing_path[-1])
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def incomplete(*_):
        return {'content': [{'type': 'text', 'text': json.dumps({'data': full})}]}

    monkeypatch.setattr(owner, '_call', incomplete)
    with pytest.raises(GraphQLMcpError, match='introspection_incomplete'):
        await owner.describe()
    assert owner._schema is None
    assert owner._spec is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['fragment', 'nested', 'null_required', 'rootless'])
async def test_incomplete_introspection_nested_shapes_fail(monkeypatch, change):
    schema = build_schema('type Query { hello: String }')
    full = graphql_sync(schema, get_introspection_query()).formatted['data']
    if change == 'fragment':
        full['__schema']['types'][0].pop('kind')
    elif change == 'nested':
        full['__schema']['types'][0] = 'invalid'
    elif change == 'null_required':
        full['__schema']['directives'] = None
    else:
        full['__schema']['queryType'] = None
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def incomplete(*_):
        return {'content': [{'type': 'text', 'text': json.dumps({'data': full})}]}

    monkeypatch.setattr(owner, '_call', incomplete)
    with pytest.raises(GraphQLMcpError, match='introspection_incomplete'):
        await owner.describe()


@pytest.mark.asyncio
async def test_close_reaped_process_without_stderr_task():
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')
    owner._process = _Process([])
    await owner.aclose()
    assert owner._process is None


@pytest.mark.asyncio
async def test_schema_builder_without_query_root_is_rejected(monkeypatch):
    import api_agent.api_providers.graphql_mcp.owner as owner_module
    schema = build_schema('type Query { hello: String }')
    full = graphql_sync(schema, get_introspection_query()).formatted['data']
    owner = GraphQLMcpOwner('http://127.0.0.1:1/graphql')

    async def complete(*_):
        return {'content': [{'type': 'text', 'text': json.dumps({'data': full})}]}

    monkeypatch.setattr(owner, '_call', complete)
    monkeypatch.setattr(owner_module, 'build_client_schema', lambda _: type('NoRoot', (), {'query_type': None})())
    with pytest.raises(GraphQLMcpError, match='introspection_incomplete'):
        await owner.describe()


@pytest.mark.asyncio
async def test_planner_schema_description_scrubs_header_echo(endpoint, monkeypatch):
    import api_agent.api_providers.graphql_mcp.owner as owner_module
    url, _ = endpoint
    secret = 'SYNTHETIC_TEST_ONLY'
    owner = GraphQLMcpOwner(url, headers={'X-Key': secret})
    original_print = owner_module.print_schema
    monkeypatch.setattr(owner_module, 'print_schema', lambda schema: original_print(schema) + '\n# ' + secret)
    try:
        spec = await owner.describe()
        description = spec.input_schema['properties']['query']['description']
        assert secret not in description
        assert '[REDACTED]' in description
    finally:
        await owner.aclose()
