"""Synthetic backend and independent GraphQL targets through normal tool owners."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
from threading import Thread
from types import MappingProxyType
from urllib.request import Request, urlopen

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


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/integration/fixtures/tool_instances'


def fixture_module():
    spec = importlib.util.spec_from_file_location('synthetic_tool_server', FIXTURE / 'server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def server(root, role='api', upstream=''):
    http = fixture_module().serve(role=role, root=root, upstream=upstream, port=0,
                                  schema_path=FIXTURE / 'schema.json' if role == 'api' else None)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(http.server_port)
    finally:
        http.shutdown()
        thread.join(timeout=5)
        http.server_close()


def get(endpoint, path):
    with urlopen(endpoint + path, timeout=5) as response:
        return json.load(response)


def external_plan(first, second):
    context = LaunchContext(ROOT, ROOT, ROOT, 'fixture', 'fixture', ROOT / 'cfg/config.toml',
        MappingProxyType({}), MappingProxyType({}),
        workflow_entry=FIXTURE / 'shared_primary/flow.md.html')
    plan = compose_tools(context, ConfigSchema({}), environ={})
    return plan.bind_values(resolve_config(plan.schema, environ={}, overrides={
        'API_ENDPOINT': first + '/graphql', 'API_2_ENDPOINT': second + '/graphql'}))


def test_backing_seed_is_once_only_and_primary_reads_its_unknown_value(tmp_path):
    with server(tmp_path / 'backing', role='backing') as backing:
        original = get(backing, '/control/value')['hello']
        assert original.startswith('value-')
        with server(tmp_path / 'api', upstream=backing) as primary:
            assert get(primary, '/ready') == {'service': 'api'}
            assert get(primary, '/seed') == {'seed': 'done', 'runs': 1}
            assert get(primary, '/control/value')['hello'] == original
            request = Request(primary + '/graphql', data=b'{"query":"{ hello }"}',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                assert json.load(response) == {'data': {'hello': original}}
            assert get(primary, '/control/value')['reads'] == 1
    with server(tmp_path / 'backing', role='backing') as restarted:
        assert get(restarted, '/control/value')['hello'] == original
        assert get(restarted, '/seed')['runs'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('same_target', [False, True])
async def test_normal_runtime_preserves_two_graphql_owners_and_rejects_cross_instance_replay(tmp_path, same_target):
    with server(tmp_path / 'first') as first, server(tmp_path / 'second') as second:
        secondary = first if same_target else second
        plan = external_plan(first, secondary)
        assert not plan.shared_primary
        assert not selected_contributions(plan)
        runtime = Runtime('fixture', tool_plan=plan, services={'owner_root': ROOT})
        await runtime.astart()
        try:
            definition, = plan.definitions
            tools = runtime.tools_for(definition)
            primary_gate = tools.gate(scope_kind='node', scope='conversation', port='primary', invocation_id='first-call')
            secondary_gate = tools.gate(scope_kind='node', scope='conversation', port='secondary', invocation_id='second-call')
            assert primary_gate.owner.owner is not secondary_gate.owner.owner
            prepared = await primary_gate.prepare({'query': '{ hello }'})
            verified = await primary_gate.verify(prepared, relevance=lambda candidate: {'relevant': True, 'grounded': True})
            with pytest.raises(ToolGateError, match='verification is missing'):
                await secondary_gate.execute(verified)
            first_result = await primary_gate.execute(verified)
            second_prepared = await secondary_gate.prepare({'query': '{ hello }'})
            second_verified = await secondary_gate.verify(second_prepared, relevance=lambda candidate: {'relevant': True, 'grounded': True})
            second_result = await secondary_gate.execute(second_verified)
            assert first_result.data == {'hello': get(first, '/control/value')['hello']}
            assert second_result.data == {'hello': get(secondary, '/control/value')['hello']}
            assert (first_result.data == second_result.data) is same_target
            assert len(runtime.tool_owners) == 2
        finally:
            await runtime.aclose()
        assert all(view.closed for view in runtime._tool_views)


async def graph_trial(tmp_path, journal, final):
    with server(tmp_path / 'backend') as endpoint:
        expected = get(endpoint, '/control/value')['hello']
        runtime = Runtime('fixture', tool_plan=external_plan(endpoint, endpoint), services={'owner_root': ROOT})
        await runtime.astart()
        requests = []
        rows = iter([
            LLMResult(Message('assistant', calls=(
                ToolCall('primary-call', 'primary_read', '{"query":"{ hello }"}'),
                ToolCall('secondary-call', 'secondary_read', '{"query":"{ hello }"}'))),
                Usage(5, 2), outcome='tool_calls'),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            LLMResult(Message('assistant', (TextContent(final(expected)),)), Usage(5, 2)),
        ])

        async def complete(request):
            requests.append(request)
            return next(rows)

        try:
            definition, = runtime.tool_plan.definitions
            native = load_native_catalog(definition, next(step.source for step in definition.layout.steps))
            graph = Workflow(definition, {'converse': NodeFunction(native['converse'], 'llm')}, services={
                'tools': runtime.tools_for(definition), 'tool_journal': journal,
                'expected_value': expected, 'complete': complete, 'selection': 'synthetic'})
            output = await graph.arun({})
            assert expected not in requests[0].payload.messages[0].content[0].text
            return output, requests
        finally:
            await runtime.aclose()


@pytest.mark.asyncio
async def test_shared_native_graph_executes_both_real_tools_and_verifies_final_attribution(tmp_path):
    journal = {}
    output, requests = await graph_trial(tmp_path, journal,
        lambda value: json.dumps({'primary_read': value, 'secondary_read': value}))
    assert output['conversation'] == {'verified': True}
    assert journal['completed'] is True
    assert len(journal['calls']) == 2
    assert journal['final_matches'] is True
    assert all(call['result_matches'] and call['verified_by_inference'] for call in journal['calls'])
    assert sum(request.payload.structured is not None for request in requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('final', [
    lambda value: json.dumps({'primary_read': value, 'secondary_read': 'incorrect'}),
    lambda value: '{"primary_read":' + json.dumps(value) + ',"secondary_read":' + json.dumps(value)
                  + ',"secondary_read":' + json.dumps(value) + '}',
])
async def test_shared_native_graph_rejects_wrong_or_ambiguous_final_and_preserves_results(tmp_path, final):
    journal = {}
    with pytest.raises(ValueError):
        await graph_trial(tmp_path, journal, final)
    assert len(journal['calls']) == 2
    assert all(call['result_matches'] for call in journal['calls'])
    assert len(journal['inference']) == 4
    assert journal['inference'][-1]['usage']['input_tokens'] == 5
    assert journal['final_matches'] is False
