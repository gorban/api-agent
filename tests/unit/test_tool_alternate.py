"""Independent HTTP consumers through actual captured tool selection."""
import json
from types import MappingProxyType

import pytest

from api_agent.api.application import Runtime
from api_agent.config.declarations import ConfigSchema
from api_agent.config.resolve import resolve_config
from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall, Usage
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.tooling.selection import compose_tools
from api_agent.tooling.services import selected_contributions
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.tool_gate import ToolGateError
from tests.unit.test_tool_fixture import FIXTURE, ROOT, get, server


def selected_plan(first='', second='', *, alternate=False, combined=False):
    case = 'test_independent_tools'
    context = LaunchContext(ROOT, ROOT, ROOT, 'fixture', 'fixture', ROOT / 'cfg/config.toml',
        MappingProxyType({}), MappingProxyType({}), workflow_entry=FIXTURE / 'shared_primary/flow.md.html',
        tool_catalog_path=FIXTURE / 'catalog.toml')
    overrides = {'SECONDARY_IMPLEMENTATION': 'rest.toml'} if alternate else {}
    cases = (f'api-agent::tests/integration/test_tool_alternate.py::{case}',)
    if combined:
        cases += ('api-agent::tests/integration/test_tool_instances.py::test_shared_primary',)
    plan = compose_tools(context, ConfigSchema({}), environ={}, overrides=overrides, selection=cases)
    if first:
        overrides.update(API_ENDPOINT=first + '/graphql', API_2_ENDPOINT=second + '/graphql',
                         API_HEADERS=json.dumps({'X-Instance': 'primary'}),
                         API_2_HEADERS=json.dumps({'X-Instance': 'secondary'}))
    return plan.bind_values(resolve_config(plan.schema, environ={}, overrides=overrides))


def test_alternate_fields_and_managed_roots_belong_to_selected_instances():
    graphql = selected_plan()
    rest = selected_plan(alternate=True)
    assert 'API_2_PATH' not in graphql.schema.fields
    assert 'API_2_PATH' in rest.schema.fields
    assert {'API_ENDPOINT', 'API_HEADERS', 'API_2_ENDPOINT', 'API_2_HEADERS'} <= rest.schema.fields.keys()
    assert 'SECONDARY_IMPLEMENTATION' not in rest.review_fields
    assert {item.id for item in rest.tools} == {'fixture/primary', 'fixture/secondary'}
    assert selected_contributions(rest)


