"""A declared application factory reaches tools only through the running node's declarations."""
import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.api.application import Runtime
from api_agent.providers.contracts import ContractError
from api_agent.runtime import chat_composition
from api_agent.tooling.runtime import WorkflowTools, bind_workflow_tools, invocation_tools, node_tools
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.execution import NodeContext, NodeFunction, Workflow
from tool_operations_support import GRAMMARS, build, compose

HANDLES = {'tools', 'tool_owner', 'tool_plan', 'runtime'}


@pytest.fixture(params=GRAMMARS)
def grammar(request):
    return request.param


@pytest.fixture
def composition(monkeypatch):
    monkeypatch.setattr(chat_composition, 'WorkflowObservationFactory', Mock())

    def compose_with(runtime, config, graph, **options):
        return chat_composition.compose_chat_runtime(
            SimpleNamespace(owner_root='owner'), config, callback_origin='https://localhost:8443',
            tls_material=None, graph=graph, llm_factory=Mock(return_value=SimpleNamespace(describe=AsyncMock(), aclose=AsyncMock())),
            on_error=None, runtime_factory=Mock(return_value=runtime), nonce='n' * 32, **options)
    return compose_with


NATIVE = '''from api_agent.providers.contracts import ContractError


async def reader(inputs, context):
    lookup, probes = context.tools['lookup'], context.services['probes']
    probes.append(await lookup.query(text='a'))
    for port in ('store', 'absent'):
        try:
            context.tools[port]
        except ContractError:
            probes.append(('denied', port))
    for name in ('mutate', 'explode', 'invoke'):
        try:
            getattr(lookup, name)
        except ContractError:
            probes.append(('denied', name))
    try:
        context.tool('store')
    except ContractError:
        probes.append(('denied', 'compatibility'))
    probes.append(('visible', 'tools' in context.services))
    return {}


async def writer(inputs, context):
    await context.tools['store'].mutate(id=1)
    return {'accepted': True, 'feedback': {}}
'''


async def started(tmp_path, grammar, native=False, **kwargs):
    events = []
    entry = build(tmp_path, grammar, graph_timeout=30.0, **kwargs)
    if native:
        (tmp_path / 'native.py').write_text(NATIVE)
    plan = compose(tmp_path, entry)
    runtime = Runtime('demo', tool_plan=plan, services={'events': events},
                      emissions=EmissionBoundary(ContentPolicy()))
    await runtime.astart()
    return runtime, plan, events


def values(timeout=7.0):
    return {'GRAPH_TIMEOUT': timeout, 'LLM_ALLOWED_MODELS': '.*', 'LLM_DEFAULT_MODEL': 'model'}


def test_application_object_receives_no_tool_handles(tmp_path, grammar, composition):
    async def scenario():
        runtime, plan, _ = await started(tmp_path, grammar)
        try:
            application = SimpleNamespace(definition=plan.definitions[0], complete=AsyncMock())
            composition(runtime, SimpleNamespace(values=values(), tool_plan=plan), application)
            assert not HANDLES & set(vars(application))
            assert not any(isinstance(value, WorkflowTools) or type(value).__name__ == 'RuntimeToolOwner'
                           for value in vars(application).values())
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_the_api_bridge_is_not_an_application_attribute(composition):
    definition = SimpleNamespace(tool_usages=(object(),), definition_id='d')
    application = SimpleNamespace(definition=definition, complete=AsyncMock())
    runtime = SimpleNamespace(services=MappingProxyType({}), emissions=Mock(), tracing=None, factory_plan=())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_composition, 'declared_timeouts', lambda *_: {'GRAPH': 3})
        patch.setattr(chat_composition, 'OwnerTools', Mock())
        patch.setattr(chat_composition, 'WorkflowObservationFactory', Mock())
        chat_composition.compose_chat_runtime(
            SimpleNamespace(owner_root='owner'), SimpleNamespace(values={'API_PROVIDER': 'api.py:make'}, tool_plan=None),
            callback_origin='https://localhost:8443', tls_material=None, graph=application,
            llm_factory=Mock(), on_error=None, runtime_factory=Mock(return_value=runtime), nonce='n' * 32)

    assert not HANDLES & set(vars(application))
    assert runtime.factory_plan[0][:2] == ('api', 'api.py:make')


@pytest.mark.parametrize('claimed', [False, True])
def test_api_need_comes_from_declared_tool_usages_not_the_application(composition, claimed):
    tools = (object(),) if claimed is False else ()
    definition = SimpleNamespace(tool_usages=tools, definition_id='d')
    application = SimpleNamespace(definition=definition, complete=AsyncMock(), requires_api=claimed)
    runtime = SimpleNamespace(services=MappingProxyType({}), emissions=Mock(), tracing=None, factory_plan=())
    config = SimpleNamespace(values={'API_PROVIDER': 'api.py:make'}, tool_plan=None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_composition, 'declared_timeouts', lambda *_: {'GRAPH': 3})
        patch.setattr(chat_composition, 'OwnerTools', Mock())
        composition(runtime, config, application)

    assert bool(runtime.factory_plan) is (claimed is False)


def test_graph_timeout_is_the_declared_row_not_the_applications_mapping(tmp_path, composition):
    async def scenario():
        runtime, plan, _ = await started(tmp_path, 'older')
        try:
            application = SimpleNamespace(definition=plan.definitions[0], complete=AsyncMock(),
                                          timeouts={'GRAPH': 999})
            composed = composition(runtime, SimpleNamespace(values=values(7.0), tool_plan=plan), application)
            assert composed.backend.timeout == 7.0
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_workflow_without_a_declared_graph_timeout_fails_admission_before_resources(tmp_path, grammar, composition):
    plan = compose(tmp_path, build(tmp_path, grammar))
    runtime = Mock()
    application = SimpleNamespace(definition=plan.definitions[0], complete=AsyncMock(),
                                  timeouts={'GRAPH': 120})

    with pytest.raises(ContractError, match='declared GRAPH timeout'):
        composition(runtime, SimpleNamespace(values={}, tool_plan=plan), application)

    runtime.assert_not_called()


def test_resolver_exposes_no_scope_choice_runtime_or_raw_views(tmp_path, grammar):
    async def scenario():
        runtime, plan, events = await started(tmp_path, grammar)
        try:
            tools = runtime.tools_for(plan.definitions[0])
            assert not {'runtime', 'instance', 'for_port', 'gate'} & {name for name in dir(tools)
                                                                      if not name.startswith('__')}
            with pytest.raises(ContractError, match='scope is unavailable'):
                tools.for_node('work')
            with pytest.raises(ContractError, match='scope is unavailable'):
                tools.for_node('absent')
            reader = tools.for_node('reader')
            assert not {'runtime', 'for_port', 'definition'} & {name for name in dir(reader)
                                                                if not name.startswith('__')}
            assert reader.instance('lookup') == 'sample/records'
            with pytest.raises(ContractError, match='port is unavailable'):
                reader.instance('store')
            with pytest.raises(ContractError, match=r'context\.tools\[port\]'):
                reader.borrow('lookup')
            assert events == []
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_ambient_resolver_serves_only_its_own_definition(tmp_path, grammar):
    async def scenario():
        runtime, plan, events = await started(tmp_path, grammar)
        definition = plan.definitions[0]
        try:
            tools = runtime.tools_for(definition)
            with bind_workflow_tools(tools):
                async with invocation_tools(definition, {}) as invocation:
                    assert invocation.resolver is tools
                    assert (await node_tools('reader')['lookup'].introspect())['operation'] == 'introspect'
                async with invocation_tools(SimpleNamespace(definition_id='other', tool_usages=()), {}) as other:
                    assert other.resolver is None
                with pytest.raises(ContractError, match='supplied by the engine'):
                    async with invocation_tools(definition, {'tools': tools}):
                        pytest.fail('application supplied its own resolver')
            with pytest.raises(ContractError, match='has no admitted tool binding: no tools are bound'):
                async with invocation_tools(definition, {}):
                    pytest.fail('declared tools started without a binding')
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_ambient_binding_with_no_resolver_leaves_workflows_without_tools():
    async def scenario():
        with bind_workflow_tools(None):
            async with invocation_tools(SimpleNamespace(definition_id='d', tool_usages=()), {}) as invocation:
                assert invocation.resolver is None
    asyncio.run(scenario())


def test_node_context_compatibility_resolver_serves_only_the_running_nodes_declared_ports(tmp_path):
    async def scenario():
        runtime, plan, _ = await started(tmp_path, 'older')
        definition = plan.definitions[0]
        try:
            async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
                for node, port in (('writer', 'lookup'), ('reader', 'store'), ('reader', 'absent'), ('other', 'lookup')):
                    with pytest.raises(ContractError, match='port is not declared'):
                        NodeContext(node, (), (), {}).tool(port)
                with pytest.raises(ContractError, match=r'context\.tools\[port\]'):
                    NodeContext('reader', (), (), {}).tool('lookup')
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('route', ['engine', 'supplied'])
def test_declared_application_cannot_widen_its_tool_access(tmp_path, composition, route):
    """An adversarial application reaches only declared operations of the running node."""
    async def scenario():
        runtime, plan, events = await started(tmp_path, 'older', native=True)
        definition = plan.definitions[0]
        steps = {step.id: step for step in definition.layout.steps}
        rows = {row['ID']: row for row in definition.layout.tables['Functions']}
        registry = {row['Registry']: NodeFunction(load_native_catalog(definition, steps[name].source)[row['Registry']])
                    for name, row in rows.items()}
        probes = []

        async def complete(request, *, llm, context):
            services = {'probes': probes}
            if route == 'supplied':
                services['tools'] = runtime.tools_for(definition)
            await Workflow(definition, registry, config={'GRAPH_TIMEOUT': 7.0}, services=services).arun({})
        application = SimpleNamespace(definition=definition, complete=complete)
        try:
            if route == 'engine':
                composed = composition(runtime, SimpleNamespace(values=values(), tool_plan=plan), application)
                await composed.workflow_complete('request', llm=None, context=None)
            else:
                await complete(None, llm=None, context=None)
        finally:
            await runtime.aclose()
        return probes, events

    probes, events = asyncio.run(scenario())
    assert probes == [{'operation': 'query', 'arguments': {'text': 'a'}},
                      ('denied', 'store'), ('denied', 'absent'), ('denied', 'mutate'),
                      ('denied', 'explode'), ('denied', 'invoke'), ('denied', 'compatibility'),
                      ('visible', False)]
    assert events == [('query', {'text': 'a'}), ('mutate', {'id': 1}), ('close',)]