@pytest.mark.asyncio
@pytest.mark.parametrize('alternate', [False, True])
async def test_selected_independent_consumer_uses_actual_schemas_headers_and_owners(tmp_path, alternate):
    with server(tmp_path / 'first') as first, server(tmp_path / 'second') as second:
        plan = selected_plan(first, second, alternate=alternate)
        assert not selected_contributions(plan)
        runtime = Runtime('fixture', tool_plan=plan, services={'owner_root': ROOT})
        await runtime.astart()
        try:
            definition, = plan.definitions
            tools = runtime.tools_for(definition)
            primary = tools.gate(scope_kind='node', scope='conversation', port='primary', invocation_id='grant')
            secondary = tools.gate(scope_kind='node', scope='conversation', port='secondary', invocation_id='grant')
            assert primary.owner.owner is not secondary.owner.owner
            if alternate:
                with pytest.raises(ToolGateError, match='selected schema'):
                    await secondary.prepare({'query': '{ hello }'})
            prepared = await primary.prepare({'query': '{ hello }'})
            verified = await primary.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
            with pytest.raises(ToolGateError, match='verification is missing'):
                await secondary.execute(verified)
            expected = {'primary_read': get(first, '/control/value')['hello'],
                        'secondary_read': get(second, '/control/value')['hello']}
            second_args = {'method': 'GET'} if alternate else {'query': '{ hello }'}
            rows = iter([
                LLMResult(Message('assistant', calls=(
                    ToolCall('primary-call', 'primary_read', '{"query":"{ hello }"}'),
                    ToolCall('secondary-call', 'secondary_read', json.dumps(second_args)))),
                    Usage(5, 2), outcome='tool_calls'),
                LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
                LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
                LLMResult(Message('assistant', (TextContent(json.dumps(expected)),)), Usage(5, 2)),
            ])
            requests = []

            async def complete(request):
                requests.append(request)
                return next(rows)

            native = load_native_catalog(definition, definition.layout.steps[0].source)
            journal = {}
            graph = Workflow(definition, {'converse': NodeFunction(native['converse'], 'llm')}, services={
                'tools': tools, 'tool_journal': journal, 'expected_values': expected,
                'complete': complete, 'selection': 'synthetic'})
            output = await graph.arun({})
            assert output['conversation'] == {'verified': True}
            assert journal['final_matches']
            assert all(row['result_matches'] and row['verified_by_inference'] for row in journal['calls'])
            for endpoint, label in ((first, 'primary'), (second, 'secondary')):
                control = get(endpoint, '/control/value')
                assert control['reads'] == 1
                assert control['headers'] == [label]
            assert len(set(expected.values())) == 2
            assert len(requests) == 4
            await primary.execute(verified)
            with pytest.raises(ToolGateError, match='stale or already used'):
                await primary.execute(verified)
        finally:
            await runtime.aclose()
        assert all(owner.closed and owner.disposed for owner in runtime.tool_owners.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(('arguments', 'selected_spec'), [
    ({'method': 'POST'}, True), ({'query': '{ hello }'}, True), ({'method': 'GET'}, False),
])
async def test_rest_owner_rejects_protocol_mismatch_before_http(arguments, selected_spec):
    import importlib.util
    from api_agent.providers.tools import PreparedCall, ToolValidationError

    spec = importlib.util.spec_from_file_location('synthetic_rest_fixture', FIXTURE / 'rest_tool.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner = module.create_tool({'ENDPOINT': 'http://127.0.0.1:1/graphql',
                               'PATH': '/hello', 'HEADERS': {}}, {})
    try:
        description = await owner.describe()
        prepared = PreparedCall(description if selected_spec else None, arguments, 'controlled', 'unused')
        with pytest.raises(ToolValidationError, match='declared hello GET'):
            await owner.invoke(prepared)
    finally:
        await owner.aclose()


def test_combined_case_selection_preserves_independent_secondary_inputs():
    plan = selected_plan(combined=True)
    independent = plan.for_case('api-agent::tests/integration/test_tool_alternate.py::test_independent_tools')
    shared = plan.for_case('api-agent::tests/integration/test_tool_instances.py::test_shared_primary')
    assert not independent.shared_primary
    assert shared.shared_primary == {'fixture/shared-secondary': 'fixture/primary'}
    assert {'API_2_ENDPOINT', 'API_2_HEADERS'} <= plan.schema.fields.keys()
    secondary = next(tool for tool in independent.tools if tool.id == 'fixture/secondary')
    assert secondary.pending['ENDPOINT']['output'] == 'fixture/secondary/http'
    assert secondary.derived_from == {}
    assert plan.schema.fields['API_2_ENDPOINT'].required is True


@pytest.fixture
def rest_fixture():
    import importlib.util

    spec = importlib.util.spec_from_file_location('rest_origin_fixture', FIXTURE / 'rest_tool.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('path', [
    'https://other.example/hello', '//other.example/hello',
    '\n\t//other.example/hello', 'HTTP://OTHER.example/hello',
    'http://selected.example:81/hello', 'https://selected.example/hello',
    'http://selected.example@other.example/hello',
])
def test_rest_owner_rejects_changed_origin_before_client_creation(rest_fixture, monkeypatch, path):
    from unittest.mock import Mock

    client = Mock()
    monkeypatch.setattr(rest_fixture.httpx, 'AsyncClient', client)

    with pytest.raises(ValueError, match='selected origin'):
        rest_fixture.create_tool({'ENDPOINT': 'http://selected.example/graphql',
                                  'PATH': path, 'HEADERS': {'X-Instance': 'secondary'}}, {})

    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'expected'), [
    ('/hello', 'http://selected.example/hello'),
    ('hello?label=one', 'http://selected.example/hello?label=one'),
    ('?label=one', 'http://selected.example/graphql?label=one'),
    ('///hello', 'http://selected.example/hello'),
    ('http://SELECTED.example:80/hello', 'http://selected.example/hello'),
])
async def test_rest_owner_preserves_local_path_query_and_headers(rest_fixture, monkeypatch, path, expected):
    import httpx
    from api_agent.providers.tools import PreparedCall

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'hello': 'synthetic'})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(rest_fixture.httpx, 'AsyncClient',
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    owner = rest_fixture.create_tool({'ENDPOINT': 'http://selected.example/graphql',
        'PATH': path, 'HEADERS': {'X-Instance': 'secondary'}}, {})
    try:
        await owner.invoke(PreparedCall(await owner.describe(), {'method': 'GET'}, 'controlled', 'unused'))
        request, = requests
        assert str(request.url) == expected
        assert request.headers['X-Instance'] == 'secondary'
    finally:
        await owner.aclose()


def test_combined_selection_rejects_explicit_secondary_conflict():
    from api_agent.tooling.models import ToolSelectionError

    with pytest.raises(ToolSelectionError, match='conflicts with shared primary'):
        selected_plan('http://127.0.0.1:1', 'http://127.0.0.1:2', combined=True)


@pytest.mark.parametrize('alternate', [False, True])
def test_actual_independent_publication_retains_match_proofs_after_inference_failure(tmp_path, monkeypatch, alternate):
    import asyncio
    from contextlib import asynccontextmanager
    import importlib.util
    from types import SimpleNamespace

    events_path = tmp_path / 'independent-events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events_path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_independent_tools.py::test_failure (call)')
    spec = importlib.util.spec_from_file_location('independent_publication_trial',
        ROOT / 'tests/integration/test_tool_alternate.py')
    integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(integration)
    with server(tmp_path / 'primary') as first, server(tmp_path / 'secondary') as second:
        plan = selected_plan(first, second, alternate=alternate)
        definition, = plan.definitions
        operation = SimpleNamespace(plan=plan, receipt={},
            endpoint_for=lambda identity: (first if identity == 'fixture/primary' else second) + '/graphql')

        @asynccontextmanager
        async def open_operation(selected):
            runtime = Runtime('fixture', tool_plan=plan, services={'owner_root': ROOT})
            await runtime.astart()
            operation.tools = runtime.tools_for(selected)
            try:
                yield operation
            finally:
                await runtime.aclose()
                operation.receipt.update(tool_count=2, remaining_owners=0, service_count=0,
                                         closed_tools=2, released=True)

        secondary_arguments = {'method': 'GET'} if alternate else {'query': '{ hello }'}
        responses = iter([
            LLMResult(Message('assistant', calls=(
                ToolCall('primary-call', 'primary_read', '{"query":"{ hello }"}'),
                ToolCall('secondary-call', 'secondary_read', json.dumps(secondary_arguments)))),
                Usage(5, 2), outcome='tool_calls'),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            RuntimeError('Synthetic later inference failure'),
        ])

        def complete(request):
            response = next(responses)
            if isinstance(response, BaseException):
                raise response
            return response

        tools = SimpleNamespace(definition_for_case=lambda graph_id: definition,
                                open=open_operation, run=asyncio.run)
        expected = [get(endpoint, '/control/value')['hello'] for endpoint in (first, second)]
        with pytest.raises(RuntimeError, match='Synthetic later inference failure'):
            integration.test_independent_tools(tools, SimpleNamespace(complete=complete),
                                               'synthetic', SimpleNamespace(values={}))
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    report = next(event['data'] for event in events if event['data'].get('kind') == 'tool_execution')
    assert len(report['calls']) == 2
    assert all(call['result_matches'] and call['result_received'] for call in report['calls'])
    assert report['ownership']['owners_remaining'] == 0
    assert report['completed'] is False
    assert all(value not in events_path.read_text() for value in expected)
